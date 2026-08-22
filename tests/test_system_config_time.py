from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core import config as config_module
from backend.core.config import Settings
from backend.core.time_service import InvalidTimezoneError
from backend.services.system_config_service import (
    RESTART_REQUIRED_KEYS,
    SYSTEM_CONFIG_KEYS,
    SystemConfigService,
)


def test_settings_default_timezone_and_system_config_contract():
    assert Settings().app_timezone == "system"
    assert "app_timezone" in SYSTEM_CONFIG_KEYS
    assert "app_timezone" in RESTART_REQUIRED_KEYS


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, config=None):
        self.config = config
        self.commits = 0

    async def execute(self, _statement):
        return _Result(self.config)

    def add(self, value):
        self.config = value

    async def commit(self):
        self.commits += 1


class _ConfigRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ConfigResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ConfigRows(self._rows)


class _ConfigSession:
    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        if self._error:
            raise self._error
        return _ConfigResult(self._rows)


@pytest.mark.anyio
async def test_save_timezone_validates_before_writing():
    service = SystemConfigService()
    db = _Session()
    with patch("backend.services.system_config_service.resolve_timezone") as resolver:
        resolver.side_effect = InvalidTimezoneError("invalid")
        with pytest.raises(ValueError, match="无效应用时区"):
            await service.save_configs(db, {"app_timezone": "CST"})
    assert db.commits == 0
    assert db.config is None


@pytest.mark.anyio
async def test_restart_required_timezone_is_not_hot_applied():
    service = SystemConfigService()
    with (
        patch("backend.services.system_config_service.update_settings_field") as update,
        patch(
            "backend.services.system_config_service.invalidate_dynamic_config_cache"
        ) as invalidate,
    ):
        await service.apply_live_settings(
            {"app_timezone": {"raw_new": "America/New_York", "new": "America/New_York"}}
        )
    update.assert_not_called()
    invalidate.assert_called_once()


@pytest.mark.anyio
async def test_required_timezone_query_failure_is_fail_closed(monkeypatch):
    settings = Settings(app_timezone="UTC")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        config_module, "get_all_db_config_keys", lambda: ["app_timezone"]
    )
    monkeypatch.setattr(
        "backend.models.database.async_session",
        lambda: _ConfigSession(error=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="app_timezone"):
        await config_module.load_dynamic_configs_to_settings(
            required_keys={"app_timezone"}
        )


@pytest.mark.anyio
async def test_required_timezone_rejects_invalid_database_value(monkeypatch):
    settings = Settings(app_timezone="UTC")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        config_module, "get_all_db_config_keys", lambda: ["app_timezone"]
    )
    monkeypatch.setattr(
        "backend.models.database.async_session",
        lambda: _ConfigSession(
            rows=[SimpleNamespace(key_name="app_timezone", key_value="CST")]
        ),
    )

    with pytest.raises(RuntimeError, match="app_timezone") as exc_info:
        await config_module.load_dynamic_configs_to_settings(
            required_keys={"app_timezone"}
        )
    assert isinstance(exc_info.value.__cause__, InvalidTimezoneError)
    assert settings.app_timezone == "UTC"


@pytest.mark.anyio
async def test_non_timezone_cast_failure_keeps_dynamic_loader_tolerant(monkeypatch):
    settings = Settings(app_timezone="system", app_port=8000)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        config_module,
        "get_all_db_config_keys",
        lambda: ["app_timezone", "app_port"],
    )
    monkeypatch.setattr(
        "backend.models.database.async_session",
        lambda: _ConfigSession(
            rows=[
                SimpleNamespace(key_name="app_timezone", key_value="UTC"),
                SimpleNamespace(key_name="app_port", key_value="bad-port"),
            ]
        ),
    )
    original_cast = config_module._cast_config_type

    def cast_with_non_timezone_failure(value, expected_type):
        if value == "bad-port":
            raise ValueError("invalid port")
        return original_cast(value, expected_type)

    monkeypatch.setattr(
        config_module, "_cast_config_type", cast_with_non_timezone_failure
    )

    await config_module.load_dynamic_configs_to_settings(required_keys={"app_timezone"})

    assert settings.app_timezone == "UTC"
    assert settings.app_port == 8000
