"""Super-admin security management service."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.security_models import SecurityEventLog
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)


@dataclass(frozen=True)
class UserSecuritySummary:
    """Security status summary for a user."""

    user: TelegramUser
    recovery_code_count: int
    passkey_count: int
    last_security_event_at: object | None


async def get_user_security_summaries(
    session: AsyncSession,
) -> list[UserSecuritySummary]:
    """Return MFA status summaries for all users."""
    users_result = await session.execute(
        select(TelegramUser).order_by(TelegramUser.role, TelegramUser.github_username)
    )
    users = list(users_result.scalars().all())
    summaries: list[UserSecuritySummary] = []
    for user in users:
        summaries.append(await get_user_security_summary(session, user))
    return summaries


async def get_user_security_summary(
    session: AsyncSession,
    user: TelegramUser,
) -> UserSecuritySummary:
    """Return MFA status summary for a single user."""
    recovery_count = await session.scalar(
        select(func.count(UserRecoveryCode.id)).where(
            UserRecoveryCode.user_id == user.id,
            UserRecoveryCode.used_at.is_(None),
        )
    )
    passkey_count = await session.scalar(
        select(func.count(UserWebAuthnCredential.id)).where(
            UserWebAuthnCredential.user_id == user.id
        )
    )
    last_event_at = await session.scalar(
        select(func.max(SecurityEventLog.created_at)).where(
            SecurityEventLog.target_user_id == user.id
        )
    )
    return UserSecuritySummary(
        user=user,
        recovery_code_count=int(recovery_count or 0),
        passkey_count=int(passkey_count or 0),
        last_security_event_at=last_event_at,
    )


async def get_user_passkeys(
    session: AsyncSession, user_id: int
) -> list[UserWebAuthnCredential]:
    """Return passkeys for a user."""
    result = await session.execute(
        select(UserWebAuthnCredential)
        .where(UserWebAuthnCredential.user_id == user_id)
        .order_by(UserWebAuthnCredential.created_at.desc())
    )
    return list(result.scalars().all())


async def get_recent_security_events(
    session: AsyncSession,
    user_id: int | None = None,
    limit: int = 50,
) -> list[SecurityEventLog]:
    """Return recent security events."""
    query = select(SecurityEventLog).order_by(SecurityEventLog.created_at.desc()).limit(limit)
    if user_id is not None:
        query = query.where(SecurityEventLog.target_user_id == user_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def reset_user_totp(session: AsyncSession, target_user: TelegramUser) -> None:
    """Reset TOTP and recovery codes for a target user."""
    target_user.totp_enabled = False
    target_user.totp_secret_encrypted = None
    target_user.totp_enabled_at = None
    target_user.totp_last_used_step = None
    await session.execute(
        delete(UserRecoveryCode).where(UserRecoveryCode.user_id == target_user.id)
    )


def user_has_mfa_enabled(user: TelegramUser, passkey_count: int = 0) -> bool:
    """Return whether a user has at least one MFA method enabled."""
    return bool(user.totp_enabled or passkey_count > 0)


async def set_user_mfa_required(
    session: AsyncSession, target_user: TelegramUser, required: bool
) -> None:
    """Set whether a user must enroll MFA before using normal WebUI/API features."""
    target_user.mfa_required = required


async def delete_user_passkey(
    session: AsyncSession, target_user_id: int, credential_id: int
) -> int:
    """Delete one passkey owned by the target user."""
    result = await session.execute(
        delete(UserWebAuthnCredential).where(
            UserWebAuthnCredential.id == credential_id,
            UserWebAuthnCredential.user_id == target_user_id,
        )
    )
    return int(result.rowcount or 0)


async def delete_user_passkeys(session: AsyncSession, target_user_id: int) -> int:
    """Delete all passkeys owned by the target user."""
    result = await session.execute(
        delete(UserWebAuthnCredential).where(
            UserWebAuthnCredential.user_id == target_user_id
        )
    )
    return int(result.rowcount or 0)


async def reset_user_mfa(session: AsyncSession, target_user: TelegramUser) -> None:
    """Reset all MFA methods for a target user."""
    await reset_user_totp(session, target_user)
    await delete_user_passkeys(session, target_user.id)
