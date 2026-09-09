"""Internal user, identity, and notification endpoint services.

This module is the compatibility boundary for the old ``telegram_users``
model.  New authentication and notification code should use the functions in
this module instead of matching Telegram ids or GitHub usernames directly.
"""

from __future__ import annotations

import json
from collections import defaultdict
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


class LegacyIdentityAmbiguityError(ValueError):
    """A legacy username bridge has more than one possible account owner.

    Legacy GitHub usernames are only a migration compatibility hint.  Once
    more than one case-insensitive mirror/alias can claim that hint, choosing
    a row by query order would be an account takeover primitive.  Callers use
    this dedicated exception to fail closed without exposing candidate users.
    """


class NotificationEndpointConflictError(ValueError):
    """A notification address is already owned by another internal user."""


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
    return f"legacy:{username.strip().casefold()}"


def _is_legacy_github_identity(identity: UserIdentity) -> bool:
    """Return whether an identity is derived from a GitHub username.

    ``legacy:`` rows come from the data migration, while ``login:`` rows are
    the deterministic fallback used when a provider payload omitted GitHub's
    numeric id.  Neither is a stable provider identity and both must use the
    same ambiguity guard.
    """

    return (
        str(getattr(identity, "provider", "")).casefold()
        == AuthProvider.GITHUB.value
        and str(getattr(identity, "provider_user_id", "")).casefold().startswith(
            ("legacy:", "login:")
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
    return provider_id in {
        f"legacy:{normalized}",
        f"login:{normalized}",
    } and provider_username == normalized


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
    exact_identities = list(result.scalars().all())
    exact_stable_identities = [
        identity
        for identity in exact_identities
        if not _is_legacy_github_identity(identity)
    ]
    if exact_stable_identities:
        # The provider id is authoritative.  A healthy database has one row
        # because of uq_user_identity_provider_id; if an old/manual database
        # contains duplicate rows, only accept them when all rows point at the
        # same internal user.  Never pick a winner between different users.
        exact_owner_ids = {
            identity.user_id for identity in exact_stable_identities
        }
        if len(exact_owner_ids) > 1:
            raise LegacyIdentityAmbiguityError(
                "GitHub 账号存在冲突，请联系管理员处理"
            )
        return min(
            exact_stable_identities,
            key=lambda identity: (identity.id is None, identity.id or 0),
        )

    # A legacy backfill uses a deterministic synthetic id.  It can be upgraded
    # only when the username is the same explicit legacy GitHub binding.  A
    # Telegram-only account is never merged based on an untrusted username.
    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            func.lower(UserIdentity.provider_username)
            == account.username.strip().casefold(),
        )
    )
    identities = list(result.scalars().all())
    # Username matching is only a migration bridge.  Require the synthetic id,
    # synthetic username, and current legacy mirror to agree; an old alias
    # retained after an admin rename must never be able to claim that account.
    # Aggregate all possible owners before selecting a row: ``first()`` here
    # would make Alice/alice login depend on database ordering.
    valid_candidates: list[UserIdentity] = []
    owner_ids: set[int] = set()
    # The mirror is part of the legacy bridge even when only one synthetic
    # alias row survived an interrupted migration.  Load every case-folded
    # mirror owner before deciding whether the bridge is unambiguous; otherwise
    # Alice/alice can still be resolved by whichever owner happened to retain
    # the alias row.
    mirror_result = await db.execute(select(TelegramUser))
    mirror_users = [
        user
        for user in mirror_result.scalars().all()
        if str(getattr(user, "github_username", "") or "").strip().casefold()
        == account.username.strip().casefold()
    ]
    owner_ids.update(int(user.id) for user in mirror_users if user.id is not None)
    for candidate in identities:
        if not _is_legacy_github_identity(candidate):
            continue
        owner = await db.get(TelegramUser, candidate.user_id)
        if (
            owner is not None
            and _legacy_identity_matches_username(candidate, account.username)
            and owner.github_username is not None
            and owner.github_username.strip().casefold()
            == account.username.strip().casefold()
        ):
            valid_candidates.append(candidate)
            owner_ids.add(int(owner.id))
    if len(owner_ids) > 1:
        raise LegacyIdentityAmbiguityError(
            "GitHub 账号存在冲突，请联系管理员处理"
        )
    if valid_candidates:
        return min(
            valid_candidates,
            key=lambda identity: (identity.id is None, identity.id or 0),
        )
    return None


async def _find_user_by_explicit_github_username(
    db: AsyncSession, username: str
) -> TelegramUser | None:
    # Python ``casefold`` is the canonical comparison.  Loading the mirror
    # rows here avoids relying on a backend's locale-specific ``lower`` (which
    # does not cover every Unicode case-fold pair).
    result = await db.execute(select(TelegramUser))
    users = list(result.scalars().all())
    normalized_username = username.strip().casefold()
    users = [
        user
        for user in users
        if str(getattr(user, "github_username", "") or "").strip().casefold()
        == normalized_username
    ]
    if len({user.id for user in users}) > 1:
        raise LegacyIdentityAmbiguityError(
            "GitHub 账号存在冲突，请联系管理员处理"
        )
    user = users[0] if users else None
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
    endpoints = list(result.scalars().all())
    endpoint_owner_ids = {endpoint.user_id for endpoint in endpoints}
    if any(owner_id != user.id for owner_id in endpoint_owner_ids):
        # Do not merge accounts on a shared/incorrect address.  Keep the old
        # owner's address and let an administrator resolve the conflict.
        logger.warning(
            "GitHub email endpoint conflict skipped: user_id={}, owner_user_ids={}",
            user.id,
            sorted(endpoint_owner_ids),
        )
        return
    endpoint = min(
        endpoints,
        key=lambda item: (item.id is None, item.id or 0),
        default=None,
    )
    # Older installations may have the mirrored email column populated before
    # notification_endpoints existed.  Check that facade as well, otherwise a
    # case-insensitive unique constraint error could abort OAuth login.
    legacy_result = await db.execute(
        select(TelegramUser).where(
            func.lower(TelegramUser.email) == email,
        )
    )
    legacy_owners = list(legacy_result.scalars().all())
    if any(owner.id != user.id for owner in legacy_owners):
        logger.warning(
            "GitHub email mirror conflict skipped: user_id={}, owner_user_ids={}",
            user.id,
            sorted({owner.id for owner in legacy_owners}),
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
        # An endpoint that was already verified and explicitly disabled is a
        # user opt-out.  A later OAuth login with the same verified address
        # must not silently reactivate it.  Only a new endpoint, or an
        # unverified endpoint transitioning to verified, may be enabled.
        if incoming_verified and reactivate and not was_verified:
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


def _is_github_provider_identity_conflict(exc: IntegrityError) -> bool:
    """Identify only the GitHub provider-id uniqueness race.

    OAuth account creation can legitimately race with another first login.  A
    commit ``IntegrityError`` must not otherwise be swallowed: username,
    email, foreign-key, and unrelated constraint failures still belong to the
    caller.  PostgreSQL exposes the constraint name, while SQLite/MySQL
    expose a column/table diagnostic, so support both narrow forms.
    """

    original = getattr(exc, "orig", exc)
    constraint_name = getattr(
        getattr(original, "diag", None), "constraint_name", None
    )
    if constraint_name == "uq_user_identity_provider_id":
        return True
    message = str(original).casefold()
    if "uq_user_identity_provider_id" in message:
        return True
    return (
        "user_identities" in message
        and "provider" in message
        and "provider_user_id" in message
        and ("unique" in message or "duplicate" in message)
    )


def _is_github_username_unique_conflict(exc: IntegrityError) -> bool:
    """Identify only the legacy mirror username uniqueness race.

    A first OAuth login inserts the compatibility ``telegram_users`` row
    before its provider identity is committed.  Another request for the same
    account can therefore lose at the username flush rather than at the
    identity commit.  Keep this classifier deliberately narrower than a
    generic ``IntegrityError`` handler: provider-id, email, foreign-key, and
    unrelated username-like columns must still propagate to the caller.
    """

    original = getattr(exc, "orig", exc)
    constraint_name = str(
        getattr(getattr(original, "diag", None), "constraint_name", "") or ""
    ).casefold()
    if (
        "telegram" in constraint_name
        and "github_username" in constraint_name
    ):
        return True
    message = str(original).casefold()
    return (
        "telegram_users" in message
        and "github_username" in message
        and ("unique" in message or "duplicate" in message)
    )


async def _assert_legacy_github_bridge_unambiguous(
    db: AsyncSession,
    user_id: int,
    username: str,
) -> None:
    """Fail closed if a username-derived bridge has another mirror owner.

    ``_find_github_identity`` performs the initial lookup, but a second
    application writer can add a case-insensitive mirror before this request
    commits.  Check again immediately before mutating/upgrading the
    synthetic identity and immediately before commit.  The database's legacy
    exact-value unique constraint remains a final safeguard for a race after
    this query; direct out-of-transaction writes are intentionally outside
    this application-level guarantee.
    """

    normalized_username = username.strip().casefold()
    result = await db.execute(select(TelegramUser))
    conflicting_mirrors = [
        mirror
        for mirror in result.scalars().all()
        if mirror.id != user_id
        and str(getattr(mirror, "github_username", "") or "")
        .strip()
        .casefold()
        == normalized_username
    ]
    if conflicting_mirrors:
        # The caller may have staged a synthetic identity/email endpoint by the
        # time the commit-boundary guard runs.  Roll it all back before
        # exposing the controlled ambiguity error so no provider id or token
        # can be issued from a dirty transaction.
        await db.rollback()
        raise LegacyIdentityAmbiguityError(
            "GitHub 账号存在冲突，请联系管理员处理"
        )


async def _reload_github_identity_winner(
    db: AsyncSession, provider_user_id: str
) -> TelegramUser | None:
    """Reload the exact provider-id winner after a concurrent commit race."""

    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == AuthProvider.GITHUB.value,
            UserIdentity.provider_user_id == provider_user_id,
        )
    )
    identities = list(result.scalars().all())
    owner_ids = {identity.user_id for identity in identities}
    if len(owner_ids) != 1:
        # A dirty database with multiple owners is ambiguous just like a
        # legacy alias collision.  Do not guess a winner after rollback.
        return None
    user = await db.get(TelegramUser, next(iter(owner_ids)))
    if user is None or not user.is_active:
        return None
    return user


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
    legacy_bridge_identity = False
    if identity is not None:
        user = await db.get(TelegramUser, identity.user_id)
        if user is None or not user.is_active:
            return None
        legacy_bridge_identity = _is_legacy_github_identity(identity)
        if legacy_bridge_identity:
            # Re-check after the lookup and before changing the synthetic row.
            # A second case-insensitive mirror may have been inserted while
            # the OAuth request was exchanging the provider token.
            await _assert_legacy_github_bridge_unambiguous(
                db, user.id, username
            )
        if identity.provider_user_id != account.provider_user_id:
            # Upgrade only synthetic legacy identities.  Never overwrite a
            # real provider id, as that could hijack another account.
            if legacy_bridge_identity:
                identity.provider_user_id = account.provider_user_id
            else:
                raise ValueError("GitHub provider identity conflict")
        identity.provider_username = username
        # Keep the legacy mirror useful to older pages while the identity row
        # remains authoritative for authentication matching.  A dirty legacy
        # database may already contain another case-insensitive mirror for the
        # returned GitHub username; exact provider-id authentication remains
        # valid in that case, but must not trigger a mirror unique-conflict or
        # silently choose which legacy account to rename.
        mirror_result = await db.execute(select(TelegramUser))
        conflicting_mirrors = [
            mirror
            for mirror in mirror_result.scalars().all()
            if mirror.id != user.id
            and str(getattr(mirror, "github_username", "") or "")
            .strip()
            .casefold()
            == username.casefold()
        ]
        if conflicting_mirrors:
            logger.warning(
                "Preserving a conflicting legacy GitHub mirror for exact provider login: "
                "user_id={}, conflicting_user_ids={}",
                user.id,
                [mirror.id for mirror in conflicting_mirrors],
            )
        else:
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
                    func.lower(TelegramUser.github_username)
                    == username.casefold()
                )
            )
            explicit_users = [
                candidate
                for candidate in explicit_result.scalars().all()
                if str(getattr(candidate, "github_username", "") or "")
                .strip()
                .casefold()
                == username.casefold()
            ]
            if len({candidate.id for candidate in explicit_users}) > 1:
                raise LegacyIdentityAmbiguityError(
                    "GitHub 账号存在冲突，请联系管理员处理"
                )
            explicit_user = explicit_users[0] if explicit_users else None
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
            # New compatibility mirrors use the provider's canonical
            # case-folded spelling.  Existing rows keep their display casing
            # for backup/UI compatibility, while the exact legacy unique key
            # then also becomes an atomic cross-dialect guard for concurrent
            # ``Alice``/``alice`` first logins.
            canonical_username = username.casefold()
            # GitHub-only OAuth registrations use the same compatibility
            # boundary as administrator/setup/backup creation.  The factory
            # is replayed for each savepoint retry, so a failed sentinel
            # candidate never leaks a half-persistent ORM object.
            try:
                user = await create_user_and_flush(
                    db,
                    lambda resolved_telegram_id: TelegramUser(
                        telegram_id=resolved_telegram_id,
                        github_username=canonical_username,
                        is_active=True,
                        role="user",
                        **quotas,
                    ),
                    telegram_id=None,
                )
            except IntegrityError as exc:
                # On current schemas the compatibility user is flushed before
                # its UserIdentity.  Concurrent first logins for the same
                # provider can therefore collide on the legacy username
                # column before the existing commit-race recovery runs.
                # Roll back the failed flush, then accept only the exact
                # provider-id winner; never guess by username.
                await db.rollback()
                if not _is_github_username_unique_conflict(exc):
                    raise
                winner = await _reload_github_identity_winner(
                    db, account.provider_user_id
                )
                if winner is None:
                    raise
                await db.refresh(winner)
                return winner
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
            legacy_bridge_identity = True
            await _assert_legacy_github_bridge_unambiguous(
                db, user.id, username
            )
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
    if legacy_bridge_identity:
        # Commit-boundary guard: the initial guard above cannot observe a
        # mirror committed by another application writer after the lookup.
        # If one is now visible, rollback all staged identity/email changes and
        # fail closed rather than issuing a token for an ambiguous alias.
        await _assert_legacy_github_bridge_unambiguous(db, user.id, username)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not _is_github_provider_identity_conflict(exc):
            raise
        winner = await _reload_github_identity_winner(
            db, account.provider_user_id
        )
        if winner is None:
            # The provider-id conflict classifier was intentionally narrow;
            # if no exact active winner can be reloaded, preserve the original
            # failure rather than guessing by username.
            raise
        await db.refresh(winner)
        return winner
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


def _normalise_notification_address(provider: str, address: str) -> tuple[str, int | None]:
    """Validate/canonicalize an endpoint without touching the database."""

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
    telegram_chat_id: int | None = None
    if provider == NotificationProvider.TELEGRAM.value:
        try:
            telegram_chat_id = int(normalized)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Telegram endpoint address must be a positive integer"
            ) from exc
        if telegram_chat_id <= 0:
            raise ValueError("Telegram endpoint address must be a positive integer")
        # Canonicalize equivalent values (for example ``00123``) so the
        # provider/address uniqueness constraint cannot be bypassed.
        normalized = str(telegram_chat_id)
    return normalized, telegram_chat_id


async def stage_notification_endpoint(
    db: AsyncSession,
    user_id: int,
    provider: str,
    address: str,
    *,
    verified: bool = False,
    metadata: dict | None = None,
    allow_inactive_user: bool = False,
) -> NotificationEndpoint:
    """Stage an endpoint mutation in the caller's transaction.

    A provider/address pair is globally unique; conflicts are rejected rather
    than silently moving an endpoint between users.  This helper deliberately
    does not commit or refresh, allowing admin/API/setup flows to atomically
    create a user and its authoritative Telegram endpoint.  Inactive targets
    are rejected by default (binding flows fail closed); authoritative admin
    maintenance passes ``allow_inactive_user=True`` so the endpoint keeps
    tracking the mirror across disable/enable cycles.
    """

    provider = str(provider).strip().lower()
    normalized, telegram_chat_id = _normalise_notification_address(
        provider, address
    )
    user = await db.get(TelegramUser, user_id)
    if user is None or (not user.is_active and not allow_inactive_user):
        raise ValueError("internal user does not exist or is inactive")
    result = await db.execute(
        select(NotificationEndpoint).where(
            NotificationEndpoint.provider == provider,
            NotificationEndpoint.address == normalized,
        )
    )
    endpoints = list(result.scalars().all())
    endpoint_owner_ids = {endpoint.user_id for endpoint in endpoints}
    if any(owner_id != user_id for owner_id in endpoint_owner_ids):
        raise NotificationEndpointConflictError(
            "notification endpoint is already bound to another user"
        )
    endpoint = min(
        endpoints,
        key=lambda item: (item.id is None, item.id or 0),
        default=None,
    )
    if endpoint is None:
        endpoint = NotificationEndpoint(
            user_id=user_id,
            provider=provider,
            address=normalized,
            verified=bool(verified),
            enabled=True,
            metadata_json=(
                json.dumps(metadata, ensure_ascii=False)
                if metadata is not None
                else None
            ),
        )
        db.add(endpoint)
        # Flush before constructing the exclusion predicate below.  Without
        # this, ``endpoint.id`` is None and SQLAlchemy translates ``id !=
        # None`` to ``IS NOT NULL``; the autoflush triggered by that query can
        # then include the just-created row and immediately disable it.
        if endpoint.id is None and hasattr(db, "flush"):
            await _flush_session(db)
    else:
        endpoint.enabled = True
        endpoint.verified = bool(endpoint.verified or verified)
        if metadata is not None:
            endpoint.metadata_json = json.dumps(metadata, ensure_ascii=False)
    if provider == NotificationProvider.TELEGRAM.value:
        # Delivery rows are unique by (user, channel), so keep exactly one
        # active Telegram endpoint for a user.  Disable old endpoints
        # deterministically instead of allowing the worker to pick an
        # arbitrary address.  Do not rewrite a populated legacy telegram_id:
        # child tables still reference that key.
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
    return endpoint


async def bind_notification_endpoint(
    db: AsyncSession,
    user_id: int,
    provider: str,
    address: str,
    *,
    verified: bool = False,
    metadata: dict | None = None,
) -> NotificationEndpoint:
    """Create or update an endpoint and commit it immediately."""

    try:
        endpoint = await stage_notification_endpoint(
            db,
            user_id,
            provider,
            address,
            verified=verified,
            metadata=metadata,
        )
        await db.commit()
        await db.refresh(endpoint)
        return endpoint
    except IntegrityError as exc:
        await db.rollback()
        raise NotificationEndpointConflictError(
            "notification endpoint is already bound to another user"
        ) from exc


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


async def migrate_legacy_identity_data(
    db: AsyncSession | None = None,
) -> dict[str, int | list[str]]:
    """Idempotently backfill legacy usernames/Telegram ids into new tables.

    All source rows are preloaded once.  Legacy username bridges are
    preflighted before any mutation so a case-insensitive ambiguous group is
    recorded and skipped without creating one arbitrary synthetic alias;
    runtime OAuth matching independently fails closed for that key.  Conflicting
    endpoints are left untouched and counted, while the original
    ``telegram_users`` rows and all legacy foreign keys remain unchanged.
    """

    owns_session = db is None
    if db is None:
        from backend.models.database import async_session

        db = async_session()
    created_identities = created_endpoints = conflicts = 0
    try:
        users = list(
            (
                await db.execute(
                    select(TelegramUser).order_by(TelegramUser.id)
                )
            )
            .scalars()
            .all()
        )
        users_by_id = {user.id: user for user in users}

        # Startup runs this migration repeatedly, and rows may have been
        # inserted by a setup/restore operation since the previous run.  Do
        # not use a completion marker; preload the current source tables every
        # time and maintain indexes while staging new rows below.
        identity_rows = list(
            (
                await db.execute(
                    select(UserIdentity)
                    .where(UserIdentity.provider == AuthProvider.GITHUB.value)
                    .order_by(UserIdentity.id)
                )
            )
            .scalars()
            .all()
        )
        endpoint_rows = list(
            (
                await db.execute(
                    select(NotificationEndpoint).order_by(NotificationEndpoint.id)
                )
            )
            .scalars()
            .all()
        )

        identities_by_user: dict[int, list[UserIdentity]] = defaultdict(list)
        legacy_aliases_by_key: dict[str, list[UserIdentity]] = defaultdict(list)
        stable_owner_ids_by_key: dict[str, set[int]] = defaultdict(set)
        legacy_owner_ids_by_key: dict[str, set[int]] = defaultdict(set)
        for identity in identity_rows:
            identities_by_user[int(identity.user_id)].append(identity)
            if _is_legacy_github_identity(identity):
                provider_id = str(identity.provider_user_id).casefold()
                alias_key = provider_id.removeprefix("legacy:").removeprefix(
                    "login:"
                )
                display_key = str(identity.provider_username or "").strip().casefold()
                if alias_key:
                    legacy_aliases_by_key[alias_key].append(identity)
                if display_key and display_key != alias_key:
                    legacy_aliases_by_key[display_key].append(identity)

        # A stable identity is also an owner for its current GitHub username.
        # A legacy mirror may upgrade only when it is the sole owner of the
        # normalized key.  Include both fields because an imported stable row
        # can retain a stale provider_username while the mirror was renamed.
        for user in users:
            username_key = str(user.github_username or "").strip().casefold()
            if not username_key:
                continue
            user_identities = identities_by_user.get(int(user.id), [])
            has_stable_identity = any(
                not _is_legacy_github_identity(identity)
                for identity in user_identities
            )
            if has_stable_identity:
                stable_owner_ids_by_key[username_key].add(int(user.id))
                for identity in user_identities:
                    provider_username = str(
                        identity.provider_username or ""
                    ).strip().casefold()
                    if provider_username:
                        stable_owner_ids_by_key[provider_username].add(int(user.id))
            else:
                legacy_owner_ids_by_key[username_key].add(int(user.id))

        # Existing synthetic rows are considered only when their owner still
        # mirrors the same normalized username.  Stale aliases from an admin
        # rename stay inert and must not block a new legitimate account.
        alias_owner_ids_by_key: dict[str, set[int]] = defaultdict(set)
        for alias_key, aliases in legacy_aliases_by_key.items():
            for identity in aliases:
                owner = users_by_id.get(identity.user_id)
                if owner is None:
                    continue
                owner_key = str(owner.github_username or "").strip().casefold()
                if owner_key == alias_key:
                    alias_owner_ids_by_key[alias_key].add(int(owner.id))

        ambiguous_username_keys: set[str] = set()
        for key in (
            set(stable_owner_ids_by_key)
            | set(legacy_owner_ids_by_key)
            | set(alias_owner_ids_by_key)
        ):
            owner_ids = (
                stable_owner_ids_by_key.get(key, set())
                | legacy_owner_ids_by_key.get(key, set())
                | alias_owner_ids_by_key.get(key, set())
            )
            if len(owner_ids) > 1:
                # Keep unrelated users/endpoints migratable.  Runtime OAuth
                # matching has an independent guard and will raise the
                # dedicated ambiguity exception for this key.
                ambiguous_username_keys.add(key)

        endpoints_by_key: dict[tuple[str, str], list[NotificationEndpoint]] = (
            defaultdict(list)
        )
        endpoints_by_user: dict[int, list[NotificationEndpoint]] = defaultdict(list)
        for endpoint in endpoint_rows:
            provider = str(endpoint.provider).casefold()
            address = str(endpoint.address)
            key = (
                provider,
                address.casefold()
                if provider == NotificationProvider.EMAIL.value
                else address,
            )
            endpoints_by_key[key].append(endpoint)
            endpoints_by_user[int(endpoint.user_id)].append(endpoint)

        for user in users:
            username_key = str(user.github_username or "").strip().casefold()
            user_identities = identities_by_user.get(int(user.id), [])
            has_stable_identity = any(
                not _is_legacy_github_identity(identity)
                for identity in user_identities
            )
            if (
                username_key
                and not has_stable_identity
                and username_key not in ambiguous_username_keys
            ):
                synthetic_id = _legacy_provider_id(user.github_username)
                aliases = legacy_aliases_by_key.get(username_key, [])
                owner_conflict = any(
                    alias.user_id != user.id
                    for alias in aliases
                    if alias.user_id in users_by_id
                )
                # Existing aliases with the same normalized key are retained,
                # including malformed/stale ones; runtime matching validates
                # both fields independently.  Never create a second synthetic
                # row when an alias already exists for this owner.
                has_alias = any(alias.user_id == user.id for alias in aliases)
                if owner_conflict:
                    conflicts += 1
                elif not has_alias:
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
                key = (NotificationProvider.TELEGRAM.value, address)
                matching_endpoints = endpoints_by_key.get(key, [])
                if any(
                    endpoint.user_id != user.id
                    for endpoint in matching_endpoints
                ):
                    conflicts += 1
                elif not any(
                    str(existing.provider).casefold()
                    == NotificationProvider.TELEGRAM.value
                    for existing in endpoints_by_user.get(int(user.id), [])
                ):
                    endpoint = NotificationEndpoint(
                        user_id=user.id,
                        provider=NotificationProvider.TELEGRAM.value,
                        address=address,
                        verified=True,
                        enabled=True,
                    )
                    db.add(endpoint)
                    endpoints_by_key[key].append(endpoint)
                    endpoints_by_user[int(user.id)].append(endpoint)
                    created_endpoints += 1

            if user.email:
                email = _normalized_email(user.email)
                if email is None:
                    continue
                key = (NotificationProvider.EMAIL.value, email)
                matching_endpoints = endpoints_by_key.get(key, [])
                if any(
                    endpoint.user_id != user.id
                    for endpoint in matching_endpoints
                ):
                    conflicts += 1
                    continue
                email_endpoint = min(
                    matching_endpoints,
                    key=lambda item: (item.id is None, item.id or 0),
                    default=None,
                )
                if email_endpoint is None:
                    email_endpoint = NotificationEndpoint(
                        user_id=user.id,
                        provider=NotificationProvider.EMAIL.value,
                        address=email,
                        verified=bool(user.email_verified),
                        enabled=bool(user.email_verified),
                    )
                    db.add(email_endpoint)
                    endpoints_by_key[key].append(email_endpoint)
                    endpoints_by_user[int(user.id)].append(email_endpoint)
                    created_endpoints += 1
                else:
                    was_verified = bool(email_endpoint.verified)
                    incoming_verified = bool(user.email_verified)
                    email_endpoint.verified = bool(
                        was_verified or incoming_verified
                    )
                    if not was_verified:
                        # A legacy migration repairs unsafe unverified rows;
                        # a later OAuth verification path handles explicit
                        # opt-in/reactivation separately.
                        email_endpoint.enabled = False
                if bool(user.email_verified):
                    for old_endpoint in endpoints_by_user.get(int(user.id), []):
                        if (
                            old_endpoint is not email_endpoint
                            and old_endpoint.provider
                            == NotificationProvider.EMAIL.value
                        ):
                            old_endpoint.enabled = False
                user.email = email
                user.email_verified = bool(email_endpoint.verified)
                user.email_updated_at = now_utc()
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        if owns_session:
            await db.close()
    result = {
        "identities_created": created_identities,
        "endpoints_created": created_endpoints,
        "conflicts": conflicts,
    }
    if ambiguous_username_keys:
        result["ambiguous_github_usernames"] = sorted(ambiguous_username_keys)
    return result
