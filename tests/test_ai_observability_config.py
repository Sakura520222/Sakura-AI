"""Contracts for the AI observability configuration surface."""

import json

import pytest

from backend.api.v1 import config as config_api
from backend.core.config import AI_STRATEGY_CONFIG_KEYS, Settings


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _RecordingDb:
    def __init__(self):
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return _ScalarResult()

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


def test_observability_settings_are_runtime_strategy_keys():
    expected = {
        "activity_reasoning_capture_enabled",
        "activity_request_response_capture_enabled",
        "activity_reasoning_provider_allowlist",
        "activity_reasoning_protocol_allowlist",
        "activity_artifact_retention_days",
        "activity_artifact_encryption_key_id",
        "activity_artifact_super_admin_read_enabled",
    }

    assert expected.issubset(AI_STRATEGY_CONFIG_KEYS)
    settings = Settings()
    assert settings.activity_reasoning_capture_enabled is False
    assert settings.activity_request_response_capture_enabled is False
    assert settings.activity_artifact_retention_days >= 1


@pytest.mark.asyncio
async def test_observability_settings_save_to_app_config_and_refresh_runtime(
    monkeypatch,
):
    db = _RecordingDb()
    refreshed = []
    monkeypatch.setattr(
        config_api,
        "update_settings_field",
        lambda key, value: refreshed.append((key, value)),
    )
    request = config_api.AIStrategyRequest(
        activity_reasoning_capture_enabled=True,
        activity_request_response_capture_enabled=True,
        activity_reasoning_provider_allowlist="anthropic,openai",
        activity_reasoning_protocol_allowlist="anthropic_native,responses",
        activity_artifact_retention_days=30,
        activity_artifact_encryption_key_id="app-fernet-v2",
        activity_artifact_super_admin_read_enabled=False,
    )

    response = await config_api.put_ai_strategy_settings(
        request,
        db=db,
        user={"sub": "super-admin"},
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["success"] is True
    assert db.commits == 1
    assert {row.key_name: row.key_value for row in db.added} == {
        "activity_reasoning_capture_enabled": "true",
        "activity_request_response_capture_enabled": "true",
        "activity_reasoning_provider_allowlist": "anthropic,openai",
        "activity_reasoning_protocol_allowlist": "anthropic_native,responses",
        "activity_artifact_retention_days": "30",
        "activity_artifact_encryption_key_id": "app-fernet-v2",
        "activity_artifact_super_admin_read_enabled": "false",
    }
    assert dict(refreshed) == {row.key_name: row.key_value for row in db.added}


@pytest.mark.asyncio
async def test_observability_retention_rejects_out_of_range_value(monkeypatch):
    db = _RecordingDb()
    refreshed = []
    monkeypatch.setattr(
        config_api,
        "update_settings_field",
        lambda key, value: refreshed.append((key, value)),
    )

    response = await config_api.put_ai_strategy_settings(
        config_api.AIStrategyRequest(activity_artifact_retention_days=0),
        db=db,
        user={"sub": "super-admin"},
    )

    assert response.status_code == 400
    assert db.added == []
    assert db.commits == 0
    assert refreshed == []
