"""配置备份导出、校验、精确恢复与路由保护回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute

from backend.models.database import AppConfig
from backend.services.config_backup_service import (
    AI_SECTION,
    BACKUP_FORMAT,
    BACKUP_VERSION,
    GLOBAL_SECTION,
    LEGACY_BACKUP_VERSION,
    SYSTEM_SECTION,
    BackupRecord,
    ConfigBackupError,
    build_backup_document,
    parse_config_backup,
    restore_config_backup,
    serialize_config_backup,
)
from backend.services.system_config_service import (
    SYSTEM_CONFIG_GROUPS,
    SYSTEM_CONFIG_KEYS,
)
from backend.webui.deps import require_csrf, require_super_admin
from backend.webui.routes import config as config_routes
from backend.webui.routes.config import router


def _ai_account_record(
    *,
    account_id: str = "acc_primary",
    api_base: str = "",
) -> BackupRecord:
    return BackupRecord(
        key=f"ai_account.{account_id}",
        value=json.dumps(
            {
                "id": account_id,
                "name": "Primary",
                "provider_id": "openai",
                "protocol": "openai-compatible",
                "api_base": api_base,
                "api_key": "sk-backup-secret",
                "models": ["gpt-4.1"],
                "default_model": "gpt-4.1",
                "enabled": True,
            }
        ),
        description="AI provider account Primary",
    )


def test_combined_backup_round_trip_preserves_allowed_values_and_secrets():
    exported_at = datetime(2026, 7, 29, 10, 30, tzinfo=UTC)
    records = [
        BackupRecord("max_concurrent_reviews", "4", "最大并发审查数量"),
        _ai_account_record(),
        BackupRecord(
            "ai_role_bindings",
            json.dumps(
                {
                    "main": {
                        "primary": {
                            "account": "acc_primary",
                            "model": "gpt-4.1",
                        },
                        "fallback": [],
                    }
                }
            ),
            "AI role bindings",
        ),
        BackupRecord("ai_api_max_retries", "3", "ai_api_max_retries"),
        BackupRecord("app_port", "8000", "app_port"),
    ]

    document = build_backup_document(records, "all", exported_at=exported_at)
    parsed = parse_config_backup(serialize_config_backup(document))

    assert document["format"] == BACKUP_FORMAT
    assert document["version"] == BACKUP_VERSION
    assert document["contains_sensitive_values"] is True
    assert document["exported_at"] == "2026-07-29T10:30:00+00:00"
    assert {record.key for record in parsed[GLOBAL_SECTION]} == {
        "max_concurrent_reviews"
    }
    assert {record.key for record in parsed[SYSTEM_SECTION]} == {"app_port"}
    account = next(
        record
        for record in parsed[AI_SECTION]
        if record.key == "ai_account.acc_primary"
    )
    assert "sk-backup-secret" in account.value


def test_backup_rejects_key_outside_declared_section():
    document = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "scope": "global",
        "sections": {
            "global": {
                "count": 1,
                "configs": [
                    {
                        "key": "ai_api_max_retries",
                        "value": "3",
                        "description": None,
                    }
                ],
            }
        },
    }

    with pytest.raises(ConfigBackupError, match="不属于 global 分类"):
        parse_config_backup(json.dumps(document).encode())


def test_backup_rejects_unsafe_builtin_ai_endpoint():
    document = build_backup_document(
        [_ai_account_record(api_base="https://127.0.0.1:8000/v1")],
        "ai",
    )

    with pytest.raises(ConfigBackupError, match="不能覆盖到本机或私有网络地址"):
        parse_config_backup(serialize_config_backup(document))


def test_backup_rejects_invalid_typed_global_value():
    document = build_backup_document(
        [BackupRecord("max_concurrent_reviews", "many", None)],
        "global",
    )

    with pytest.raises(ConfigBackupError, match="值类型无效"):
        parse_config_backup(serialize_config_backup(document))


def test_system_backup_round_trip_includes_connection_secrets():
    document = build_backup_document(
        [
            BackupRecord(
                "database_url",
                "mysql+asyncmy://user:secret@db/sakura",
                "database_url",
            ),
            BackupRecord("redis_url", "redis://:secret@redis:6379/0", "redis_url"),
            BackupRecord("log_level", "INFO", "log_level"),
        ],
        "system",
    )

    parsed = parse_config_backup(serialize_config_backup(document))

    assert document["contains_sensitive_values"] is True
    assert {record.key for record in parsed[SYSTEM_SECTION]} == {
        "database_url",
        "redis_url",
        "log_level",
    }


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("database_url", "sqlite:///tmp/app.db", "database_url 格式无效"),
        ("app_port", "70000", "app_port 必须在 1 到 65535"),
        ("log_level", "TRACE", "log_level 无效"),
    ],
)
def test_system_backup_rejects_invalid_values(key: str, value: str, message: str):
    document = build_backup_document(
        [BackupRecord(key, value, key)],
        "system",
    )

    with pytest.raises(ConfigBackupError, match=message):
        parse_config_backup(serialize_config_backup(document))


def test_v1_combined_backup_remains_importable_without_system_section():
    document = {
        "format": BACKUP_FORMAT,
        "version": LEGACY_BACKUP_VERSION,
        "scope": "all",
        "sections": {
            "global": {"count": 0, "configs": []},
            "ai": {"count": 0, "configs": []},
        },
    }

    parsed = parse_config_backup(json.dumps(document).encode())

    assert set(parsed) == {GLOBAL_SECTION, AI_SECTION}


def test_system_backup_key_registry_matches_the_system_config_page():
    page_keys = {key for group in SYSTEM_CONFIG_GROUPS for key in group["keys"]}

    assert SYSTEM_CONFIG_KEYS == page_keys
    assert "redis_url" in SYSTEM_CONFIG_KEYS


class _ScalarResult:
    def __init__(self, rows: list[AppConfig]):
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list[AppConfig]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[AppConfig]):
        self.rows = rows
        self.added: list[AppConfig] = []
        self.deleted: list[AppConfig] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _query):
        return _ScalarResult(self.rows)

    def add(self, row: AppConfig) -> None:
        self.added.append(row)

    async def delete(self, row: AppConfig) -> None:
        self.deleted.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_restore_replaces_only_the_included_section_exactly():
    changed = AppConfig(
        key_name="max_concurrent_reviews",
        key_value="2",
        description="old",
    )
    removed = AppConfig(
        key_name="web_search_provider",
        key_value="tavily",
        description="old",
    )
    session = _FakeSession([changed, removed])
    sections = {
        GLOBAL_SECTION: [
            BackupRecord("max_concurrent_reviews", "5", "new"),
            BackupRecord("enable_auto_review", "true", "auto"),
        ]
    }

    result = await restore_config_backup(session, sections)

    assert result.sections == (GLOBAL_SECTION,)
    assert (result.created, result.updated, result.deleted, result.unchanged) == (
        1,
        1,
        1,
        0,
    )
    assert changed.key_value == "5"
    assert changed.description == "new"
    assert session.deleted == [removed]
    assert [row.key_name for row in session.added] == ["enable_auto_review"]
    assert result.deleted_keys == frozenset({"web_search_provider"})
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_system_restore_marks_restart_required_and_stays_in_scope():
    database = AppConfig(
        key_name="database_url",
        key_value="mysql+asyncmy://old/db",
        description="database_url",
    )
    redis = AppConfig(
        key_name="redis_url",
        key_value="redis://old:6379/0",
        description="redis_url",
    )
    session = _FakeSession([database, redis])
    sections = {
        SYSTEM_SECTION: [
            BackupRecord(
                "database_url",
                "mysql+asyncmy://new/db",
                "database_url",
            ),
            BackupRecord("app_domain", "example.com", "app_domain"),
        ]
    }

    result = await restore_config_backup(session, sections)

    assert result.sections == (SYSTEM_SECTION,)
    assert result.requires_restart is True
    assert (result.created, result.updated, result.deleted) == (1, 1, 1)
    assert session.deleted == [redis]


def _route(path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route {method} {path} not found")


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dependency.call for dependency in route.dependant.dependencies]


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/config/backup", "GET"),
        ("/config/backup/export/{scope}", "POST"),
        ("/config/backup/import", "POST"),
    ],
)
def test_config_backup_routes_require_super_admin(path: str, method: str):
    assert require_super_admin in _dependency_calls(_route(path, method))


@pytest.mark.parametrize(
    "path",
    ["/config/backup/export/{scope}", "/config/backup/import"],
)
def test_config_backup_mutations_require_form_csrf(path: str):
    assert require_csrf in _dependency_calls(_route(path, "POST"))


@pytest.mark.asyncio
async def test_download_response_is_attachment_and_never_cached(monkeypatch):
    document = build_backup_document(
        [BackupRecord("max_concurrent_reviews", "4", None)],
        "global",
    )

    async def fake_export(_db, scope):
        assert scope == "global"
        return document

    async def fake_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(config_routes, "export_config_backup", fake_export)
    monkeypatch.setattr(config_routes, "log_admin_action", fake_log)

    response = await config_routes.download_config_backup(
        "global",
        db=object(),
        user={"user_id": 1, "sub": "root"},
        csrf_token="valid",
    )

    assert response.media_type == "application/json"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="sakura-ai-config-global-'
    )
    assert json.loads(response.body)["format"] == BACKUP_FORMAT
