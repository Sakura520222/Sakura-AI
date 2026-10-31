"""React settings adapters preserve authorization and restore commit boundaries."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from backend.api.v1 import deps, settings_webui
from backend.services.config_backup_service import ConfigBackupError
from backend.webui.deps import generate_csrf_token, get_db


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(settings_webui.router, prefix="/api/v1")
    db = SimpleNamespace(rollback=AsyncMock())
    app.dependency_overrides[get_db] = lambda: db
    auth = AsyncMock(return_value={"user_id": 7, "sub": "admin", "role": "super_admin"})
    monkeypatch.setattr(deps, "require_api_auth", auth)
    monkeypatch.setattr(settings_webui, "log_admin_action", AsyncMock())
    with TestClient(app) as http:
        yield http, db, auth


def headers():
    return {"X-CSRF-Token": generate_csrf_token()}


def test_metadata_is_private_and_requires_super_admin(client):
    http, _db, auth = client
    response = http.get("/api/v1/webui/backup")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert response.json()["scopes"] == ["all", "global", "ai", "system", "users"]
    assert http.get("/api/v1/webui/system").json()["database_reset_confirmation"]
    auth.return_value["role"] = "admin"
    assert http.get("/api/v1/webui/backup").status_code == 403
    assert http.get("/api/v1/webui/system").status_code == 403


def test_unauthenticated_cannot_read_backup_metadata():
    app = FastAPI()
    app.include_router(settings_webui.router, prefix="/api/v1")
    with TestClient(app) as http:
        assert http.get("/api/v1/webui/backup").status_code == 401


def test_export_requires_csrf_and_preserves_existing_download_response(
    client, monkeypatch
):
    http, _db, _auth = client
    export = AsyncMock(
        return_value=Response(
            b'{"format":"fixture"}',
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="backup.json"',
                "Cache-Control": "no-store",
            },
        )
    )
    monkeypatch.setattr(settings_webui.legacy_backup, "download_config_backup", export)
    assert http.post("/api/v1/webui/backup/export/all").status_code == 422
    assert (
        http.post(
            "/api/v1/webui/backup/export/all", headers={"X-CSRF-Token": "invalid"}
        ).status_code
        == 403
    )
    assert export.await_count == 0
    response = http.post("/api/v1/webui/backup/export/all", headers=headers())
    assert response.status_code == 200
    assert (
        response.headers["content-disposition"] == 'attachment; filename="backup.json"'
    )
    assert export.call_args.kwargs["scope"] == "all"


def test_import_protects_connection_settings_and_reports_only_safe_counters(
    client, monkeypatch
):
    http, db, _auth = client
    parsed = {"ai": ["private fixture content"]}
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "parse_config_backup",
        Mock(return_value=parsed),
    )
    restored = SimpleNamespace(
        sections=("ai",),
        created=1,
        updated=2,
        deleted=0,
        unchanged=4,
        requires_restart=False,
    )
    restore = AsyncMock(return_value=restored)
    monkeypatch.setattr(
        settings_webui.config_backup_service, "restore_config_backup", restore
    )
    refresh = Mock()
    monkeypatch.setattr(
        settings_webui.config_backup_service, "refresh_imported_runtime_config", refresh
    )
    response = http.post(
        "/api/v1/webui/backup/import",
        headers=headers(),
        files={
            "backup_file": ("config.json", io.BytesIO(b"fixture"), "application/json")
        },
    )
    assert response.status_code == 200
    restore.assert_awaited_once_with(db, parsed, allow_database_url=False)
    refresh.assert_called_once_with(restored)
    assert response.json()["persisted"] is True
    assert response.json()["created"] == 1
    assert "private fixture content" not in response.text
    assert "no-store" in response.headers["cache-control"]


def test_runtime_failure_does_not_report_committed_import_as_failed(
    client, monkeypatch
):
    http, db, _auth = client
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "parse_config_backup",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "restore_config_backup",
        AsyncMock(
            return_value=SimpleNamespace(
                sections=("global",),
                created=0,
                updated=1,
                deleted=0,
                unchanged=0,
                requires_restart=False,
            )
        ),
    )
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "refresh_imported_runtime_config",
        Mock(side_effect=RuntimeError("fixture refresh failure")),
    )
    response = http.post(
        "/api/v1/webui/backup/import",
        headers=headers(),
        files={"backup_file": ("config.json", b"fixture")},
    )
    assert response.status_code == 200
    assert response.json()["persisted"] is True
    assert response.json()["requires_restart"] is True
    assert response.json()["runtime_refresh_ok"] is False
    assert "fixture refresh failure" not in response.text
    db.rollback.assert_not_awaited()


def test_invalid_backup_is_sanitized_and_never_restored(client, monkeypatch):
    http, db, _auth = client
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "parse_config_backup",
        Mock(side_effect=ConfigBackupError("opaque-private-value")),
    )
    restore = AsyncMock()
    monkeypatch.setattr(
        settings_webui.config_backup_service, "restore_config_backup", restore
    )
    response = http.post(
        "/api/v1/webui/backup/import",
        headers=headers(),
        files={"backup_file": ("config.json", b"fixture")},
    )
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalid_backup"}}
    restore.assert_not_awaited()
    db.rollback.assert_awaited_once()


def test_upload_limit_and_non_admin_block_restore_before_services(client, monkeypatch):
    http, _db, auth = client
    monkeypatch.setattr(settings_webui.config_backup_service, "BACKUP_MAX_BYTES", 4)
    parse = Mock()
    monkeypatch.setattr(
        settings_webui.config_backup_service, "parse_config_backup", parse
    )
    response = http.post(
        "/api/v1/webui/backup/import",
        headers=headers(),
        files={"backup_file": ("config.json", b"12345")},
    )
    assert response.status_code == 413
    auth.return_value["role"] = "user"
    assert (
        http.post(
            "/api/v1/webui/backup/import",
            headers=headers(),
            files={"backup_file": ("config.json", b"123")},
        ).status_code
        == 403
    )
    parse.assert_not_called()


def test_connection_probe_only_accepts_saved_connection_and_csrf(client, monkeypatch):
    http, _db, _auth = client
    probe = AsyncMock(return_value={"success": True, "message": "ok"})
    monkeypatch.setattr(settings_webui.maintenance, "test_connection", probe)
    assert (
        http.post(
            "/api/v1/webui/system/test-connection",
            headers=headers(),
            json={"type": "database", "url": "secret-url"},
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/api/v1/webui/system/test-connection",
            headers=headers(),
            json={"type": "database", "use_saved": False},
        ).status_code
        == 422
    )
    probe.assert_not_awaited()
    response = http.post(
        "/api/v1/webui/system/test-connection",
        headers=headers(),
        json={"type": "database", "use_saved": True},
    )
    assert response.json()["success"] is True
    probe.assert_awaited_once()


def test_restart_and_reset_forward_to_existing_guarded_operations(client, monkeypatch):
    http, _db, _auth = client
    restart = AsyncMock(return_value=Response(status_code=202))
    reset = AsyncMock(return_value=Response(status_code=200))
    monkeypatch.setattr(settings_webui.maintenance, "restart_application", restart)
    monkeypatch.setattr(settings_webui.maintenance, "reset_database", reset)
    assert (
        http.post("/api/v1/webui/system/restart", headers=headers()).status_code == 202
    )
    assert (
        http.post(
            "/api/v1/webui/system/reset-database",
            headers=headers(),
            json={"confirmation": "fixture", "language": "en"},
        ).status_code
        == 200
    )
    restart.assert_awaited_once()
    reset.assert_awaited_once()


def test_plugin_metadata_uses_only_masked_projection(client, monkeypatch):
    http, _db, _auth = client
    monkeypatch.setattr(
        settings_webui,
        "_public_state",
        AsyncMock(
            return_value={
                "plugins": {"version": 1, "mcp": {"servers": []}, "hooks": []},
                "profile": "read_only",
            }
        ),
    )
    response = http.get("/api/v1/webui/plugins?language=en")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert (
        response.json()["schema"]["$defs"]["MCPServerConfig"]["properties"]["headers"][
            "additionalProperties"
        ]["format"]
        == "password"
    )
    assert "read_only" in response.json()["profiles"]


def test_plugin_validation_errors_never_echo_submitted_credentials(client, monkeypatch):
    http, _db, _auth = client
    save = AsyncMock()
    monkeypatch.setattr(settings_webui, "save_plugin_config", save)
    response = http.put(
        "/api/v1/webui/plugins",
        headers=headers(),
        json={
            "plugins": {
                "mcp": {
                    "servers": [
                        {"headers": {"Authorization": "opaque-sensitive-value"}}
                    ]
                }
            },
            "profile": ["invalid-type"],
        },
    )
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalid_plugin_configuration"}}
    assert "opaque-sensitive-value" not in response.text
    save.assert_not_awaited()


def test_plugin_save_uses_domain_service_and_masks_response(client, monkeypatch):
    from backend.services.agent_team.plugin_config import MASK, parse_harness_plugins

    http, db, _auth = client
    plugins = {
        "version": 1,
        "mcp": {
            "servers": [
                {
                    "id": "fixture",
                    "url": "https://example.test/mcp",
                    "timeout_seconds": 10.0,
                    "headers": {"Authorization": "opaque-sensitive-value"},
                }
            ]
        },
        "hooks": [],
    }
    config = parse_harness_plugins(plugins)
    save = AsyncMock(return_value=config)
    monkeypatch.setattr(settings_webui, "save_plugin_config", save)
    response = http.put(
        "/api/v1/webui/plugins",
        headers=headers(),
        json={"plugins": plugins, "profile": "read_only"},
    )
    assert response.status_code == 200
    save.assert_awaited_once_with(db, plugins, "read_only")
    assert (
        response.json()["plugins"]["mcp"]["servers"][0]["headers"]["Authorization"]
        == MASK
    )
    assert "opaque-sensitive-value" not in response.text
    assert response.json()["persisted"] is True


def test_committed_backup_audit_failure_is_reported_without_replaying_restore(
    client, monkeypatch
):
    http, _db, _auth = client
    monkeypatch.setattr(
        settings_webui.config_backup_service,
        "parse_config_backup",
        Mock(return_value={}),
    )
    restore = AsyncMock(
        return_value=SimpleNamespace(
            sections=("global",),
            created=0,
            updated=1,
            deleted=0,
            unchanged=0,
            requires_restart=False,
        )
    )
    monkeypatch.setattr(
        settings_webui.config_backup_service, "restore_config_backup", restore
    )
    monkeypatch.setattr(
        settings_webui.config_backup_service, "refresh_imported_runtime_config", Mock()
    )
    monkeypatch.setattr(
        settings_webui,
        "log_admin_action",
        AsyncMock(side_effect=RuntimeError("fixture audit failure")),
    )
    response = http.post(
        "/api/v1/webui/backup/import",
        headers=headers(),
        files={"backup_file": ("config.json", b"fixture")},
    )
    assert response.status_code == 200
    assert response.json()["persisted"] is True
    assert response.json()["audit_recorded"] is False
    restore.assert_awaited_once()
