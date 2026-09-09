"""Telegram notifications for refund request workflow.

All functions are best-effort: notification failures are logged and never
propagated to the payment/refund business flow.
"""

from __future__ import annotations

from collections.abc import Iterable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.helpers import escape_markdown

from backend.models.identity_models import NotificationEndpoint
from backend.models.payment_models import RefundRequest
from backend.models.telegram_models import TelegramUser
from backend.telegram.notifications import get_notification_sender
from backend.webui.i18n import i18n


def _format_amount(amount_cents: int, currency: str) -> str:
    return f"{currency} {amount_cents / 100:.2f}"


def _safe(value: object) -> str:
    return escape_markdown(str(value or "-"), version=1)


def _unique_chat_ids(values: Iterable[int | str | None]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            chat_id = int(value)
        except TypeError, ValueError, OverflowError:
            continue
        if chat_id <= 0:
            continue
        if chat_id not in seen:
            seen.add(chat_id)
            result.append(chat_id)
    return result


async def _get_user_chat_ids(session: AsyncSession, user_id: int) -> list[int]:
    """Return every enabled, valid Telegram endpoint for one active user."""
    result = await session.execute(
        select(NotificationEndpoint.address)
        .join(TelegramUser, TelegramUser.id == NotificationEndpoint.user_id)
        .where(
            NotificationEndpoint.user_id == user_id,
            TelegramUser.is_active.is_(True),
            NotificationEndpoint.provider == "telegram",
            NotificationEndpoint.enabled.is_(True),
        )
        .order_by(NotificationEndpoint.id)
    )
    return _unique_chat_ids(result.scalars().all())


async def _get_user_chat_id(session: AsyncSession, user_id: int) -> int | None:
    """Backward-compatible first-target helper for legacy callers."""
    chat_ids = await _get_user_chat_ids(session, user_id)
    return chat_ids[0] if chat_ids else None


async def _get_super_admin_chat_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(NotificationEndpoint.address)
        .join(TelegramUser, TelegramUser.id == NotificationEndpoint.user_id)
        .where(
            TelegramUser.role == "super_admin",
            TelegramUser.is_active.is_(True),
            NotificationEndpoint.provider == "telegram",
            NotificationEndpoint.enabled.is_(True),
        )
    )
    db_chat_ids = list(result.scalars().all())
    return _unique_chat_ids(db_chat_ids)


def _request_context(refund_request: RefundRequest) -> dict[str, str]:
    order = getattr(refund_request, "order", None)
    plan = getattr(order, "plan", None) if order else None
    user = getattr(refund_request, "user", None)
    return {
        "request_id": str(refund_request.id),
        "order_no": getattr(order, "order_no", None) or str(refund_request.order_id),
        "amount": _format_amount(refund_request.amount_cents, refund_request.currency),
        "reason": refund_request.reason or "-",
        "review_note": refund_request.review_note or "-",
        "error": refund_request.error_message or "-",
        "user": getattr(user, "github_username", None)
        or getattr(user, "telegram_id", None)
        or refund_request.user_id,
        "plan": getattr(plan, "name", None) or "-",
    }


def _message(key: str, lang: str, **kwargs: object) -> str:
    safe_kwargs = {name: _safe(value) for name, value in kwargs.items()}
    return i18n.t(key, lang=lang, **safe_kwargs)


async def _send_to_targets(text: str, chat_ids: list[int]) -> None:
    sender = get_notification_sender()
    if not sender or not chat_ids:
        return
    await sender.send_to_targets(text, chat_ids)


async def notify_refund_request_submitted(
    session: AsyncSession,
    refund_request: RefundRequest,
) -> None:
    """Notify super admins that a new refund request needs review."""
    try:
        chat_ids = await _get_super_admin_chat_ids(session)
        ctx = _request_context(refund_request)
        text = _message("telegram_refund.admin_new_request", "zh-CN", **ctx)
        await _send_to_targets(text, chat_ids)
    except Exception as exc:
        logger.warning(
            "Failed to send refund request notification: request_id={}, error={}",
            getattr(refund_request, "id", None),
            exc,
        )


async def notify_refund_request_approved(
    session: AsyncSession,
    refund_request: RefundRequest,
) -> None:
    """Notify the requester that their refund request was approved."""
    try:
        chat_ids = await _get_user_chat_ids(session, refund_request.user_id)
        ctx = _request_context(refund_request)
        text = _message("telegram_refund.approved", "zh-CN", **ctx)
        await _send_to_targets(text, chat_ids)
    except Exception as exc:
        logger.warning(
            "Failed to send refund approval notification: request_id={}, error={}",
            getattr(refund_request, "id", None),
            exc,
        )


async def notify_refund_request_rejected(
    session: AsyncSession,
    refund_request: RefundRequest,
) -> None:
    """Notify the requester that their refund request was rejected."""
    try:
        chat_ids = await _get_user_chat_ids(session, refund_request.user_id)
        ctx = _request_context(refund_request)
        text = _message("telegram_refund.rejected", "zh-CN", **ctx)
        await _send_to_targets(text, chat_ids)
    except Exception as exc:
        logger.warning(
            "Failed to send refund rejection notification: request_id={}, error={}",
            getattr(refund_request, "id", None),
            exc,
        )


async def notify_refund_request_failed(
    session: AsyncSession,
    refund_request: RefundRequest,
) -> None:
    """Notify super admins that refund execution failed."""
    try:
        chat_ids = await _get_super_admin_chat_ids(session)
        ctx = _request_context(refund_request)
        text = _message("telegram_refund.failed", "zh-CN", **ctx)
        await _send_to_targets(text, chat_ids)
    except Exception as exc:
        logger.warning(
            "Failed to send refund failure notification: request_id={}, error={}",
            getattr(refund_request, "id", None),
            exc,
        )
