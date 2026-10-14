"""Effective Settings booleans survive rendering and unrelated form saves."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.models.database import AppConfig
from backend.services import system_config_service as service_module
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    require_super_admin,
)
from backend.webui.routes import system_config
from tests.test_notification_events_646 import AsyncDB
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def config_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("effective", [False, True])
async def test_grouped_config_preserves_effective_boolean_and_zero_without_db_row(
    config_db, monkeypatch, effective
):
    settings = get_settings().model_copy(deep=True)
    settings.notification_webhook_enabled = effective
    settings.notification_rate_limit_seconds = 0
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    async with AsyncDB(config_db[0]) as db:
        groups, _ = await service_module.SystemConfigService().load_grouped_configs(db)
    fields = {field["key"]: field for group in groups for field in group["fields"]}
    assert fields["notification_webhook_enabled"]["value"] == (
        "true" if effective else "false"
    )
    assert (
        fields["notification_webhook_enabled"]["default"]
        == str(Settings.model_fields["notification_webhook_enabled"].default).lower()
    )
    assert fields["notification_rate_limit_seconds"]["value"] == "0"


@pytest.mark.asyncio
async def test_db_boolean_overrides_environment_and_explicit_empty_is_preserved(
    config_db, monkeypatch
):
    settings = get_settings().model_copy(deep=True)
    settings.notification_webhook_enabled = True
    settings.notification_webhook_allowed_hosts = "environment.example.org"
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    with Session(config_db[0]) as db:
        db.add_all(
            [
                AppConfig(key_name="notification_webhook_enabled", key_value="False"),
                AppConfig(key_name="notification_webhook_allowed_hosts", key_value=""),
            ]
        )
        db.commit()
    async with AsyncDB(config_db[0]) as db:
        groups, _ = await service_module.SystemConfigService().load_grouped_configs(db)
    fields = {field["key"]: field for group in groups for field in group["fields"]}
    assert fields["notification_webhook_enabled"]["value"] == "false"
    assert fields["notification_webhook_allowed_hosts"]["value"] == ""


def test_legacy_disabled_webhook_stays_disabled_when_saving_another_setting(
    config_db, monkeypatch
):
    settings = get_settings().model_copy(deep=True)
    settings.notification_webhook_enabled = False
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    # Persist real SQL; live application mutation is outside this isolated app.
    async def no_live_settings(_changes):
        pass

    from backend.services import config_center_service

    monkeypatch.setattr(config_center_service, "apply_config_values", no_live_settings)
    app = FastAPI()
    app.include_router(system_config.router)

    async def database():
        async with AsyncDB(config_db[0]) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_super_admin] = lambda: {
        "user_id": 1,
        "role": "super_admin",
        "sub": "alice",
    }
    app.dependency_overrides[get_user_preferences] = lambda: {
        "language": "en",
        "theme": "light",
        "items_per_page": 20,
    }
    with TestClient(app) as client:
        response = client.get("/system-config/", follow_redirects=False)
        assert response.is_redirect
        assert response.headers["location"].startswith("/settings-center/system")
        response = client.post(
            "/system-config/save",
            data={
                "csrf_token": get_csrf_serializer().dumps({}),
                "notification_send_timeout_seconds": "12",
                "notification_webhook_enabled": "false",
            },
            follow_redirects=False,
        )
        assert response.is_redirect
    with Session(config_db[0]) as db:
        rows = {row.key_name: row.key_value for row in db.query(AppConfig)}
        assert rows["notification_webhook_enabled"] == "false"
        assert float(rows["notification_send_timeout_seconds"]) == 12
