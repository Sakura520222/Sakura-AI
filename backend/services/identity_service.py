"""Internal user, identity, and notification endpoint services.

This module is the compatibility boundary for the old ``telegram_users``
model.  New authentication and notification code should use the functions in
this module instead of matching Telegram ids or GitHub usernames directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import now_utc
from backend.models.identity_models import (
    AuthProvider,
    NotificationEndpoint,
    NotificationProvider,
    UserIdentity,
)
from backend.models.telegram_models import TelegramUser


@dataclass(frozen=True)
class GitHubAccount:
    """Normalized GitHub profile returned by the OAuth provider."""

    provider_user_id: str
    username: str
    avatar_url: str | None = None
    email: str | None = None
    email_verified: bool = False


def registration_quota_values() -> dict[str, int]:
    """Return the configured quotas for a newly self-registered user.

    OAuth registration must retain the same multiplier semantics that the
    legacy Telegram registration path used.  The values are read only when a
    new internal user is created; existing users are never changed.
    """

    from backend.core.config import get_settings

    settings = get_settings()
    multiplier = float(getattr(settings, "register_quota_multiplier", 0.2) or 0.2)
    fields = (
        "daily_quota",
        "weekly_quota",
        "monthly_quota",
        "issue_daily_quota",
        "issue_weekly_quota",
        "issue_monthly_quota",
        "agent_daily_quota",
        "agent_weekly_quota",
        "agent_monthly_quota",
    )
    source_fields = (
        "init_user_daily_quota",
        "init_user_weekly_quota",
        "init_user_monthly_quota",
        "init_user_issue_daily_quota",
        "init_user_issue_weekly_quota",
        "init_user_issue_monthly_quota",
        "init_user_agent_daily_quota",
        "init_user_agent_weekly_quota",
        "init_user_agent_monthly_quota",
    )
    return {
        field: max(1, int(getattr(settings, source, 1) * multiplier))
        for field, source in zip(fields, source_fields, strict=True)
    }


def _normalized_email(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if "@" in value and len(value) <= 320 else None


def _legacy_provider_id(username: str) -> str:
    return f"legacy:{username.strip().lower()}"


async def _find_github_identity(
    db: AsyncSession, account: GitHubAccount
) -> UserIdentity | None:
    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.provider_user_id == account.provider_user_id,
        )
    )
    identity = result.scalar_one_or_none()
    if identity is not None:
        return identity

    # A legacy backfill uses a deterministic synthetic id.  It can be upgraded
    # only when the username is the same explicit legacy GitHub binding.  A
    # Telegram-only account is never merged based on an untrusted username.
    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            func.lower(UserIdentity.provider_username) == account.username.lower(),
        )
    )
    # A legacy backfill should be unique, but tolerate duplicate synthetic
    # rows left by an interrupted early migration and choose one deterministic
    # row rather than raising MultipleResultsFound during OAuth.
    identities = result.scalars().all()
    identity = next(
        (
            item
            for item in identities
            if str(item.provider_user_id).startswith("legacy:")
        ),
        None,
    )
    # Username matching is only a migration bridge.  Once an account has a
    # real provider id, a different id must never be allowed to claim it.
    if identity is not None and str(identity.provider_user_id).startswith("legacy:"):
        return identity
    return None


async def _find_user_by_explicit_github_username(
    db: AsyncSession, username: str
) -> TelegramUser | None:
    result = await db.execute(
        select(TelegramUser).where(
            func.lower(TelegramUser.github_username) == username.lower(),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # A legacy row without an identity can be safely upgraded on the first
    # OAuth login.  If a real provider identity already exists, however, a
    # different provider id with the same display name must not claim it.
    identity_result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.user_id == user.id,
        )
    )
    identities = identity_result.scalars().all()
    if any(
        not str(item.provider_user_id).startswith("legacy:") for item in identities
    ):
        return None
    return user


async def _upsert_email_endpoint(
    db: AsyncSession,
    user: TelegramUser,
    email: str | None,
    verified: bool,
    *,
    reactivate: bool = True,
) -> None:
    email = _normalized_email(email)
    if email is None:
        return

    result = await db.execute(
        select(NotificationEndpoint).where(
            NotificationEndpoint.provider == NotificationProvider.EMAIL.value,
            func.lower(NotificationEndpoint.address) == email,
        )
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is not None and endpoint.user_id != user.id:
        # Do not merge accounts on a shared/incorrect address.  Keep the old
        # owner's address and let an administrator resolve the conflict.
        logger.warning(
            "GitHub email endpoint conflict skipped: user_id={}, owner_user_id={}",
            user.id,
            endpoint.user_id,
        )
        return
    # Older installations may have the mirrored email column populated before
    # notification_endpoints existed.  Check that facade as well, otherwise a
    # case-insensitive unique constraint error could abort OAuth login.
    legacy_result = await db.execute(
        select(TelegramUser).where(
            func.lower(TelegramUser.email) == email,
        )
    )
    legacy_owner = legacy_result.scalar_one_or_none()
    if legacy_owner is not None and legacy_owner.id != user.id:
        logger.warning(
            "GitHub email mirror conflict skipped: user_id={}, owner_user_id={}",
            user.id,
            legacy_owner.id,
        )
        return
    if endpoint is None:
        endpoint = NotificationEndpoint(
            user_id=user.id,
            provider=NotificationProvider.EMAIL.value,
            address=email,
            verified=bool(verified),
            enabled=True,
        )
        db.add(endpoint)
    else:
        endpoint.verified = bool(endpoint.verified or verified)
        if reactivate:
            endpoint.enabled = True

    # A user has one active email destination.  Preserve old addresses for
    # audit/backup purposes, but disable them whenever OAuth rotates the
    # current primary email.
    other_results = await db.execute(
        select(NotificationEndpoint).where(
            NotificationEndpoint.user_id == user.id,
            NotificationEndpoint.provider == NotificationProvider.EMAIL.value,
            func.lower(NotificationEndpoint.address) != email,
        )
    )
    for old_endpoint in other_results.scalars().all():
        old_endpoint.enabled = False

    # Keep legacy mirror fields available for old UI and services.  The
    # endpoint table remains authoritative for notification delivery.
    user.email = email
    user.email_verified = bool(endpoint.verified)
    user.email_updated_at = now_utc()


async def upsert_github_account(
    db: AsyncSession,
    account: GitHubAccount,
    *,
    create_if_missing: bool = True,
) -> TelegramUser | None:
    """Find or create the internal user for a GitHub OAuth profile.

    Matching order is provider id, an explicit legacy GitHub identity, then a
    legacy user whose GitHub username was explicitly configured.  A Telegram-
    only user is never guessed or merged by username.
    """

    username = account.username.strip()
    if not username or not account.provider_user_id:
        raise ValueError("GitHub account requires provider id and username")

    identity = await _find_github_identity(db, account)
    if identity is not None:
        user = await db.get(TelegramUser, identity.user_id)
        if user is None or not user.is_active:
            return None
        if identity.provider_user_id != account.provider_user_id:
            # Upgrade only synthetic legacy identities.  Never overwrite a
            # real provider id, as that could hijack another account.
            if identity.provider_user_id.startswith("legacy:"):
                identity.provider_user_id = account.provider_user_id
            else:
                raise ValueError("GitHub provider identity conflict")
        identity.provider_username = username
        # Keep the legacy mirror useful to older pages while the identity row
        # remains authoritative for authentication matching.
        user.github_username = username
    else:
        user = await _find_user_by_explicit_github_username(db, username)
        # An administrator may deliberately disable a legacy username-only
        # account.  It must remain visible to the lookup so OAuth can reject
        # it before adding an identity, changing fields, or creating a user.
        if user is not None and not user.is_active:
            return None
        if user is None:
            # The explicit lookup intentionally returns None when the username
            # is already bound to a real provider identity.  Distinguish that
            # conflict from a missing user before attempting a new insert;
            # otherwise the legacy unique github_username constraint turns a
            # provider mismatch into an IntegrityError (or invites a future
            # caller to merge the accounts incorrectly).
            explicit_result = await db.execute(
                select(TelegramUser).where(
                    func.lower(TelegramUser.github_username) == username.lower()
                )
            )
            explicit_user = explicit_result.scalar_one_or_none()
            if explicit_user is not None:
                if not explicit_user.is_active:
                    return None
                # This is a controlled authentication rejection.  Keep the
                # existing account untouched and let callers surface their
                # normal "user disabled/unavailable" response; most
                # importantly, do not fall through to a duplicate user insert.
                return None
        if user is None and not create_if_missing:
            return None
        if user is None:
            user = TelegramUser(
                telegram_id=None,
                github_username=username,
                is_active=True,
                role="user",
                **registration_quota_values(),
            )
            if await legacy_telegram_id_required(db):
                # SQLite installations created before this feature cannot alter
                # a NOT NULL column in place.  Keep a non-null compatibility
                # sentinel in that legacy facade; the real identity is the
                # UserIdentity row and no Telegram endpoint is created for it.
                user.telegram_id = await _next_legacy_placeholder(db)
            db.add(user)
            await db.flush()
        else:
            # Existing explicit legacy username binding is safe to retain.
            user.github_username = username
        existing_identities = (
            await db.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == AuthProvider.GITHUB.value,
                    UserIdentity.user_id == user.id,
                )
            )
        ).scalars().all()
        legacy_identity = next(
            (
                item
                for item in existing_identities
                if str(item.provider_user_id).startswith("legacy:")
            ),
            None,
        )
        if legacy_identity is not None:
            # Upgrade the migration facade in place.  This avoids leaving a
            # synthetic row beside the real provider id and keeps future
            # scalar lookups unambiguous after an admin username rename.
            legacy_identity.provider_user_id = account.provider_user_id
            legacy_identity.provider_username = username
            identity = legacy_identity
        else:
            identity = UserIdentity(
                user_id=user.id,
                provider=AuthProvider.GITHUB.value,
                provider_user_id=account.provider_user_id,
                provider_username=username,
            )
            db.add(identity)

    await _upsert_email_endpoint(db, user, account.email, account.email_verified)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_id(db: AsyncSession, user_id: int) -> TelegramUser | None:
    """Resolve an internal user id for auth/notification callers."""

    return await db.get(TelegramUser, user_id)


async def list_notification_endpoints(
    db: AsyncSession,
    user_id: int | None = None,
    *,
    provider: str | None = None,
    enabled_only: bool = True,
) -> list[NotificationEndpoint]:
    query = select(NotificationEndpoint)
    if user_id is not None:
        query = query.where(NotificationEndpoint.user_id == user_id)
    if provider is not None:
        query = query.where(NotificationEndpoint.provider == provider)
    if enabled_only:
        query = query.where(NotificationEndpoint.enabled)
    result = await db.execute(query.order_by(NotificationEndpoint.id))
    return list(result.scalars().all())


async def bind_notification_endpoint(
    db: AsyncSession,
    user_id: int,
    provider: str,
    address: str,
    *,
    verified: bool = False,
    metadata: dict | None = None,
) -> NotificationEndpoint:
    """Create or update an endpoint owned by an internal user.

    A provider/address pair is globally unique; conflicts are rejected rather
    than silently moving an endpoint between users.
    """

    provider = str(provider).strip().lower()
    if provider not in {
        NotificationProvider.EMAIL.value,
        NotificationProvider.TELEGRAM.value,
        NotificationProvider.WEB.value,
    }:
        raise ValueError("unsupported notification provider")
    normalized = (
        str(address).strip().lower()
        if provider == NotificationProvider.EMAIL.value
        else str(address).strip()
    )
    if not normalized:
        raise ValueError("notification endpoint address cannot be empty")
    if provider == NotificationProvider.TELEGRAM.value:
        try:
            telegram_chat_id = int(normalized)
        except (TypeError, ValueError) as exc:
            raise ValueError("Telegram endpoint address must be a positive integer") from exc
        if telegram_chat_id <= 0:
            raise ValueError("Telegram endpoint address must be a positive integer")
        # Canonicalize equivalent values (for example ``00123``) so the
        # provider/address uniqueness constraint cannot be bypassed.
        normalized = str(telegram_chat_id)
    user = await db.get(TelegramUser, user_id)
    if user is None or not user.is_active:
        raise ValueError("internal user does not exist or is inactive")
    try:
        result = await db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == provider,
                NotificationEndpoint.address == normalized,
            )
        )
        endpoint = result.scalar_one_or_none()
        if endpoint is not None and endpoint.user_id != user_id:
            raise ValueError("notification endpoint is already bound to another user")
        if endpoint is None:
            endpoint = NotificationEndpoint(
                user_id=user_id,
                provider=provider,
                address=normalized,
                verified=verified,
                enabled=True,
                metadata_json=json.dumps(metadata, ensure_ascii=False)
                if metadata
                else None,
            )
            db.add(endpoint)
        else:
            endpoint.enabled = True
            endpoint.verified = bool(endpoint.verified or verified)
            if metadata is not None:
                endpoint.metadata_json = json.dumps(metadata, ensure_ascii=False)
        if provider == NotificationProvider.TELEGRAM.value:
            # Delivery rows are unique by (user, channel), so keep exactly
            # one active Telegram endpoint for a user.  Disable old endpoints
            # deterministically instead of allowing the worker to pick an
            # arbitrary address.  Do not rewrite a populated legacy
            # telegram_id: child tables still reference that key.
            old_results = await db.execute(
                select(NotificationEndpoint).where(
                    NotificationEndpoint.user_id == user_id,
                    NotificationEndpoint.provider == provider,
                    NotificationEndpoint.id != endpoint.id,
                    NotificationEndpoint.enabled.is_(True),
                )
            )
            for old_endpoint in old_results.scalars().all():
                old_endpoint.enabled = False
            if user.telegram_id is None:
                user.telegram_id = telegram_chat_id
        if provider == NotificationProvider.EMAIL.value:
            user.email = normalized
            user.email_verified = bool(endpoint.verified)
            user.email_updated_at = now_utc()
        await db.commit()
        await db.refresh(endpoint)
        return endpoint
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError("notification endpoint is already bound to another user") from exc


async def unbind_notification_endpoint(
    db: AsyncSession,
    user_id: int,
    endpoint_id: int,
    *,
    provider: str | None = None,
) -> bool:
    endpoint = await db.get(NotificationEndpoint, endpoint_id)
    if (
        endpoint is None
        or endpoint.user_id != user_id
        or (provider is not None and endpoint.provider != provider)
    ):
        return False
    endpoint.enabled = False
    await db.commit()
    return True


async def legacy_telegram_id_required(db: AsyncSession) -> bool:
    """Detect old SQLite/MySQL schemas whose legacy column is still NOT NULL."""

    try:
        def _required(sync_session) -> bool:
            bind = sync_session.get_bind()
            if bind is None:
                return False
            columns = inspect(bind).get_columns("telegram_users")
            telegram_column = next(
                (column for column in columns if column["name"] == "telegram_id"),
                None,
            )
            return bool(telegram_column and not telegram_column.get("nullable", True))

        return await db.run_sync(_required)
    except (AttributeError, KeyError, TypeError):
        return False


async def _next_legacy_placeholder(db: AsyncSession) -> int:
    """Return a unique compatibility value for a pre-v2 NOT NULL column."""
    result = await db.execute(select(TelegramUser.telegram_id))
    used = {value for (value,) in result.all() if value is not None}
    if 0 not in used:
        return 0
    candidate = -1
    while candidate in used:
        candidate -= 1
    return candidate


async def migrate_legacy_identity_data(db: AsyncSession | None = None) -> dict[str, int]:
    """Idempotently backfill legacy usernames/Telegram ids into new tables.

    Conflicting endpoints are left untouched and counted, while the original
    ``telegram_users`` rows and all legacy foreign keys remain unchanged.
    """

    owns_session = db is None
    if db is None:
        from backend.models.database import async_session

        db = async_session()
    created_identities = created_endpoints = conflicts = 0
    try:
        users = list((await db.execute(select(TelegramUser).order_by(TelegramUser.id))).scalars().all())
        for user in users:
            if user.github_username:
                result = await db.execute(
                    select(UserIdentity).where(
                        UserIdentity.provider == AuthProvider.GITHUB.value,
                        UserIdentity.user_id == user.id,
                    )
                )
                identity = result.scalar_one_or_none()
                if identity is None:
                    synthetic_id = _legacy_provider_id(user.github_username)
                    conflict_result = await db.execute(
                        select(UserIdentity).where(
                            UserIdentity.provider == AuthProvider.GITHUB.value,
                            UserIdentity.provider_user_id == synthetic_id,
                        )
                    )
                    conflict_identity = conflict_result.scalar_one_or_none()
                    if (
                        conflict_identity is not None
                        and conflict_identity.user_id != user.id
                    ):
                        conflicts += 1
                    else:
                        db.add(
                            UserIdentity(
                                user_id=user.id,
                                provider=AuthProvider.GITHUB.value,
                                provider_user_id=synthetic_id,
                                provider_username=user.github_username,
                            )
                        )
                        created_identities += 1

            # ``0`` is the reserved compatibility placeholder used only when
            # an old SQLite table still enforces NOT NULL for GitHub-only rows.
            # Telegram chat ids are always positive.  Non-positive values are
            # compatibility sentinels used by pre-migration NOT NULL SQLite
            # schemas and must never become notification destinations.
            if user.telegram_id is not None and user.telegram_id > 0:
                address = str(user.telegram_id)
                result = await db.execute(
                    select(NotificationEndpoint).where(
                        NotificationEndpoint.provider == NotificationProvider.TELEGRAM.value,
                        NotificationEndpoint.address == address,
                    )
                )
                endpoint = result.scalar_one_or_none()
                if endpoint is None:
                    db.add(
                        NotificationEndpoint(
                            user_id=user.id,
                            provider=NotificationProvider.TELEGRAM.value,
                            address=address,
                            verified=True,
                            enabled=True,
                        )
                    )
                    created_endpoints += 1
                elif endpoint.user_id != user.id:
                    conflicts += 1

            if user.email:
                email = _normalized_email(user.email)
                email_result = await db.execute(
                    select(NotificationEndpoint).where(
                        NotificationEndpoint.provider == NotificationProvider.EMAIL.value,
                        func.lower(NotificationEndpoint.address) == email,
                    )
                )
                email_endpoint = email_result.scalar_one_or_none()
                if email_endpoint is not None and email_endpoint.user_id != user.id:
                    conflicts += 1
                else:
                    await _upsert_email_endpoint(
                        db,
                        user,
                        user.email,
                        bool(user.email_verified),
                        reactivate=False,
                    )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        if owns_session:
            await db.close()
    return {
        "identities_created": created_identities,
        "endpoints_created": created_endpoints,
        "conflicts": conflicts,
    }
