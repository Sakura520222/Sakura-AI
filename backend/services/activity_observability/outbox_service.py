"""Transactional activity events and user-scoped outbox delivery.

The activity stream is deliberately a notification channel, not a data channel.
Every notification is written with the event in the caller's transaction and the
browser must fetch the authorised projection through REST afterwards.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityObservabilityEvent,
    ActivityObservabilitySession,
    ActivityOutbox,
)
from backend.models.database import utc_now
from backend.services.activity_observability.contracts import PublicActivityNotification

_ALLOWED_VISIBILITIES = frozenset({"public", "admin_only", "internal", "hidden"})
_PUBLIC_PROJECTION_VERSION = 1


class RecipientResolver(Protocol):
    """Resolve the current authorised audience for one event."""

    async def resolve_recipients(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        event: ActivityObservabilityEvent,
        visibility: str,
        payload: dict[str, Any],
    ) -> Iterable[str]: ...


class DispatchAuthorizer(Protocol):
    """Re-check one outbox recipient at dispatch time."""

    async def is_authorized(
        self, db: AsyncSession, *, user_id: str, session_id: int
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class OutboxRetryPolicy:
    """Dispatcher policy supplied by application configuration.

    ``max_attempts=None`` means retry indefinitely.  This is intentionally a
    policy object rather than constants embedded in the dispatcher.
    """

    max_attempts: int | None = None
    initial_delay_seconds: float = 1.0
    backoff_factor: float = 2.0
    max_delay_seconds: float | None = None
    exhausted_status: str = "failed"

    def delay_for(self, attempt_count: int) -> float:
        delay = self.initial_delay_seconds * (
            self.backoff_factor ** max(attempt_count - 1, 0)
        )
        if self.max_delay_seconds is not None:
            delay = min(delay, self.max_delay_seconds)
        return max(delay, 0.0)

    def exhausted(self, attempt_count: int) -> bool:
        return self.max_attempts is not None and attempt_count >= self.max_attempts


@dataclass(frozen=True, slots=True)
class OutboxDispatcherConfig:
    """Runtime dispatcher controls; callers should build this from settings."""

    batch_size: int = 50
    poll_interval_seconds: float = 1.0
    claim_timeout_seconds: float | None = None
    retry_policy: OutboxRetryPolicy = OutboxRetryPolicy()


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    """Immutable claim snapshot safe across per-row transaction rollbacks."""

    id: int
    target_user_id: str
    session_id: int
    payload_json: str
    created_at: Any
    claim_token: str | None
    attempt_count: int


class OutboxPayloadError(ValueError):
    """Raised when an event payload is not safe JSON."""


def _safe_json(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a JSON object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OutboxPayloadError("payload must be finite, safe JSON") from exc
    return encoded


def _normalise_recipient(user_id: object) -> str:
    if not isinstance(user_id, str):
        user_id = str(user_id)
    value = user_id.strip()
    if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("recipient user id must be a non-empty safe identifier")
    if len(value) > 255:
        raise ValueError("recipient user id is too long")
    return value


async def _call_resolver(
    resolver: RecipientResolver | Callable[..., Any],
    db: AsyncSession,
    *,
    session_id: int,
    event: ActivityObservabilityEvent,
    visibility: str,
    payload: dict[str, Any],
) -> Iterable[str]:
    target = getattr(resolver, "resolve_recipients", resolver)
    kwargs = {
        "db": db,
        "session_id": session_id,
        "event": event,
        "visibility": visibility,
        "payload": payload,
    }
    try:
        parameters = inspect.signature(target).parameters
    except TypeError, ValueError:
        parameters = {}
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        result = target(**kwargs)
    else:
        result = target(
            **{name: value for name, value in kwargs.items() if name in parameters}
        )
    if inspect.isawaitable(result):
        result = await result
    return result or ()


async def _resolve_recipients(
    db: AsyncSession,
    *,
    event: ActivityObservabilityEvent,
    visibility: str,
    payload: dict[str, Any],
    recipient_resolver: RecipientResolver | Callable[..., Any] | None,
    recipient_user_ids: Iterable[str] | None,
) -> tuple[str, ...]:
    if recipient_user_ids is not None and recipient_resolver is not None:
        raise ValueError("provide recipient_resolver or recipient_user_ids, not both")
    if recipient_user_ids is not None:
        raw_recipients = recipient_user_ids
    elif recipient_resolver is not None:
        raw_recipients = await _call_resolver(
            recipient_resolver,
            db,
            session_id=event.session_id,
            event=event,
            visibility=visibility,
            payload=payload,
        )
    else:
        # Failing closed is important: an event without an explicit audience
        # must never accidentally become a global notification.
        raise ValueError("an explicit recipient resolver is required")
    return tuple(dict.fromkeys(_normalise_recipient(item) for item in raw_recipients))


async def append_event_and_outbox(
    db: AsyncSession,
    *,
    session_id: int,
    invocation_id: int | None = None,
    work_unit_id: int | None = None,
    event_type: str,
    visibility: str,
    payload: dict[str, Any],
    recipient_resolver: RecipientResolver | Callable[..., Any] | None = None,
    recipient_user_ids: Iterable[str] | None = None,
    projection_version: int = _PUBLIC_PROJECTION_VERSION,
) -> ActivityObservabilityEvent:
    """Append an event and its user-scoped outbox rows without committing.

    The caller owns the transaction.  The session row is locked before its
    sequence is incremented, and all optional parent IDs are checked against
    that same session.  The outbox stores only the three-field notification
    envelope; the event projection remains available to the authorised REST
    service and is never sent directly to SSE.
    """

    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type must not be empty")
    if visibility not in _ALLOWED_VISIBILITIES:
        raise ValueError(f"unsupported event visibility: {visibility}")
    if not isinstance(projection_version, int) or projection_version < 1:
        raise ValueError("projection_version must be a positive integer")
    payload_json = _safe_json(payload)

    session = (
        await db.execute(
            select(ActivityObservabilitySession)
            .where(ActivityObservabilitySession.id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise ValueError(f"ActivityObservabilitySession not found: {session_id}")

    invocation = None
    if invocation_id is not None:
        invocation = await db.get(ActivityInvocation, invocation_id)
        if invocation is None or invocation.session_id != session_id:
            raise ValueError("invocation does not belong to session")

    work_unit = None
    if work_unit_id is not None:
        work_unit = await db.get(ActivityInvocationWorkUnit, work_unit_id)
        if (
            work_unit is None
            or work_unit.session_id != session_id
            or (invocation is not None and work_unit.invocation_id != invocation.id)
        ):
            raise ValueError("work unit does not belong to event parent chain")

    sequence = int(session.session_event_sequence or 0) + 1
    session.session_event_sequence = sequence
    created_at = utc_now()
    event = ActivityObservabilityEvent(
        event_uuid=str(uuid4()),
        session_id=session_id,
        invocation_id=invocation_id,
        work_unit_id=work_unit_id,
        event_sequence=sequence,
        event_type=event_type.strip(),
        visibility=visibility,
        projection_json=payload_json,
        created_at=created_at,
    )
    db.add(event)
    await db.flush()

    recipients = await _resolve_recipients(
        db,
        event=event,
        visibility=visibility,
        payload=payload,
        recipient_resolver=recipient_resolver,
        recipient_user_ids=recipient_user_ids,
    )
    # Do not put payload_json in the notification envelope.  REST projection
    # reads ActivityObservabilityEvent after a commit and performs authorisation.
    notification_json = _safe_json(
        {
            "event_id": event.event_uuid,
            "sequence": sequence,
            "projection_version": projection_version,
        }
    )
    for recipient in recipients:
        db.add(
            ActivityOutbox(
                event_uuid=event.event_uuid,
                target_user_id=recipient,
                session_id=session_id,
                event_sequence=sequence,
                projection_version=projection_version,
                status="pending",
                payload_json=notification_json,
                attempt_count=0,
                created_at=created_at,
            )
        )
    await db.flush()
    return event


class ActivityOutboxService:
    """Convenience wrapper for callers that already own an AsyncSession."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        recipient_resolver: RecipientResolver | Callable[..., Any] | None = None,
        projection_version: int = _PUBLIC_PROJECTION_VERSION,
    ) -> None:
        self.db = db
        self.recipient_resolver = recipient_resolver
        self.projection_version = projection_version

    async def append_event_and_outbox(
        self, **kwargs: Any
    ) -> ActivityObservabilityEvent:
        kwargs.setdefault("recipient_resolver", self.recipient_resolver)
        kwargs.setdefault("projection_version", self.projection_version)
        return await append_event_and_outbox(self.db, **kwargs)

    async def record_event_for_session(
        self,
        session_id: int,
        payload: dict[str, Any],
        *,
        event_type: str = "activity",
        visibility: str = "public",
        invocation_id: int | None = None,
        work_unit_id: int | None = None,
        recipient_user_ids: Iterable[str] | None = None,
    ) -> ActivityObservabilityEvent:
        return await self.append_event_and_outbox(
            session_id=session_id,
            invocation_id=invocation_id,
            work_unit_id=work_unit_id,
            event_type=event_type,
            visibility=visibility,
            payload=payload,
            recipient_user_ids=recipient_user_ids,
        )


class OutboxDispatcher:
    """Claim, authorise, and publish outbox rows at least once."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        authorizer: DispatchAuthorizer | Callable[..., Any],
        publisher: Callable[[str, PublicActivityNotification], Awaitable[None]]
        | None = None,
        config: OutboxDispatcherConfig | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.authorizer = authorizer
        self.config = config or OutboxDispatcherConfig()
        self.publisher = publisher
        self._stop_event = asyncio.Event()

    async def _call_authorizer(
        self, db: AsyncSession, *, user_id: str, session_id: int
    ) -> bool:
        target = getattr(self.authorizer, "is_authorized", self.authorizer)
        kwargs = {"db": db, "user_id": user_id, "session_id": session_id}
        try:
            parameters = inspect.signature(target).parameters
        except TypeError, ValueError:
            parameters = {}
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            result = target(**kwargs)
        else:
            result = target(
                **{name: value for name, value in kwargs.items() if name in parameters}
            )
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _claim(self, db: AsyncSession) -> list[ActivityOutbox]:
        now = utc_now()
        eligible = [
            and_(
                ActivityOutbox.status == "pending",
                (ActivityOutbox.next_attempt_at.is_(None))
                | (ActivityOutbox.next_attempt_at <= now),
            )
        ]
        timeout = self.config.claim_timeout_seconds
        if timeout is not None:
            eligible.append(
                and_(
                    ActivityOutbox.status == "claimed",
                    ActivityOutbox.claimed_at.is_not(None),
                    ActivityOutbox.claimed_at <= now - timedelta(seconds=timeout),
                )
            )
        query = (
            select(ActivityOutbox)
            .where(or_(*eligible))
            .order_by(ActivityOutbox.id)
            .limit(self.config.batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = (await db.execute(query)).scalars().all()
        if not rows:
            await db.commit()
            return []
        claimed: list[ActivityOutbox] = []
        for row in rows:
            row.status = "claimed"
            row.claim_token = secrets.token_urlsafe(24)
            row.claimed_at = now
            row.attempt_count = int(row.attempt_count or 0) + 1
            claimed.append(row)
        await db.commit()  # claim is committed before any network publish
        return claimed

    @staticmethod
    def _notification(row: OutboxClaim | ActivityOutbox) -> PublicActivityNotification:
        try:
            envelope = json.loads(row.payload_json)
            event_id = envelope["event_id"]
            sequence = envelope["sequence"]
            projection_version = envelope["projection_version"]
            if set(envelope) != {"event_id", "sequence", "projection_version"}:
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise OutboxPayloadError("invalid outbox notification envelope") from exc
        return PublicActivityNotification(
            event_id=str(event_id),
            session_id=int(row.session_id),
            invocation_id=None,
            work_unit_id=None,
            sequence=int(sequence),
            projection_version=int(projection_version),
            created_at=row.created_at or utc_now(),
        )

    async def _default_publish(
        self, user_id: str, notification: PublicActivityNotification
    ) -> None:
        from backend.webui.sse import publish_user_activity_notification

        await publish_user_activity_notification(user_id, notification)

    async def _finish_success(
        self, db: AsyncSession, row_id: int, claim_token: str | None
    ) -> bool:
        statement = (
            update(ActivityOutbox)
            .where(
                ActivityOutbox.id == row_id,
                ActivityOutbox.status == "claimed",
                ActivityOutbox.claim_token == claim_token,
            )
            .values(
                status="published",
                published_at=utc_now(),
                claim_token=None,
                claimed_at=None,
                last_error=None,
            )
        )
        result = await db.execute(statement)
        if result.rowcount != 1:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def _finish_failure(
        self,
        db: AsyncSession,
        row_id: int,
        claim_token: str | None,
        error: Exception,
    ) -> None:
        row = await db.get(ActivityOutbox, row_id)
        if row is None:
            await db.rollback()
            return
        attempts = int(row.attempt_count or 0)
        policy = self.config.retry_policy
        exhausted = policy.exhausted(attempts)
        values = {
            "claim_token": None,
            "claimed_at": None,
            "last_error": type(error).__name__,
            "status": policy.exhausted_status if exhausted else "pending",
            "next_attempt_at": None
            if exhausted
            else utc_now() + timedelta(seconds=policy.delay_for(attempts)),
        }
        statement = (
            update(ActivityOutbox)
            .where(
                ActivityOutbox.id == row_id,
                ActivityOutbox.status == "claimed",
                ActivityOutbox.claim_token == claim_token,
            )
            .values(**values)
        )
        result = await db.execute(statement)
        if result.rowcount != 1:
            await db.rollback()
            return
        await db.commit()

    async def _cancel_unauthorised(
        self, db: AsyncSession, row_id: int, claim_token: str | None
    ) -> None:
        statement = (
            update(ActivityOutbox)
            .where(
                ActivityOutbox.id == row_id,
                ActivityOutbox.status == "claimed",
                ActivityOutbox.claim_token == claim_token,
            )
            .values(
                status="cancelled",
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error="authorization_revoked",
            )
        )
        result = await db.execute(statement)
        if result.rowcount != 1:
            await db.rollback()
            return
        await db.commit()

    async def dispatch_once(self) -> int:
        delivered = 0
        async with self.session_factory() as db:
            rows = await self._claim(db)
            claims = tuple(
                OutboxClaim(
                    id=row.id,
                    target_user_id=row.target_user_id,
                    session_id=row.session_id,
                    payload_json=row.payload_json,
                    created_at=row.created_at or utc_now(),
                    claim_token=row.claim_token,
                    attempt_count=int(row.attempt_count or 0),
                )
                for row in rows
            )
        for claim in claims:
            async with self.session_factory() as db:
                try:
                    authorised = await self._call_authorizer(
                        db,
                        user_id=claim.target_user_id,
                        session_id=claim.session_id,
                    )
                    if not authorised:
                        await self._cancel_unauthorised(db, claim.id, claim.claim_token)
                        continue
                    notification = self._notification(claim)
                    publish = self.publisher or self._default_publish
                    await publish(claim.target_user_id, notification)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # publish failures are retryable
                    await self._finish_failure(db, claim.id, claim.claim_token, exc)
                    logger.warning(
                        "activity outbox delivery deferred: {}", type(exc).__name__
                    )
                else:
                    if await self._finish_success(db, claim.id, claim.claim_token):
                        delivered += 1
        return delivered

    async def run(self) -> None:
        """Run until ``stop``; tests can call ``dispatch_once`` without a task."""
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                await self.dispatch_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.config.poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()


# Stable aliases for callers that prefer the domain name.
ActivityOutboxDispatcher = OutboxDispatcher


__all__ = [
    "ActivityOutboxDispatcher",
    "ActivityOutboxService",
    "DispatchAuthorizer",
    "OutboxDispatcher",
    "OutboxDispatcherConfig",
    "OutboxPayloadError",
    "OutboxRetryPolicy",
    "RecipientResolver",
    "append_event_and_outbox",
]
