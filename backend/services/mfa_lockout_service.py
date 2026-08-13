"""MFA verification failure tracking and account lockout service.

Tracks failed TOTP / Passkey / recovery-code attempts per user in Redis.
When the configurable threshold is reached the account is temporarily
locked out.  All state is keyed by ``user_id`` so it works regardless of
which transport (WebUI / API) the attempt comes from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.core.time_service import now_utc

# Redis key patterns
_FAIL_COUNT_PREFIX = "mfa:fail:count:"
_LOCK_PREFIX = "mfa:lock:"

# In-memory fallback when Redis is unavailable.
# NOTE: Not thread-safe; acceptable because the ASGI server runs a single
# async event-loop thread.  Data is lost on process restart.
_MAX_FALLBACK_ENTRIES = 1000
_lock_fallback: dict[int, tuple[int, datetime]] = {}
_fail_fallback: dict[int, tuple[int, datetime]] = {}


class AccountLockedError(Exception):
    """Raised when the user account is temporarily locked due to too many failed MFA attempts."""

    def __init__(self, remaining_seconds: int):
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Account temporarily locked. Try again in {remaining_seconds}s."
        )


def _lock_ttl_seconds() -> int:
    return get_settings().mfa_lockout_duration_minutes * 60


def _threshold() -> int:
    return get_settings().mfa_lockout_threshold


async def record_mfa_failure(user_id: int) -> int:
    """Record a failed MFA verification attempt.

    Returns the current failure count after incrementing.
    If the threshold is reached, the account is locked.
    """
    threshold = _threshold()
    lock_ttl = _lock_ttl_seconds()
    count_key = f"{_FAIL_COUNT_PREFIX}{user_id}"
    lock_key = f"{_LOCK_PREFIX}{user_id}"

    try:
        redis = await get_async_redis()
        pipe = redis.pipeline()
        pipe.incr(count_key)
        pipe.expire(count_key, lock_ttl)
        results = await pipe.execute()
        count = int(results[0])

        if count >= threshold:
            await redis.setex(lock_key, lock_ttl, "1")
            logger.warning(
                "MFA account locked: user_id={}, failures={}, lock_ttl={}s",
                user_id,
                count,
                lock_ttl,
            )
            # Fire-and-forget notification owns a short-lived DB session; keep
            # it in the reset supervisor so DROP cannot race its commit.
            from backend.services.database_reset_runtime_service import (
                DatabaseResetRuntimeAdmissionClosed,
                create_registered_background_task,
            )

            try:
                create_registered_background_task(
                    _notify_lockout(user_id), "mfa_lockout_notification"
                )
            except DatabaseResetRuntimeAdmissionClosed:
                logger.info("跳过清库静默期内的 MFA lockout 通知: user_id={}", user_id)
        return count
    except Exception as exc:
        logger.warning("Redis MFA fail track error, using memory fallback: {}", exc)
        return _record_mfa_failure_fallback(user_id, threshold, lock_ttl)


def _cleanup_expired_fallbacks() -> None:
    """Remove expired entries from both fallback dicts."""
    now = now_utc()
    lock_ttl = _lock_ttl_seconds()
    for store in (_fail_fallback, _lock_fallback):
        expired = [
            uid
            for uid, (_, ts) in store.items()
            if (now - ts).total_seconds() > lock_ttl
        ]
        for uid in expired:
            store.pop(uid, None)


def _record_mfa_failure_fallback(user_id: int, threshold: int, lock_ttl: int) -> int:
    if len(_fail_fallback) > _MAX_FALLBACK_ENTRIES:
        _cleanup_expired_fallbacks()
    now = now_utc()
    count_val, created_at = _fail_fallback.get(user_id, (0, now))
    # If previous entry is older than lock TTL, reset
    if (now - created_at).total_seconds() > lock_ttl:
        count_val = 0
        created_at = now
    count_val += 1
    _fail_fallback[user_id] = (count_val, created_at)

    if count_val >= threshold:
        _lock_fallback[user_id] = (1, now)
        # Clear the in-memory failure counter since the user is now locked
        _fail_fallback.pop(user_id, None)
        logger.warning(
            "MFA account locked (fallback): user_id={}, failures={}", user_id, count_val
        )
        # The fallback still sends a notification through a short-lived DB
        # session; register it so reset quiesce can reject/await it as well.
        from backend.services.database_reset_runtime_service import (
            DatabaseResetRuntimeAdmissionClosed,
            create_registered_background_task,
        )

        try:
            create_registered_background_task(
                _notify_lockout(user_id), "mfa_lockout_notification"
            )
        except DatabaseResetRuntimeAdmissionClosed:
            logger.info("跳过清库静默期内的 MFA fallback 通知: user_id={}", user_id)
    return count_val


async def check_mfa_lockout(user_id: int) -> None:
    """Raise ``AccountLockedError`` if the user is currently locked out."""
    lock_key = f"{_LOCK_PREFIX}{user_id}"
    try:
        redis = await get_async_redis()
        ttl = await redis.ttl(lock_key)
        if ttl and ttl > 0:
            raise AccountLockedError(ttl)
        return
    except AccountLockedError:
        raise
    except Exception as exc:
        logger.warning("Redis MFA lock check error, trying fallback: {}", exc)
        _check_mfa_lockout_fallback(user_id)


def _check_mfa_lockout_fallback(user_id: int) -> None:
    if len(_lock_fallback) > _MAX_FALLBACK_ENTRIES:
        _cleanup_expired_fallbacks()
    if user_id not in _lock_fallback:
        return
    _, locked_at = _lock_fallback[user_id]
    elapsed = (now_utc() - locked_at).total_seconds()
    remaining = _lock_ttl_seconds() - elapsed
    if remaining > 0:
        raise AccountLockedError(int(remaining))
    # Lock expired, clean up
    _lock_fallback.pop(user_id, None)


async def _notify_lockout(user_id: int) -> None:
    """Send a Telegram notification when MFA lockout is triggered.

    Creates its own short-lived DB session since the lockout service
    does not receive one from callers.
    """
    try:
        from backend.models import database as db_module
        from backend.services.mfa_notification_service import notify_mfa_event

        async with db_module.async_session() as session:
            await notify_mfa_event(session, user_id, "mfa_lockout")
    except Exception as exc:
        logger.warning(
            "Failed to send MFA lockout notification: user_id={}, error={}",
            user_id,
            exc,
        )


async def reset_mfa_failures(user_id: int) -> None:
    """Clear failure count and lock after a successful MFA verification."""
    count_key = f"{_FAIL_COUNT_PREFIX}{user_id}"
    lock_key = f"{_LOCK_PREFIX}{user_id}"
    try:
        redis = await get_async_redis()
        await redis.delete(count_key, lock_key)
    except Exception as exc:
        logger.warning("Redis MFA reset error, cleaning fallback: {}", exc)
    _fail_fallback.pop(user_id, None)
    _lock_fallback.pop(user_id, None)


async def get_mfa_lockout_status(user_id: int) -> dict[str, Any]:
    """Return lockout status info for display purposes."""
    lock_key = f"{_LOCK_PREFIX}{user_id}"
    count_key = f"{_FAIL_COUNT_PREFIX}{user_id}"
    locked = False
    remaining = 0
    failures = 0
    try:
        redis = await get_async_redis()
        ttl = await redis.ttl(lock_key)
        if ttl and ttl > 0:
            locked = True
            remaining = ttl
        failures = int(await redis.get(count_key) or 0)
    except Exception:
        if user_id in _lock_fallback:
            _, locked_at = _lock_fallback[user_id]
            elapsed = (now_utc() - locked_at).total_seconds()
            rem = _lock_ttl_seconds() - elapsed
            if rem > 0:
                locked = True
                remaining = int(rem)
        fb = _fail_fallback.get(user_id)
        failures = fb[0] if fb else 0

    return {
        "locked": locked,
        "remaining_seconds": remaining,
        "failure_count": failures,
        "threshold": _threshold(),
    }
