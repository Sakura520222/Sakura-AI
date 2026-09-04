"""Short-lived, one-time Telegram notification binding handshake.

The browser creates a token for an already authenticated internal user.  The
Telegram side only receives the token and uses the chat id from the Telegram
update itself; a caller can therefore never choose an arbitrary chat id to
bind.  Redis is preferred for multi-worker deployments, with a bounded
single-process fallback for installations where Redis is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.core.time_service import now_utc

_TOKEN_PREFIX = "tg_bind_"
_REDIS_KEY_PREFIX = "telegram:bind:"
_MAX_FALLBACK = 1000
_TOKEN_RE = re.compile(r"^tg_bind_[A-Za-z0-9_-]{32,100}$")

# token digest -> (internal user id, created at)
_binding_fallback: dict[str, tuple[int, datetime]] = {}


@dataclass(frozen=True)
class TelegramBindingToken:
    """A token and the values needed to render a Telegram deep link."""

    token: str
    user_id: int
    expires_at: datetime
    deep_link: str | None


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _ttl_seconds() -> int:
    settings = get_settings()
    return max(30, min(3600, int(getattr(settings, "telegram_bind_token_expire_seconds", 300) or 300)))


def _cleanup_fallback(now: datetime | None = None) -> None:
    now = now or now_utc()
    ttl = _ttl_seconds()
    expired = [
        digest
        for digest, (_, created_at) in _binding_fallback.items()
        if (now - created_at).total_seconds() >= ttl
    ]
    for digest in expired:
        _binding_fallback.pop(digest, None)
    overflow = len(_binding_fallback) - _MAX_FALLBACK
    if overflow > 0:
        oldest = sorted(
            _binding_fallback,
            key=lambda digest: _binding_fallback[digest][1],
        )[:overflow]
        for digest in oldest:
            _binding_fallback.pop(digest, None)


async def create_telegram_binding_token(user_id: int) -> TelegramBindingToken:
    """Create and persist a cryptographically random one-time token."""

    if int(user_id) <= 0:
        raise ValueError("invalid internal user id")
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    digest = _token_digest(token)
    created_at = now_utc()
    ttl = _ttl_seconds()
    payload = json.dumps(
        {"user_id": int(user_id), "created_at": created_at.isoformat()},
        separators=(",", ":"),
    )
    try:
        redis = await get_async_redis()
        await redis.setex(f"{_REDIS_KEY_PREFIX}{digest}", ttl, payload)
    except Exception as exc:
        logger.warning("Redis 存储 Telegram 绑定 token 失败，使用内存回退: {}", exc)
        _cleanup_fallback(created_at)
        _binding_fallback[digest] = (int(user_id), created_at)

    settings = get_settings()
    bot_username = (getattr(settings, "telegram_bot_username", None) or "").strip()
    deep_link = f"https://t.me/{bot_username}?start={token}" if bot_username else None
    return TelegramBindingToken(
        token=token,
        user_id=int(user_id),
        expires_at=created_at + timedelta(seconds=ttl),
        deep_link=deep_link,
    )


def _parse_payload(raw: object) -> int | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    return user_id


async def consume_telegram_binding_token(token: str) -> int | None:
    """Atomically consume a token and return its internal user id once."""

    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    digest = _token_digest(token)
    key = f"{_REDIS_KEY_PREFIX}{digest}"
    try:
        redis = await get_async_redis()
        raw = await redis.execute_command("GETDEL", key)
        if raw is not None:
            return _parse_payload(raw)
    except Exception as exc:
        logger.warning("Redis 消费 Telegram 绑定 token 失败，尝试内存回退: {}", exc)

    _cleanup_fallback()
    fallback = _binding_fallback.pop(digest, None)
    if fallback is None:
        return None
    user_id, created_at = fallback
    if (now_utc() - created_at).total_seconds() >= _ttl_seconds():
        return None
    return user_id


__all__ = [
    "TelegramBindingToken",
    "consume_telegram_binding_token",
    "create_telegram_binding_token",
]
