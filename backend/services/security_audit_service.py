"""Security audit service."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.security_models import SecurityEventLog

_SENSITIVE_KEYS = {
    "code",
    "secret",
    "totp_secret",
    "totp_secret_encrypted",
    "recovery_code",
    "recovery_codes",
    "public_key",
    "credential_public_key",
    "credential",
    "assertion",
    "attestation",
}


def sanitize_detail(detail: dict[str, Any] | None) -> str | None:
    """Serialize non-sensitive audit detail."""
    if not detail:
        return None
    sanitized = {
        key: value
        for key, value in detail.items()
        if key.lower() not in _SENSITIVE_KEYS
    }
    if not sanitized:
        return None
    return json.dumps(sanitized, ensure_ascii=False, default=str)


def _request_ip(request: Request | None) -> str | None:
    if not request:
        return None
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:100]
    if request.client:
        return request.client.host[:100]
    return None


def _request_user_agent(request: Request | None) -> str | None:
    if not request:
        return None
    user_agent = request.headers.get("user-agent")
    return user_agent[:500] if user_agent else None


async def record_security_event(
    session: AsyncSession,
    event_type: str,
    event_result: str,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    request: Request | None = None,
    detail: dict[str, Any] | None = None,
) -> SecurityEventLog:
    """Record a security audit event without committing the session."""
    event = SecurityEventLog(
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        event_type=event_type,
        event_result=event_result,
        ip_address=_request_ip(request),
        user_agent=_request_user_agent(request),
        detail=sanitize_detail(detail),
    )
    session.add(event)
    await session.flush()
    logger.info(
        "Security event recorded: type={}, result={}, actor={}, target={}",
        event_type,
        event_result,
        actor_user_id,
        target_user_id,
    )
    return event
