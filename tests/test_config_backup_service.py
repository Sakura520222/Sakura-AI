"""配置备份导出、校验、精确恢复与路由保护回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute

from backend.core import config_sections
from backend.core.config_section_defaults import (
    LABEL_SECTION_DEFAULTS,
    STRATEGY_SECTION_DEFAULTS,
)
from backend.models.database import AppConfig
from backend.services.config_backup_service import (
    AI_SECTION,
    BACKUP_FORMAT,
    BACKUP_VERSION,
    GLOBAL_SECTION,
    LABEL_SECTION,
    LEGACY_BACKUP_VERSION,
    STRATEGY_SECTION,
    SYSTEM_SECTION,
    BackupRecord,
    ConfigBackupError,
    ConfigImportResult,
    build_backup_document,
    parse_config_backup,
    refresh_imported_runtime_config,
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
    assert document["exported_at"] == "2026-07-29T10:30:00.000000Z"
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
        "exported_at": "2026-08-12T12:00:00.000000Z",
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


@pytest.mark.parametrize("version", [2, BACKUP_VERSION])
def test_backup_skips_unknown_legacy_keys_instead_of_rejecting(version):
    """含已移除配置键（如旧版平铺键）的备份可导入，未知键跳过不报错。

    历史 v2 备份在线上存在且携带后来删除的平铺键，恢复必须宽容跳过。
    """
    document = {
        "format": BACKUP_FORMAT,
        "version": version,
        "exported_at": "2026-08-12T12:00:00.000000Z",
        "scope": "global",
        "sections": {
            "global": {
                "count": 3,
                "configs": [
                    {
                        "key": "max_concurrent_reviews",
                        "value": "4",
                        "description": None,
                    },
                    {
                        "key": "issue_max_tool_iterations",
                        "value": "200",
                        "description": None,
                    },
                    {
                        "key": "auto_index_pr_changes",
                        "value": "true",
                        "description": None,
                    },
                ],
            }
        },
    }

    parsed = parse_config_backup(json.dumps(document).encode())

    assert {record.key for record in parsed[GLOBAL_SECTION]} == {
        "max_concurrent_reviews"
    }


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


@pytest.mark.parametrize("value", ["", " ", " CST", "UTC+08:00", "not/a-zone"])
def test_system_backup_rejects_invalid_app_timezone(value: str):
    document = build_backup_document(
        [BackupRecord("app_timezone", value, "app_timezone")],
        "system",
    )
    with pytest.raises(ConfigBackupError, match="app_timezone"):
        parse_config_backup(serialize_config_backup(document))


def test_system_backup_accepts_system_or_iana_app_timezone():
    for value in ("system", "UTC", "America/New_York"):
        document = build_backup_document(
            [BackupRecord("app_timezone", value, "app_timezone")],
            "system",
        )
        parsed = parse_config_backup(serialize_config_backup(document))
        assert parsed[SYSTEM_SECTION][0].value == value


@pytest.mark.parametrize(
    "exported_at", [None, "", "2026-08-12 12:00:00", "2026-08-12T12:00:00"]
)
def test_v2_backup_rejects_missing_or_naive_exported_at(exported_at):
    document = build_backup_document([], "global")
    if exported_at is None:
        document.pop("exported_at")
    else:
        document["exported_at"] = exported_at

    with pytest.raises(ConfigBackupError, match="exported_at"):
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


def test_v2_backup_without_section_configs_still_imports():
    # v2 备份（无 strategy/label 节）在 v3 导入端保持可恢复
    document = {
        "format": BACKUP_FORMAT,
        "version": 2,
        "exported_at": "2026-08-12T12:00:00.000000Z",
        "scope": "all",
        "sections": {
            "global": {"count": 0, "configs": []},
            "ai": {"count": 0, "configs": []},
            "system": {"count": 0, "configs": []},
        },
    }

    parsed = parse_config_backup(json.dumps(document).encode())

    assert set(parsed) == {GLOBAL_SECTION, AI_SECTION, SYSTEM_SECTION}


def test_v3_all_backup_includes_strategy_and_label_sections():
    records = [
        BackupRecord("max_concurrent_reviews", "4", None),
        BackupRecord(
            "strategy.strategies",
            json.dumps({"standard": {"prompt": "custom standard prompt"}}),
            "strategy.strategies",
        ),
        BackupRecord(
            "label.definitions",
            json.dumps({"bug": {"color": "000000", "description": "缺陷"}}),
            "label.definitions",
        ),
    ]

    document = build_backup_document(records, "all")
    parsed = parse_config_backup(serialize_config_backup(document))

    assert document["version"] == BACKUP_VERSION
    assert set(document["sections"]) == {
        GLOBAL_SECTION,
        AI_SECTION,
        SYSTEM_SECTION,
        STRATEGY_SECTION,
        LABEL_SECTION,
    }
    assert document["contains_sensitive_values"] is False
    assert {record.key for record in parsed[STRATEGY_SECTION]} == {
        "strategy.strategies"
    }
    assert {record.key for record in parsed[LABEL_SECTION]} == {"label.definitions"}
    assert {record.key for record in parsed[GLOBAL_SECTION]} == {
        "max_concurrent_reviews"
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-json{", "不是有效 JSON"),
        (json.dumps(["not", "a", "dict"]), "必须是 JSON 对象"),
        # 合法 JSON 但结构校验失败：标签颜色非 6 位十六进制
        (
            json.dumps({"bug": {"color": "xyz", "description": "d"}}),
            "结构无效",
        ),
    ],
)
def test_section_backup_rejects_invalid_payload(value: str, message: str):
    document = build_backup_document(
        [BackupRecord("label.definitions", value, "label.definitions")],
        "all",
    )

    with pytest.raises(ConfigBackupError, match=message):
        parse_config_backup(serialize_config_backup(document))


@pytest.mark.asyncio
async def test_restore_replaces_strategy_section_exactly():
    strategies_row = AppConfig(
        key_name="strategy.strategies",
        key_value=json.dumps({"standard": {"prompt": "old"}}),
        description="strategy.strategies",
    )
    extra_row = AppConfig(
        key_name="strategy.file_filters",
        key_value=json.dumps({"skip_paths": [".git/"]}),
        description="strategy.file_filters",
    )
    session = _FakeSession([strategies_row, extra_row])
    sections = {
        STRATEGY_SECTION: [
            BackupRecord(
                "strategy.strategies",
                json.dumps({"standard": {"prompt": "custom standard prompt"}}),
                "strategy.strategies",
            )
        ]
    }

    result = await restore_config_backup(session, sections)

    assert result.sections == (STRATEGY_SECTION,)
    # 节内备份缺失的键（file_filters）被删除 → 该节回退内置默认
    assert (result.created, result.updated, result.deleted, result.unchanged) == (
        0,
        1,
        1,
        0,
    )
    assert session.deleted == [extra_row]
    assert strategies_row.key_value == json.dumps(
        {"standard": {"prompt": "custom standard prompt"}}
    )
    assert result.deleted_keys == frozenset({"strategy.file_filters"})
    assert session.committed is True
    assert session.rolled_back is False


def test_refresh_imported_runtime_config_syncs_section_store():
    config_sections.clear_section_store()
    try:
        config_sections.update_section_store(
            "label.definitions",
            {"bug": {"color": "000000", "description": "覆盖将被删除"}},
        )
        result = ConfigImportResult(
            sections=(STRATEGY_SECTION, LABEL_SECTION),
            created=1,
            updated=0,
            deleted=1,
            unchanged=0,
            imported_values={
                "strategy.strategies": json.dumps(
                    {"standard": {"prompt": "custom standard prompt"}}
                ),
            },
            deleted_keys=frozenset({"label.definitions"}),
            requires_restart=False,
        )

        refresh_imported_runtime_config(result)

        # 导入的覆盖进入 store（与默认深度合并读取）
        merged = config_sections.get_section_config("strategy.strategies")
        assert merged["standard"]["prompt"] == "custom standard prompt"
        assert merged["quick"] == STRATEGY_SECTION_DEFAULTS["strategies"]["quick"]
        # 被删除的节键回退内置默认
        assert (
            config_sections.get_section_config("label.definitions")
            == LABEL_SECTION_DEFAULTS["labels"]
        )
    finally:
        config_sections.clear_section_store()


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


@pytest.mark.asyncio
async def test_runtime_restore_rejects_database_url_with_setup_guidance():
    session = _FakeSession(
        [
            AppConfig(
                key_name="database_url",
                key_value="mysql+asyncmy://old/db",
                description="database_url",
            )
        ]
    )
    sections = {
        SYSTEM_SECTION: [
            BackupRecord(
                "database_url",
                "mysql+asyncmy://new/db",
                "database_url",
            )
        ]
    }

    with pytest.raises(ConfigBackupError, match="Setup") as exc_info:
        await restore_config_backup(
            session,
            sections,
            allow_database_url=False,
        )

    assert "mysql+asyncmy://new/db" not in str(exc_info.value)
    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_runtime_restore_protects_connection_anchor_when_backup_omits_it():
    database = AppConfig(
        key_name="database_url",
        key_value="mysql+asyncmy://current/db",
        description="database_url",
    )
    redis = AppConfig(
        key_name="redis_url",
        key_value="redis://old:6379/0",
        description="redis_url",
    )
    session = _FakeSession([database, redis])
    sections = {
        SYSTEM_SECTION: [BackupRecord("redis_url", "redis://new:6379/0", "redis_url")]
    }

    result = await restore_config_backup(
        session,
        sections,
        allow_database_url=False,
    )

    assert result.updated == 1
    assert session.deleted == []
    assert database.key_value == "mysql+asyncmy://current/db"


class _BackupUpload:
    filename = "backup.json"

    async def read(self, _limit):
        return b"backup"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_runtime_backup_route_passes_database_url_guard(monkeypatch):
    sections = {
        SYSTEM_SECTION: [
            BackupRecord(
                "database_url",
                "mysql+asyncmy://new/db",
                "database_url",
            )
        ]
    }
    restore = AsyncMock(
        side_effect=ConfigBackupError("database_url restore database_url through Setup")
    )
    monkeypatch.setattr(config_routes, "parse_config_backup", lambda _content: sections)
    monkeypatch.setattr(config_routes, "restore_config_backup", restore)
    monkeypatch.setattr(config_routes, "detect_language", lambda: "en")
    monkeypatch.setattr(
        config_routes,
        "toast_redirect",
        lambda *_args, **kwargs: kwargs,
    )

    db = object()
    result = await config_routes.upload_config_backup(
        _BackupUpload(),
        db=db,
        user={"user_id": 1, "sub": "admin"},
        csrf_token="csrf",
    )

    restore.assert_awaited_once_with(
        db,
        sections,
        allow_database_url=False,
    )
    assert "Setup" in result["reason"]


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
