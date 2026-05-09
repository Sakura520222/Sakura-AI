"""Super-admin security management service."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import AppConfig
from backend.models.security_models import SecurityEventLog
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)

GLOBAL_MFA_REQUIRED_CONFIG_KEY = "security_global_mfa_required"


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
    if not users:
        return []

    user_ids = [user.id for user in users]
    recovery_rows = await session.execute(
        select(UserRecoveryCode.user_id, func.count(UserRecoveryCode.id))
        .where(
            UserRecoveryCode.user_id.in_(user_ids),
            UserRecoveryCode.used_at.is_(None),
        )
        .group_by(UserRecoveryCode.user_id)
    )
    recovery_counts = {user_id: int(count or 0) for user_id, count in recovery_rows.all()}

    passkey_rows = await session.execute(
        select(UserWebAuthnCredential.user_id, func.count(UserWebAuthnCredential.id))
        .where(UserWebAuthnCredential.user_id.in_(user_ids))
        .group_by(UserWebAuthnCredential.user_id)
    )
    passkey_counts = {user_id: int(count or 0) for user_id, count in passkey_rows.all()}

    event_rows = await session.execute(
        select(SecurityEventLog.target_user_id, func.max(SecurityEventLog.created_at))
        .where(SecurityEventLog.target_user_id.in_(user_ids))
        .group_by(SecurityEventLog.target_user_id)
    )
    last_event_at = {user_id: value for user_id, value in event_rows.all()}

    return [
        UserSecuritySummary(
            user=user,
            recovery_code_count=recovery_counts.get(user.id, 0),
            passkey_count=passkey_counts.get(user.id, 0),
            last_security_event_at=last_event_at.get(user.id),
        )
        for user in users
    ]


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


async def is_global_mfa_required(session: AsyncSession) -> bool:
    """Return whether all users are required to enroll MFA."""
    value = await session.scalar(
        select(AppConfig.key_value).where(
            AppConfig.key_name == GLOBAL_MFA_REQUIRED_CONFIG_KEY
        )
    )
    return str(value).lower() in {"1", "true", "yes", "on"}


async def set_global_mfa_required(session: AsyncSession, required: bool) -> None:
    """Set global MFA enrollment requirement."""
    result = await session.execute(
        select(AppConfig).where(AppConfig.key_name == GLOBAL_MFA_REQUIRED_CONFIG_KEY)
    )
    config = result.scalar_one_or_none()
    value = "true" if required else "false"
    if config:
        config.key_value = value
    else:
        session.add(
            AppConfig(
                key_name=GLOBAL_MFA_REQUIRED_CONFIG_KEY,
                key_value=value,
                description="Require all users to enroll at least one MFA method",
            )
        )


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


async def user_has_any_mfa_method(session: AsyncSession, user: TelegramUser) -> bool:
    """Return whether a user can complete MFA with TOTP or at least one passkey."""
    if user.totp_enabled:
        return True
    passkey_count = await session.scalar(
        select(func.count(UserWebAuthnCredential.id)).where(
            UserWebAuthnCredential.user_id == user.id
        )
    )
    return int(passkey_count or 0) > 0


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
