"""Effective Settings booleans survive rendering and unrelated form saves."""

import pytest
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.models.database import AppConfig
from backend.services import system_config_service as service_module
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
