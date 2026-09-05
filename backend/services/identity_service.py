"""Internal user, identity, and notification endpoint services.

This module is the compatibility boundary for the old ``telegram_users``
model.  New authentication and notification code should use the functions in
this module instead of matching Telegram ids or GitHub usernames directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from inspect import isawaitable

from loguru import logger
from sqlalchemy import delete, func, inspect, select
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


class GitHubUsernameConflictError(ValueError):
    """Raised before a username rename would leave identities ambiguous."""


_LEGACY_PLACEHOLDER_MAX_ATTEMPTS = 8


def _is_sqlite_telegram_id_unique_conflict(exc: IntegrityError) -> bool:
    """Return whether ``exc`` is the legacy Telegram-id unique conflict.

    SQLite exposes unique violations through one generic exception type.  Do
    not retry based only on that type (or on SQLite's numeric error code): the
    same insert can also fail because ``github_username``/``email`` is
    duplicated.  The column-qualified SQLite diagnostic is the narrow
    discriminator that lets the caller retry only a compatibility sentinel.
    """

    message = str(getattr(exc, "orig", exc)).casefold()
    return "unique constraint failed: telegram_users.telegram_id" in message


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


def _is_legacy_github_identity(identity: UserIdentity) -> bool:
    """Return whether an identity is a synthetic legacy GitHub binding."""

    return (
        str(getattr(identity, "provider", "")).casefold()
        == AuthProvider.GITHUB.value
        and str(getattr(identity, "provider_user_id", "")).casefold().startswith(
            "legacy:"
        )
    )


def _legacy_identity_matches_username(
    identity: UserIdentity, username: str
) -> bool:
    """Validate both fields of a synthetic identity against the user mirror.

    ``provider_username`` alone is not an identity.  Requiring the synthetic
    id and username to agree with the current ``telegram_users`` mirror keeps
    an abandoned pre-migration username from claiming an administratively
    renamed account.
    """

    if not _is_legacy_github_identity(identity):
        return False
    normalized = username.strip().casefold()
    provider_id = str(getattr(identity, "provider_user_id", "")).casefold()
    provider_username = str(
        getattr(identity, "provider_username", "") or ""
    ).strip().casefold()
    return provider_id == f"legacy:{normalized}" and provider_username == normalized


async def rename_github_username(
    db: AsyncSession,
    user: TelegramUser,
    new_username: str,
) -> TelegramUser:
    """Rename a legacy user and retire matching synthetic GitHub aliases.

    This is deliberately a pre-commit helper used by both admin surfaces.
    Every conflict is checked before changing ``user`` or deleting duplicate
    synthetic rows.  Real provider identities are never rewritten or deleted;
    GitHub OAuth remains authoritative for those rows.
    """

    if not isinstance(new_username, str):
        raise GitHubUsernameConflictError("GitHub 用户名无效")
    new_username = new_username.strip()
    if not new_username or len(new_username) > 100:
        raise GitHubUsernameConflictError("GitHub 用户名无效")

    old_username = str(getattr(user, "github_username", "") or "").strip()
    old_key = old_username.casefold()
    new_key = new_username.casefold()

    # Check the legacy mirror case-insensitively even on databases whose
    # collation treats GitHub usernames as case-sensitive.
    result = await db.execute(
        select(TelegramUser).where(
            TelegramUser.id != user.id,
            func.lower(TelegramUser.github_username) == new_key,
        )
    )
    if result.scalars().first() is not None:
        raise GitHubUsernameConflictError(
            f"GitHub 用户名 {new_username} 已被其他用户使用"
        )

    identity_result = await db.execute(
        select(UserIdentity)
        .where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.user_id == user.id,
        )
        .order_by(UserIdentity.id)
    )
    identities = list(identity_result.scalars().all())

    # A stale real provider row can retain a username that now belongs to a
    # different GitHub account.  Do not let an admin mirror rename create an
    # ambiguous username binding; importantly, this check never rewrites the
    # other user's real identity.
    provider_name_result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.user_id != user.id,
            func.lower(UserIdentity.provider_username) == new_key,
        )
    )
    if provider_name_result.scalars().first() is not None:
        raise GitHubUsernameConflictError(
            f"GitHub 用户名 {new_username} 已绑定到其他身份"
        )

    provider_alias_result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.user_id != user.id,
            func.lower(UserIdentity.provider_user_id)
            == _legacy_provider_id(new_username),
        )
    )
    if provider_alias_result.scalars().first() is not None:
        raise GitHubUsernameConflictError(
            f"GitHub 用户名 {new_username} 已绑定到其他身份"
        )

    # A synthetic alias must not already be owned by another internal user.
    # Check both the deterministic id and the display-name field because old
    # interrupted migrations can have one field populated without the other.
    alias_conflict_query = select(UserIdentity).where(
        UserIdentity.provider == AuthProvider.GITHUB.value,
        UserIdentity.user_id != user.id,
    )
    if old_key:
        alias_conflict_query = alias_conflict_query.where(
            func.lower(UserIdentity.provider_username).in_({old_key, new_key})
            | func.lower(UserIdentity.provider_user_id).in_(
                {_legacy_provider_id(old_username), _legacy_provider_id(new_username)}
            )
        )
    else:
        alias_conflict_query = alias_conflict_query.where(
            (func.lower(UserIdentity.provider_username) == new_key)
            | (
                func.lower(UserIdentity.provider_user_id)
                == _legacy_provider_id(new_username)
            )
        )
    conflict_result = await db.execute(alias_conflict_query)
    conflicting_aliases = [
        identity
        for identity in conflict_result.scalars().all()
        if _is_legacy_github_identity(identity)
        and (
            str(getattr(identity, "provider_username", "") or "")
            .strip()
            .casefold()
            in {old_key, new_key}
            or str(getattr(identity, "provider_user_id", "")).casefold()
            in {
                _legacy_provider_id(old_username).casefold(),
                _legacy_provider_id(new_username).casefold(),
            }
        )
    ]
    if conflicting_aliases:
        raise GitHubUsernameConflictError(
            f"GitHub 用户名 {new_username} 已绑定到其他身份"
        )

    # Only synthetic rows matching the old mirror (plus an already-created new
    # canonical row) are candidates.  Real provider identities are intentionally
    # excluded, even when their provider_username happens to match the rename.
    synthetic_rows = [
        identity
        for identity in identities
        if _is_legacy_github_identity(identity)
        and (
            _legacy_identity_matches_username(identity, old_username)
            or _legacy_identity_matches_username(identity, new_username)
            or str(getattr(identity, "provider_username", "") or "")
            .strip()
            .casefold()
            == old_key
        )
    ]
    if synthetic_rows:
        canonical = next(
            (
                identity
                for identity in synthetic_rows
                if _legacy_identity_matches_username(identity, new_username)
            ),
            synthetic_rows[0],
        )
        duplicates = [identity for identity in synthetic_rows if identity is not canonical]
        duplicate_ids = [identity.id for identity in duplicates if identity.id is not None]
        if duplicate_ids:
            # Remove only duplicate synthetic rows.  In particular, an imported
            # real GitHub identity must remain untouched for audit/auth safety.
            await db.execute(
                delete(UserIdentity).where(UserIdentity.id.in_(duplicate_ids))
            )
            await db.flush()
        canonical.provider_user_id = _legacy_provider_id(new_username)
        canonical.provider_username = new_username

    user.github_username = new_username
    return user


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
    # Username matching is only a migration bridge.  Require the synthetic id,
    # synthetic username, and current legacy mirror to agree; an old alias
    # retained after an admin rename must never be able to claim that account.
    for candidate in identities:
        if not _is_legacy_github_identity(candidate):
            continue
        owner = await db.get(TelegramUser, candidate.user_id)
        if owner is not None and _legacy_identity_matches_username(
            candidate, account.username
        ) and owner.github_username is not None:
            if owner.github_username.strip().casefold() == account.username.casefold():
                return candidate
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
    if any(not _is_legacy_github_identity(item) for item in identities):
        return None
    if any(
        not _legacy_identity_matches_username(item, user.github_username or username)
        for item in identities
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
    incoming_verified = bool(verified)
    if endpoint is None:
        endpoint = NotificationEndpoint(
            user_id=user.id,
            provider=NotificationProvider.EMAIL.value,
            address=email,
            verified=incoming_verified,
            # GitHub profile/email data is not a notification authorization
            # until GitHub explicitly marks the address verified.
            enabled=incoming_verified,
        )
        db.add(endpoint)
    else:
        was_verified = bool(endpoint.verified)
        endpoint.verified = bool(was_verified or incoming_verified)
        if incoming_verified and reactivate:
            endpoint.enabled = True
        elif not was_verified:
            # Correct rows created by older versions that enabled an unverified
            # OAuth address, while preserving an already-verified endpoint's
            # active state when a later GitHub response is inconclusive.
            endpoint.enabled = False

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
    # An unverified replacement must never turn off a previously verified,
    # active address.  Once the new address is verified it becomes primary and
    # older addresses are retired as before.
    if incoming_verified:
        for old_endpoint in other_results.scalars().all():
            old_endpoint.enabled = False

    # Keep legacy mirror fields available for old UI and services.  The
    # endpoint table remains authoritative for notification delivery.
    user.email = email
    user.email_verified = bool(endpoint.verified)
    user.email_updated_at = now_utc()


async def _disable_unverified_email_endpoints(
    db: AsyncSession, user_id: int
) -> None:
    """Repair legacy rows that enabled an email before verification existed."""

    result = await db.execute(
        select(NotificationEndpoint).where(
            NotificationEndpoint.user_id == user_id,
            NotificationEndpoint.provider == NotificationProvider.EMAIL.value,
            NotificationEndpoint.verified.is_(False),
            NotificationEndpoint.enabled.is_(True),
        )
    )
    for endpoint in result.scalars().all():
        endpoint.enabled = False


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
            quotas = registration_quota_values()
            # GitHub-only OAuth registrations use the same compatibility
            # boundary as administrator/setup/backup creation.  The factory
            # is replayed for each savepoint retry, so a failed sentinel
            # candidate never leaks a half-persistent ORM object.
            user = await create_user_and_flush(
                db,
                lambda resolved_telegram_id: TelegramUser(
                    telegram_id=resolved_telegram_id,
                    github_username=username,
                    is_active=True,
                    role="user",
                    **quotas,
                ),
                telegram_id=None,
            )
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
                if _legacy_identity_matches_username(item, username)
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

    # Even when GitHub omits the email scope/value, repair any legacy enabled
    # unverified rows before the next announcement query can use them.
    await _disable_unverified_email_endpoints(db, user.id)
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
    """Detect an old physical SQLite column that still requires a value.

    MySQL/PostgreSQL installations are made nullable by the startup schema
    migration.  A sentinel is therefore never a portable fallback value and
    must not be introduced on those dialects (or on the current SQLite
    schema).
    """

    try:
        def _required(sync_session) -> bool:
            bind = sync_session.get_bind()
            if bind is None:
                return False
            if getattr(getattr(bind, "dialect", None), "name", None) != "sqlite":
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


async def _flush_session(db: AsyncSession) -> None:
    """Flush both real async sessions and the small sync-backed test facades."""

    result = db.flush()
    if isawaitable(result):
        await result


async def create_user_and_flush(
    db: AsyncSession,
    user_factory: Callable[[int | None], TelegramUser],
    *,
    telegram_id: int | None = None,
    max_attempts: int = _LEGACY_PLACEHOLDER_MAX_ATTEMPTS,
) -> TelegramUser:
    """Create a legacy-compatible user and flush it into the current transaction.

    ``telegram_id`` is passed to ``user_factory`` unchanged on current schemas
    and whenever the caller supplied an explicit value.  Only an old physical
    SQLite ``telegram_users.telegram_id NOT NULL`` column receives a synthetic
    non-positive value.  Each sentinel attempt creates a fresh object inside a
    savepoint, so a unique conflict can be rolled back and retried without
    invalidating unrelated pending work in the outer transaction.

    The savepoint is entered only after pending caller changes have been
    flushed.  SQLAlchemy flushes pending state while entering
    ``begin_nested()``, before the savepoint exists; flushing first is what
    guarantees that the candidate INSERT and its failure are actually scoped
    by the savepoint.  A few legacy maintenance/test facades do not expose
    savepoints; they still use the same factory and strict error classifier,
    while production ``AsyncSession`` always takes the savepoint path.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    legacy_required = telegram_id is None and await legacy_telegram_id_required(db)
    if not legacy_required:
        user = user_factory(telegram_id)
        db.add(user)
        await _flush_session(db)
        return user

    # Make sure begin_nested() has no unrelated pending objects to flush before
    # its savepoint is established.  If this flush fails, propagate that exact
    # error instead of treating it as a sentinel collision.
    await _flush_session(db)
    begin_nested = getattr(db, "begin_nested", None)

    for attempt in range(max_attempts):
        candidate = await _next_legacy_placeholder(db)
        if begin_nested is None:
            # Compatibility with old synchronous test/maintenance facades.  A
            # real AsyncSession always provides begin_nested; keeping this
            # fallback avoids making legacy callers implement an otherwise
            # unused transaction adapter.
            user = user_factory(candidate)
            db.add(user)
            try:
                await _flush_session(db)
            except IntegrityError as exc:
                if not _is_sqlite_telegram_id_unique_conflict(exc):
                    raise
                if attempt + 1 >= max_attempts:
                    raise
                rollback = db.rollback()
                if isawaitable(rollback):
                    await rollback
                continue
            return user

        nested = begin_nested()
        if isawaitable(nested):
            nested = await nested
        try:
            # The object is deliberately instantiated after entering the
            # savepoint.  See the method docstring about begin_nested's entry
            # flush behavior.
            async with nested:
                user = user_factory(candidate)
                db.add(user)
                await _flush_session(db)
        except IntegrityError as exc:
            if not _is_sqlite_telegram_id_unique_conflict(exc):
                raise
            if attempt + 1 >= max_attempts:
                raise
            continue
        return user

    # The loop always returns or raises.  Keep a defensive error for static
    # analyzers if max_attempts is changed in the future.
    raise RuntimeError("legacy Telegram placeholder allocation exhausted")


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
                # ``user_id`` is intentionally not unique per provider: a
                # user may retain more than one valid GitHub identity (for
                # example, after importing a backup).  This is an existence
                # check, not a scalar lookup; ``scalar_one_or_none`` would
                # abort startup with MultipleResultsFound for that valid
                # state.
                existing_identities = result.scalars().all()
                identity = existing_identities[0] if existing_identities else None
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
                    # A restored user may deliberately retain the legacy
                    # ``telegram_id`` mirror while using a different,
                    # canonical Telegram endpoint.  Do not recreate the old
                    # mirror on every startup: that would make the stale
                    # address active again (and can duplicate deliveries).
                    other_result = await db.execute(
                        select(NotificationEndpoint).where(
                            NotificationEndpoint.provider
                            == NotificationProvider.TELEGRAM.value,
                            NotificationEndpoint.user_id == user.id,
                        )
                    )
                    other_endpoints = other_result.scalars().all()
                    if not other_endpoints:
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
