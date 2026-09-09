"""Regression coverage for configured Telegram group/channel destinations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.models import database as database_module
from backend.services import scan_report_service
from backend.telegram import notifications as notifications_module
from backend.telegram.notifications import NotificationSender


class _EmptyResult:
    def all(self):
        return []


class _EmptySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return _EmptyResult()


class _Bot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def _settings(monkeypatch, chat_id: str):
    settings = SimpleNamespace(telegram_default_chat_id=chat_id)
    monkeypatch.setattr(
        "backend.core.config.get_settings", lambda: settings
    )
    return settings


@pytest.mark.asyncio
async def test_only_explicit_configured_negative_system_target_bypasses_filter(
    monkeypatch,
):
    monkeypatch.setattr(database_module, "async_session", None)
    _settings(monkeypatch, "-100999")
    bot = _Bot()
    sender = NotificationSender(bot)

    await sender.send_to_targets("group", [-100123])
    await sender.send_to_targets(
        "wrong group", [], system_chat_ids=[-100123]
    )
    await sender.send_to_targets(
        "configured group", [], system_chat_ids=[-100999]
    )

    assert [item["chat_id"] for item in bot.sent] == [-100999]


@pytest.mark.asyncio
async def test_zero_default_and_negative_legacy_placeholder_are_not_targets(
    monkeypatch,
):
    monkeypatch.setattr(database_module, "async_session", None)
    _settings(monkeypatch, "0")
    bot = _Bot()
    sender = NotificationSender(bot)

    await sender.send_to_targets("placeholder", [-100999])
    await sender.send_to_targets("zero", [], system_chat_ids=[0])

    assert bot.sent == []


@pytest.mark.asyncio
async def test_scan_notification_reaches_group_when_no_user_targets(
    monkeypatch,
):
    monkeypatch.setattr(database_module, "async_session", lambda: _EmptySession())
    settings = _settings(monkeypatch, "-100777")
    monkeypatch.setattr(scan_report_service, "get_settings", lambda: settings)

    bot = _Bot()
    sender = NotificationSender(bot)
    monkeypatch.setattr(
        notifications_module, "get_notification_sender", lambda: sender
    )

    async def no_admins():
        return []

    service = scan_report_service.ScanReportService()
    service._get_all_admin_telegram_ids = no_admins
    service.generate_telegram_message = lambda *_args, **_kwargs: "scan"
    scan = SimpleNamespace(repo_name="owner/repo")

    await service._send_telegram_notification(scan)

    assert [item["chat_id"] for item in bot.sent] == [-100777]
