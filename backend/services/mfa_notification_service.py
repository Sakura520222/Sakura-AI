"""Helper to send MFA security Telegram notifications.

This is a thin wrapper that resolves the user's Telegram chat_id from
their ``user_id`` and delegates to ``NotificationSender.send_mfa_event``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models.telegram_models import TelegramUser
from backend.telegram.notifications import get_notification_sender


async def notify_mfa_event(
    session: AsyncSession,
    user_id: int,
    event_type: str,
    detail: str = "",
) -> None:
    """Send an MFA security notification to the user via Telegram.

    Non-blocking: failures are logged but never raised.
    """
    sender = get_notification_sender()
    if not sender:
        return

    try:
        result = await session.execute(
            select(TelegramUser.telegram_id).where(TelegramUser.id == user_id)
        )
        chat_id = result.scalar_one_or_none()
        if not chat_id:
            return
        await sender.send_mfa_event(
            event_type=event_type,
            detail=detail,
            chat_id=int(chat_id),
        )
    except Exception as exc:
        logger.warning(
            "Failed to send MFA notification: user_id={}, error={}", user_id, exc
        )
