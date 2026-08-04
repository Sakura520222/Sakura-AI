"""Setup/bootstrap/config 旧扁平 AI 配置硬切换回归测试。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.api.v1.setup import CompleteSetupRequest
from backend.core import bootstrap
from backend.core.setup_service import _ENV_TO_SETTINGS_KEY, ENV_FIELD_GROUPS

LEGACY_KEYS = {
    "AI_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_MODEL",
    "SUMMARY_PROVIDER",
    "SUMMARY_API_KEY",
    "SUMMARY_API_BASE",
    "SUMMARY_MODEL",
}


def test_setup_env_mapping_does_not_write_legacy_flat_ai_keys():
    assert LEGACY_KEYS.isdisjoint(_ENV_TO_SETTINGS_KEY)
    assert LEGACY_KEYS.isdisjoint(
        {field for fields in ENV_FIELD_GROUPS.values() for field in fields}
    )


def test_setup_request_does_not_accept_legacy_flat_ai_fields():
    body = CompleteSetupRequest.model_validate(
        {
            "DATABASE_URL": "postgresql://db",
            "OPENAI_API_KEY": "legacy-key",
            "OPENAI_API_BASE": "https://legacy.example/v1",
            "OPENAI_MODEL": "legacy-model",
        }
    )
    assert not LEGACY_KEYS.intersection(body.model_dump())


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        result.all.return_value = self.rows
        return result


@pytest.mark.asyncio
async def test_bootstrap_missing_fields_does_not_query_legacy_openai_key(monkeypatch):
    session = _FakeSession(
        [
            ("github_app_id", "app"),
            ("github_private_key", "private"),
            ("github_webhook_secret", "secret"),
            ("telegram_bot_token", "bot"),
        ]
    )
    monkeypatch.setattr("backend.models.database.async_session", lambda: session)

    missing = await bootstrap.get_missing_fields()

    assert "OPENAI_API_KEY" not in missing
    assert "TELEGRAM_BOT_TOKEN" not in missing
    assert all(
        "openai_api_key" not in str(statement.compile())
        for statement in session.statements
    )


@pytest.mark.asyncio
async def test_bootstrap_current_step_does_not_require_legacy_openai_key(monkeypatch):
    session = _FakeSession(
        [
            ("github_app_id", "app"),
            ("github_private_key", "private"),
            ("github_webhook_secret", "secret"),
        ]
    )
    db_module = __import__("backend.models.database", fromlist=["async_engine"])
    monkeypatch.setattr(db_module, "async_engine", object())
    monkeypatch.setattr("backend.models.database.async_session", lambda: session)
    monkeypatch.setattr(
        bootstrap,
        "read_connection_config",
        lambda: {"database_url": "postgresql://db"},
    )

    assert await bootstrap.get_current_step() == 3
    assert all(
        "openai_api_key" not in str(statement.compile())
        for statement in session.statements
    )


@pytest.mark.asyncio
async def test_setup_save_rejects_legacy_env_and_settings_keys(monkeypatch):
    service = __import__(
        "backend.core.setup_service", fromlist=["SetupService"]
    ).SetupService()
    monkeypatch.setattr(
        "backend.core.config.update_settings_field",
        lambda *_args: pytest.fail("legacy setting must not be applied"),
    )

    assert (
        await service.save_configs_to_db(
            {
                "OPENAI_API_KEY": "legacy-key",
                "openai_api_base": "https://legacy.example/v1",
                "SUMMARY_PROVIDER": "legacy",
            }
        )
        == 0
    )


def test_startup_and_telegram_status_do_not_read_legacy_model_setting():
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "backend/main.py").read_text(encoding="utf-8")
    telegram_source = (root / "backend/telegram/handlers.py").read_text(
        encoding="utf-8"
    )
    assert "settings.openai_model" not in main_source
    assert "settings.openai_model" not in telegram_source


def test_config_api_has_no_legacy_provider_credential_fallback():
    import backend.api.v1.config as config_api

    assert not hasattr(config_api, "_resolve_provider_credentials")
    assert not hasattr(config_api, "_PROVIDER_KEY_NAMES")
