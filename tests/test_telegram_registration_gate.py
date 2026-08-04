"""Telegram Bot 用户注册关闭时的统一门禁测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from backend.core.config import get_settings
from backend.telegram import handlers


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _UserService:
    user = None

    def __init__(self, _session):
        pass

    async def get_user_by_telegram_id(self, _telegram_id):
        return self.user


def _update(telegram_id=12345):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_registration_gate_blocks_unregistered_user_when_disabled(monkeypatch):
    settings = get_settings()
    old_value = settings.allow_user_registration
    settings.allow_user_registration = False
    monkeypatch.setattr(handlers, "get_async_session", _SessionContext)
    monkeypatch.setattr(handlers, "TelegramService", _UserService)
    update = _update()

    try:
        with pytest.raises(ApplicationHandlerStop):
            await handlers.enforce_registration_gate(update, None)
    finally:
        settings.allow_user_registration = old_value

    update.effective_message.reply_text.assert_awaited_once_with(
        "❌ 用户自注册已关闭，请联系管理员创建账号"
    )


@pytest.mark.asyncio
async def test_registration_gate_allows_registered_active_user(monkeypatch):
    settings = get_settings()
    old_value = settings.allow_user_registration
    settings.allow_user_registration = False
    _UserService.user = SimpleNamespace(is_active=True)
    monkeypatch.setattr(handlers, "get_async_session", _SessionContext)
    monkeypatch.setattr(handlers, "TelegramService", _UserService)
    update = _update()

    try:
        await handlers.enforce_registration_gate(update, None)
    finally:
        _UserService.user = None
        settings.allow_user_registration = old_value

    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_gate_is_inactive_when_registration_enabled():
    settings = get_settings()
    old_value = settings.allow_user_registration
    settings.allow_user_registration = True
    update = _update()

    try:
        await handlers.enforce_registration_gate(update, None)
    finally:
        settings.allow_user_registration = old_value

    update.effective_message.reply_text.assert_not_awaited()
