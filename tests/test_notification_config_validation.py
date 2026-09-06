from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.services.system_config_service import (
    RESTART_REQUIRED_KEYS,
    SystemConfigService,
    SystemConfigValidationError,
)


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.added = []
        self.commits = 0
        self.queries = 0

    async def execute(self, _statement):
        self.queries += 1
        return _Result(self.rows.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("smtp_port", "1.5"),
        ("smtp_port", "65536"),
        ("notification_max_concurrency", "0"),
        ("notification_retry_max_attempts", "21"),
        ("notification_retry_initial_delay_seconds", "nan"),
        ("notification_retry_backoff_factor", "inf"),
        ("notification_rate_limit_seconds", "-0.1"),
    ],
)
async def test_invalid_notification_batch_has_no_db_side_effects(key, value):
    """Invalid values fail before even querying or mutating an ORM row."""
    service = SystemConfigService()
    db = _Session([SimpleNamespace(key_value="old")])

    with pytest.raises(SystemConfigValidationError):
        await service.save_configs(
            db,
            {"smtp_port": "465", key: value},
        )

    assert db.queries == 0
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_valid_notification_and_telegram_changes_persist_with_restart_flag():
    service = SystemConfigService()
    existing_telegram_enabled = SimpleNamespace(key_value="true")
    db = _Session([SimpleNamespace(key_value="587"), existing_telegram_enabled, None])

    changed, needs_restart = await service.save_configs(
        db,
        {
            "smtp_port": "465",
            "telegram_enabled": "false",
            "telegram_bot_token": "new-token",
        },
    )

    assert needs_restart is True
    assert changed["smtp_port"]["raw_new"] == "465"
    assert changed["telegram_enabled"]["raw_new"] == "false"
    assert changed["telegram_bot_token"]["raw_new"] == "new-token"
    assert existing_telegram_enabled.key_value == "false"
    assert [row.key_value for row in db.added] == ["new-token"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_telegram_restart_required_changes_are_not_hot_applied():
    service = SystemConfigService()
    assert {"telegram_enabled", "telegram_bot_token"}.issubset(
        RESTART_REQUIRED_KEYS
    )

    with (
        patch(
            "backend.services.system_config_service.update_settings_field"
        ) as update,
        patch(
            "backend.services.system_config_service.invalidate_dynamic_config_cache"
        ) as invalidate,
    ):
        await service.apply_live_settings(
            {
                "telegram_enabled": {"raw_new": "false", "new": "false"},
                "telegram_bot_token": {
                    "raw_new": "new-token",
                    "new": "new-token",
                },
            }
        )

    update.assert_not_called()
    invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_unchanged_telegram_setting_does_not_require_restart():
    service = SystemConfigService()
    db = _Session([SimpleNamespace(key_value="true")])

    changed, needs_restart = await service.save_configs(
        db,
        {"telegram_enabled": "true"},
    )

    assert changed == {}
    assert needs_restart is False
    assert db.commits == 0


def test_unknown_system_update_key_is_not_accepted():
    with pytest.raises(SystemConfigValidationError):
        SystemConfigService.validate_updates({"not_a_setting": "value"})


def test_system_config_template_has_numeric_constraints_for_notification_fields():
    template = (
        Path(__file__).parents[1]
        / "backend"
        / "webui"
        / "templates"
        / "system_config.html"
    )
    text = template.read_text(encoding="utf-8")
    expected = {
        "smtp_port": ('min="1" max="65535" step="1"',),
        "notification_max_concurrency": ('min="1" max="100" step="1"',),
        "notification_retry_max_attempts": ('min="1" max="20" step="1"',),
        "notification_retry_initial_delay_seconds": ('min="0" step="any"',),
        "notification_retry_backoff_factor": ('min="1" step="any"',),
        "notification_rate_limit_seconds": ('min="0" step="any"',),
    }
    for key, fragments in expected.items():
        assert f"item.key == '{key}'" in text
        assert all(fragment in text for fragment in fragments)
