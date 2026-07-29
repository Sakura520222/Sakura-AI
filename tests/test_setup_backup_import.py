"""首次部署 Setup 模式导入配置备份的回归测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from backend.core.setup_service import SetupService
from backend.services.config_backup_service import (
    AI_SECTION,
    GLOBAL_SECTION,
    SYSTEM_SECTION,
    BackupRecord,
    ConfigImportResult,
    build_backup_document,
    serialize_config_backup,
)
from backend.webui.routes import setup as setup_route


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _response_json(response) -> dict:
    return json.loads(response.body)


def _setup_backup_content() -> str:
    document = build_backup_document(
        [
            BackupRecord(
                "database_url",
                "mysql+asyncmy://user:secret@db/sakura",
                "database_url",
            ),
            BackupRecord("redis_url", "redis://redis:6379/0", "redis_url"),
            BackupRecord("github_app_id", "123456", "github_app_id"),
            BackupRecord("github_private_key", "private-key", "github_private_key"),
            BackupRecord("webui_secret_key", "session-secret", "webui_secret_key"),
            BackupRecord(
                "embedding_api_key",
                "embedding-secret",
                "embedding_api_key",
            ),
        ],
        "all",
    )
    return serialize_config_backup(document).decode()


@pytest.mark.asyncio
async def test_setup_backup_inspection_prefills_only_wizard_fields(monkeypatch):
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)

    response = await setup_route.inspect_config_backup(
        _Request({"content": _setup_backup_content()})
    )

    assert response.status_code == 200
    payload = _response_json(response)
    assert payload["success"] is True
    assert payload["sections"] == [GLOBAL_SECTION, AI_SECTION, SYSTEM_SECTION]
    assert payload["setup_values"]["database_url"].startswith("mysql+asyncmy://")
    assert payload["setup_values"]["github_private_key"] == "private-key"
    assert payload["setup_values"]["embedding_api_key"] == "embedding-secret"
    assert "webui_secret_key" not in payload["setup_values"]
    assert payload["requires_database_url"] is False


@pytest.mark.asyncio
async def test_setup_backup_inspection_is_disabled_after_setup(monkeypatch):
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: False)
    parse_backup = AsyncMock()
    monkeypatch.setattr(setup_route, "parse_config_backup", parse_backup)

    response = await setup_route.inspect_config_backup(
        _Request({"content": _setup_backup_content()})
    )

    assert response.status_code == 403
    assert _response_json(response)["success"] is False
    parse_backup.assert_not_called()


@pytest.mark.asyncio
async def test_setup_complete_revalidates_backup_and_passes_parsed_sections(monkeypatch):
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    complete = AsyncMock(return_value={"success": False, "message": "stop before restart"})
    monkeypatch.setattr(setup_route.setup_service, "complete_setup", complete)

    response = await setup_route.complete_setup(
        _Request(
            {
                "DATABASE_URL": "mysql+asyncmy://override:secret@db/sakura",
                "ADMIN_GITHUB_USERNAME": "admin",
                "ADMIN_TELEGRAM_ID": "123",
                "CONFIG_BACKUP": _setup_backup_content(),
            }
        )
    )

    assert response.status_code == 200
    call = complete.await_args
    assert "CONFIG_BACKUP" not in call.args[0]
    assert call.args[0]["DATABASE_URL"].startswith("mysql+asyncmy://override:")
    assert set(call.kwargs["backup_sections"]) == {
        GLOBAL_SECTION,
        AI_SECTION,
        SYSTEM_SECTION,
    }


@pytest.mark.asyncio
async def test_setup_complete_rejects_invalid_backup_before_service_call(monkeypatch):
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    complete = AsyncMock()
    monkeypatch.setattr(setup_route.setup_service, "complete_setup", complete)

    response = await setup_route.complete_setup(
        _Request(
            {
                "ADMIN_GITHUB_USERNAME": "admin",
                "ADMIN_TELEGRAM_ID": "123",
                "CONFIG_BACKUP": '{"format":"not-sakura"}',
            }
        )
    )

    assert response.status_code == 400
    assert "备份文件无效" in _response_json(response)["message"]
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_setup_restores_backup_before_explicit_setup_values(monkeypatch):
    service = SetupService()
    database_url = "mysql+asyncmy://backup:secret@db/sakura"
    sections = {
        GLOBAL_SECTION: [
            BackupRecord("embedding_api_key", "embedding-secret", None),
        ],
        AI_SECTION: [],
        SYSTEM_SECTION: [
            BackupRecord("database_url", database_url, None),
            BackupRecord("webui_secret_key", "backup-session-secret", None),
            BackupRecord(
                "activity_cursor_signing_secret",
                "backup-cursor-secret",
                None,
            ),
        ],
    }
    import_result = ConfigImportResult(
        sections=(GLOBAL_SECTION, AI_SECTION, SYSTEM_SECTION),
        created=4,
        updated=0,
        deleted=0,
        unchanged=0,
        imported_values={},
        deleted_keys=frozenset(),
        requires_restart=True,
    )
    events: list[str] = []
    saved: dict[str, str] = {}

    async def init_database(value):
        assert value == database_url
        events.append("init")

    async def restore_backup(value):
        assert value is sections
        events.append("restore")
        return import_result

    async def save_configs(values):
        saved.update(values)
        events.append("save")
        return len(values)

    async def create_admin(github_username, telegram_id, value):
        assert (github_username, telegram_id, value) == ("admin", 123, database_url)
        events.append("admin")

    monkeypatch.setattr(service, "init_database", init_database)
    monkeypatch.setattr(service, "restore_backup_for_setup", restore_backup)
    monkeypatch.setattr(service, "save_configs_to_db", save_configs)
    monkeypatch.setattr(service, "create_admin_user", create_admin)
    monkeypatch.setattr(
        "backend.core.setup_service.mark_setup_completed",
        lambda value: events.append(f"complete:{value}"),
    )

    result = await service.complete_setup(
        {
            "ADMIN_GITHUB_USERNAME": "admin",
            "ADMIN_TELEGRAM_ID": "123",
            "REDIS_URL": "redis://override:6379/0",
        },
        backup_sections=sections,
    )

    assert result["success"] is True
    assert events == [
        "init",
        "restore",
        "save",
        "admin",
        f"complete:{database_url}",
    ]
    assert saved["DATABASE_URL"] == database_url
    assert saved["REDIS_URL"] == "redis://override:6379/0"
    assert saved["WEBUI_SECRET_KEY"] == "backup-session-secret"
    assert saved["ACTIVITY_CURSOR_SIGNING_SECRET"] == "backup-cursor-secret"
    assert "ENABLE_RAG" not in saved
    assert result["backup_import"]["created"] == 4


@pytest.mark.asyncio
async def test_legacy_backup_without_database_url_requires_manual_database(monkeypatch):
    service = SetupService()
    init_database = AsyncMock()
    monkeypatch.setattr(service, "init_database", init_database)

    result = await service.complete_setup(
        {
            "ADMIN_GITHUB_USERNAME": "admin",
            "ADMIN_TELEGRAM_ID": "123",
        },
        backup_sections={GLOBAL_SECTION: [], AI_SECTION: []},
    )

    assert result["success"] is False
    assert "数据库连接字符串为必填项" in result["message"]
    init_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_failure_does_not_mark_setup_completed(monkeypatch):
    service = SetupService()
    save_configs = AsyncMock()
    create_admin = AsyncMock()
    mark_completed = AsyncMock()

    monkeypatch.setattr(service, "init_database", AsyncMock())
    monkeypatch.setattr(
        service,
        "restore_backup_for_setup",
        AsyncMock(side_effect=RuntimeError("restore failed")),
    )
    monkeypatch.setattr(service, "save_configs_to_db", save_configs)
    monkeypatch.setattr(service, "create_admin_user", create_admin)
    monkeypatch.setattr(
        "backend.core.setup_service.mark_setup_completed",
        mark_completed,
    )

    result = await service.complete_setup(
        {
            "DATABASE_URL": "mysql+asyncmy://user:secret@db/sakura",
            "ADMIN_GITHUB_USERNAME": "admin",
            "ADMIN_TELEGRAM_ID": "123",
        },
        backup_sections={GLOBAL_SECTION: []},
    )

    assert result["success"] is False
    assert "restore failed" in result["message"]
    save_configs.assert_not_awaited()
    create_admin.assert_not_awaited()
    mark_completed.assert_not_awaited()


def test_setup_template_submits_backup_only_with_final_completion():
    source = (
        setup_route.templates.env.loader.get_source(
            setup_route.templates.env,
            "setup_wizard.html",
        )[0]
    )

    assert "/setup/api/backup/inspect" in source
    assert "map.CONFIG_BACKUP = this.backupContent" in source
    assert "map.EMBEDDING_API_KEY = this.form.embedding_api_key" in source
