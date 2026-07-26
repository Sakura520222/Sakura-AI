"""Canonical context revisions, snapshots, operations and thread leases.

The service deliberately treats an injected session as caller-owned: it flushes
but never commits or rolls back that session.  Sessions created by this service
own their transaction and are committed by the transaction context.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityCanonicalContextRevision,
    ActivityContextOperation,
    ActivityContextSnapshot,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityObservabilityMessage,
    ActivityThread,
    ActivityThreadLease,
)
from backend.models.database import utc_now


AVAILABILITY_REPORTED = "reported"
AVAILABILITY_COUNTED = "counted"
AVAILABILITY_ESTIMATED = "estimated"
AVAILABILITY_UNAVAILABLE = "unavailable"

SOURCE_PROVIDER = "provider"
SOURCE_MODEL_CATALOG = "model_catalog"
SOURCE_TOKEN_COUNTING_API = "token_counting_api"
SOURCE_TOKENIZER = "tokenizer"
SOURCE_CONFIGURATION = "configuration"
SOURCE_HEURISTIC = "heuristic"

VALID_AVAILABILITY = {
    AVAILABILITY_REPORTED,
    AVAILABILITY_COUNTED,
    AVAILABILITY_ESTIMATED,
    AVAILABILITY_UNAVAILABLE,
}
VALID_SOURCES = {
    SOURCE_PROVIDER,
    SOURCE_MODEL_CATALOG,
    SOURCE_TOKEN_COUNTING_API,
    SOURCE_TOKENIZER,
    SOURCE_CONFIGURATION,
    SOURCE_HEURISTIC,
}
VALID_OPERATION_TYPES = {
    "canonical_summary",
    "provider_compaction",
    "context_edit",
    "model_switch_handoff",
}
VALID_TRIGGER_REASONS = {"threshold", "provider_overflow", "model_switch", "manual"}
VALID_SNAPSHOT_KINDS = {
    "before_request",
    "after_request",
    "before_compaction",
    "after_compaction",
    "after_context_edit",
    "before_model_switch",
    "after_model_switch",
}
VALID_LEASE_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "abandoned"}


class StaleThreadLeaseError(RuntimeError):
    """Raised when a lease is expired, fenced, or a CAS precondition changed."""


@dataclass(frozen=True)
class ThreadLeaseToken:
    """Immutable proof that one work unit owns a thread's write authority."""

    thread_id: int
    owner_work_unit_id: int
    fencing_token: int
    base_revision_id: int | None
    expires_at: datetime

    @property
    def work_unit_id(self) -> int:
        """Backward-compatible alias used by older callers."""
        return self.owner_work_unit_id


@dataclass(frozen=True)
class MeasuredValue:
    """One context measurement with independent value/provenance metadata."""

    value: int | None
    availability: str
    source: str

    def __post_init__(self) -> None:
        if self.availability not in VALID_AVAILABILITY:
            raise ValueError(f"unknown availability: {self.availability}")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"unknown source: {self.source}")
        if self.value is None and self.availability != AVAILABILITY_UNAVAILABLE:
            raise ValueError("NULL value must be paired with availability='unavailable'")
        if self.value is not None:
            if self.value < 0:
                raise ValueError("context measurements must be non-negative")
            if self.availability == AVAILABILITY_UNAVAILABLE:
                raise ValueError("non-NULL value cannot be marked unavailable")


@dataclass(frozen=True)
class ContextSnapshotFields:
    """Field-level context snapshot inputs; no field shares provenance."""

    context_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_HEURISTIC
        )
    )
    context_window_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_MODEL_CATALOG
        )
    )
    reserved_output_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_CONFIGURATION
        )
    )
    available_context_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_HEURISTIC
        )
    )
    cache_read_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_PROVIDER
        )
    )
    cache_write_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_PROVIDER
        )
    )
    reasoning_context_tokens: MeasuredValue = field(
        default_factory=lambda: MeasuredValue(
            None, AVAILABILITY_UNAVAILABLE, SOURCE_PROVIDER
        )
    )

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            measured = getattr(self, name)
            if not isinstance(measured, MeasuredValue):
                raise TypeError(f"{name} must be a MeasuredValue")
        window = self.context_window_tokens.value
        reserved = self.reserved_output_tokens.value
        available = self.available_context_tokens.value
        if available is not None:
            if window is None or reserved is None:
                raise ValueError(
                    "available_context_tokens requires known context window and reservation"
                )
            if available != window - reserved:
                raise ValueError(
                    "available_context_tokens must equal window minus reserved output"
                )


def _manifest_json(message_manifest: list[int]) -> str:
    return json.dumps(message_manifest, separators=(",", ":"), ensure_ascii=False)


def _manifest_hash(manifest_json: str) -> str:
    return hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class ContextService:
    """Transactional context/revision service with a single writer per thread."""

    DEFAULT_LEASE_DURATION = timedelta(minutes=30)
    # Compatibility with the draft's public name.
    DEFAULT_LEASE_TTL = DEFAULT_LEASE_DURATION

    def __init__(
        self,
        db: AsyncSession | None = None,
        lease_duration: timedelta | None = None,
        lease_ttl: timedelta | None = None,
    ) -> None:
        self._db = db
        self._lease_duration = lease_duration or lease_ttl or self.DEFAULT_LEASE_DURATION
        if self._lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._db is not None:
            yield self._db
            return
        if db_module.async_session is None:
            raise RuntimeError("异步数据库会话尚未初始化")
        async with db_module.async_session() as db:
            yield db

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._session_scope() as db:
            if self._db is None:
                async with db.begin():
                    yield db
            else:
                # A savepoint protects an outer caller transaction when the real
                # AsyncSession supports it.  The small SQLite adapter used by the
                # tests intentionally has no savepoint API; it still only flushes.
                begin_nested = getattr(db, "begin_nested", None)
                if begin_nested is None:
                    yield db
                else:
                    async with begin_nested():
                        yield db

    @staticmethod
    async def _scalar(db: AsyncSession, statement: Any) -> Any:
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    @staticmethod
    def _expires_at(lease: ActivityThreadLease) -> datetime:
        value = _aware(lease.expires_at)
        if value is None:  # pragma: no cover - database column is non-null
            raise StaleThreadLeaseError("lease has no expiry")
        return value

    async def _validate_work_unit(
        self, db: AsyncSession, thread: ActivityThread, work_unit_id: int
    ) -> ActivityInvocationWorkUnit:
        work_unit = await db.get(
            ActivityInvocationWorkUnit, work_unit_id, with_for_update=True
        )
        if work_unit is None:
            raise ValueError(f"ActivityInvocationWorkUnit not found: {work_unit_id}")
        if (
            work_unit.thread_id != thread.id
            or work_unit.session_id != thread.session_id
        ):
            raise ValueError("work unit and thread do not belong to the same session/thread")
        return work_unit

    async def _validate_token(
        self,
        db: AsyncSession,
        thread: ActivityThread,
        token: ThreadLeaseToken,
        *,
        lock: bool = True,
    ) -> ActivityThreadLease:
        if token.thread_id != thread.id:
            raise StaleThreadLeaseError("lease token belongs to another thread")
        statement = select(ActivityThreadLease).where(
            ActivityThreadLease.thread_id == thread.id
        )
        if lock:
            statement = statement.with_for_update()
        lease = await self._scalar(db, statement)
        now = utc_now()
        if (
            lease is None
            or lease.owner_work_unit_id != token.owner_work_unit_id
            or lease.fencing_token != token.fencing_token
            or self._expires_at(lease) <= _aware(now)
        ):
            raise StaleThreadLeaseError("lease expired, released, or fenced")
        return lease

    async def acquire_lease(
        self,
        thread_id: int,
        work_unit_id: int,
        *,
        ttl: timedelta | None = None,
    ) -> ThreadLeaseToken:
        """Acquire, renew idempotently, or fence a thread lease.

        An active lease owned by another work unit is rejected.  An active lease
        owned by the same work unit is idempotently renewed without changing its
        fencing token.  An expired lease is atomically taken over with a strictly
        larger token.
        """
        duration = ttl or self._lease_duration
        if duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        async with self._transaction() as db:
            thread = await db.get(ActivityThread, thread_id, with_for_update=True)
            if thread is None:
                raise ValueError(f"ActivityThread not found: {thread_id}")
            await self._validate_work_unit(db, thread, work_unit_id)
            lease = await self._scalar(
                db,
                select(ActivityThreadLease)
                .where(ActivityThreadLease.thread_id == thread_id)
                .with_for_update(),
            )
            now = utc_now()
            expires_at = _aware(lease.expires_at) if lease else None
            if lease is not None and expires_at is not None and expires_at > _aware(now):
                if lease.owner_work_unit_id != work_unit_id:
                    raise StaleThreadLeaseError(
                        f"thread {thread_id} is actively leased by another work unit"
                    )
                lease.heartbeat_at = now
                lease.expires_at = now + duration
                await db.flush()
                return ThreadLeaseToken(
                    thread_id=thread_id,
                    owner_work_unit_id=work_unit_id,
                    fencing_token=lease.fencing_token,
                    base_revision_id=lease.base_revision_id,
                    expires_at=_aware(lease.expires_at),
                )

            previous = int(getattr(thread, "lease_fencing_token", 0) or 0)
            fencing_token = previous + 1
            thread.lease_fencing_token = fencing_token
            base_revision_id = thread.current_revision_id
            if lease is None:
                lease = ActivityThreadLease(thread_id=thread_id)
                db.add(lease)
            lease.owner_work_unit_id = work_unit_id
            lease.fencing_token = fencing_token
            lease.base_revision_id = base_revision_id
            lease.heartbeat_at = now
            lease.expires_at = now + duration
            await db.flush()
            return ThreadLeaseToken(
                thread_id=thread_id,
                owner_work_unit_id=work_unit_id,
                fencing_token=fencing_token,
                base_revision_id=base_revision_id,
                expires_at=_aware(lease.expires_at),
            )

    async def heartbeat(
        self, token: ThreadLeaseToken, *, ttl: timedelta | None = None
    ) -> ThreadLeaseToken:
        """Renew a non-expired lease without changing its fencing token."""
        duration = ttl or self._lease_duration
        if duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        async with self._transaction() as db:
            thread = await db.get(ActivityThread, token.thread_id, with_for_update=True)
            if thread is None:
                raise StaleThreadLeaseError("thread no longer exists")
            lease = await self._validate_token(db, thread, token)
            now = utc_now()
            lease.heartbeat_at = now
            lease.expires_at = now + duration
            await db.flush()
            return ThreadLeaseToken(
                thread_id=token.thread_id,
                owner_work_unit_id=lease.owner_work_unit_id,
                fencing_token=lease.fencing_token,
                base_revision_id=lease.base_revision_id,
                expires_at=_aware(lease.expires_at),
            )

    renew_lease = heartbeat

    async def release_lease(
        self, token: ThreadLeaseToken, terminal_status: str | None = None
    ) -> None:
        """Release exactly the lease represented by *token*.

        A missing or expired row is stale rather than an idempotent success: this
        prevents an old worker from releasing a newer owner's lease.
        """
        if terminal_status is not None and terminal_status not in VALID_LEASE_TERMINAL_STATUSES:
            allowed = ", ".join(sorted(VALID_LEASE_TERMINAL_STATUSES))
            raise ValueError(f"invalid terminal_status; expected one of: {allowed}")
        async with self._transaction() as db:
            thread = await db.get(ActivityThread, token.thread_id, with_for_update=True)
            if thread is None:
                raise StaleThreadLeaseError("thread no longer exists")
            lease = await self._validate_token(db, thread, token)
            work_unit = await self._validate_work_unit(
                db, thread, lease.owner_work_unit_id
            )
            if terminal_status is not None:
                work_unit.status = terminal_status
                work_unit.completed_at = utc_now()
            await db.delete(lease)
            await db.flush()

    async def expire_lease_for_test(self, thread_id: int) -> None:
        """Test-only helper; production recovery should use expiry timestamps."""
        async with self._transaction() as db:
            lease = await self._scalar(
                db,
                select(ActivityThreadLease)
                .where(ActivityThreadLease.thread_id == thread_id)
                .with_for_update(),
            )
            if lease is not None:
                lease.expires_at = utc_now() - timedelta(seconds=1)
                await db.flush()

    async def create_revision(
        self,
        thread_id: int,
        token: ThreadLeaseToken,
        expected_parent_revision_id: int | None,
        message_manifest: list[int],
        reason: str,
        context_operation_id: int | None = None,
        *,
        system_manifest: list[dict[str, Any]] | None = None,
        tools_manifest: list[dict[str, Any]] | None = None,
        tool_choice_manifest: dict[str, Any] | None = None,
        summary_artifact_reference: str | None = None,
        created_invocation_id: int | None = None,
        created_work_unit_id: int | None = None,
    ) -> ActivityCanonicalContextRevision:
        """Create an immutable revision and conditionally advance the thread head."""
        if not isinstance(message_manifest, list):
            raise TypeError("message_manifest must be a list[int]")
        if any(not isinstance(message_id, int) or isinstance(message_id, bool) for message_id in message_manifest):
            raise ValueError("message_manifest must contain integer message IDs")
        if len(set(message_manifest)) != len(message_manifest):
            raise ValueError("message_manifest must not contain duplicate IDs")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        async with self._transaction() as db:
            thread = await db.get(ActivityThread, thread_id, with_for_update=True)
            if thread is None:
                raise ValueError(f"ActivityThread not found: {thread_id}")
            lease = await self._validate_token(db, thread, token)
            if thread.current_revision_id != expected_parent_revision_id:
                raise StaleThreadLeaseError("expected parent revision does not match thread head")
            if lease.base_revision_id != expected_parent_revision_id:
                raise StaleThreadLeaseError("lease base revision does not match expected parent")

            if expected_parent_revision_id is not None:
                parent = await db.get(
                    ActivityCanonicalContextRevision,
                    expected_parent_revision_id,
                    with_for_update=True,
                )
                if parent is None or parent.thread_id != thread_id:
                    raise ValueError("expected parent revision does not belong to thread")
            if message_manifest:
                rows = (
                    await db.execute(
                        select(ActivityObservabilityMessage).where(
                            ActivityObservabilityMessage.id.in_(message_manifest)
                        )
                    )
                ).scalars().all()
                by_id = {row.id: row for row in rows}
                if len(by_id) != len(message_manifest):
                    raise ValueError("message_manifest contains an unknown message ID")
                if any(by_id[message_id].thread_id != thread_id for message_id in message_manifest):
                    raise ValueError("message_manifest contains a message from another thread")

            manifest_text = _manifest_json(message_manifest)
            max_number = await self._scalar(
                db,
                select(func.max(ActivityCanonicalContextRevision.revision_number)).where(
                    ActivityCanonicalContextRevision.thread_id == thread_id
                ),
            )
            revision_number = (
                0
                if max_number is None and expected_parent_revision_id is None and not message_manifest
                else int(max_number or 0) + 1
            )
            revision = ActivityCanonicalContextRevision(
                thread_id=thread_id,
                revision_number=revision_number,
                parent_revision_id=expected_parent_revision_id,
                message_manifest_json=manifest_text,
                reason=reason,
                summary_artifact_reference=summary_artifact_reference,
                system_manifest_json=(
                    json.dumps(system_manifest, separators=(",", ":"), ensure_ascii=False)
                    if system_manifest is not None
                    else None
                ),
                tools_manifest_json=(
                    json.dumps(tools_manifest, separators=(",", ":"), ensure_ascii=False)
                    if tools_manifest is not None
                    else None
                ),
                tool_choice_manifest_json=(
                    json.dumps(tool_choice_manifest, separators=(",", ":"), ensure_ascii=False)
                    if tool_choice_manifest is not None
                    else None
                ),
                content_hash=_manifest_hash(manifest_text),
                created_invocation_id=created_invocation_id,
                created_work_unit_id=created_work_unit_id,
                created_context_operation_id=context_operation_id,
                status="ready",
            )
            db.add(revision)
            await db.flush()

            cas = await db.execute(
                update(ActivityThread)
                .where(
                    ActivityThread.id == thread_id,
                    ActivityThread.current_revision_id == expected_parent_revision_id,
                )
                .values(current_revision_id=revision.id, last_active_at=utc_now())
            )
            if cas.rowcount != 1:
                await db.delete(revision)
                await db.flush()
                raise StaleThreadLeaseError("thread revision CAS failed")
            lease.base_revision_id = revision.id
            await db.flush()
            return revision

    async def get_revision(self, revision_id: int) -> ActivityCanonicalContextRevision | None:
        async with self._session_scope() as db:
            return await db.get(ActivityCanonicalContextRevision, revision_id)

    async def get_thread(self, thread_id: int) -> ActivityThread | None:
        async with self._session_scope() as db:
            return await db.get(ActivityThread, thread_id)

    async def context_revision_for_next_attempt(self, thread_id: int) -> int | None:
        thread = await self.get_thread(thread_id)
        return thread.current_revision_id if thread is not None else None

    async def begin_operation(
        self,
        work_unit_id: int,
        operation_type: str,
        trigger_reason: str,
        before_revision_id: int | None,
    ) -> ActivityContextOperation:
        if operation_type not in VALID_OPERATION_TYPES:
            raise ValueError(f"unknown operation_type: {operation_type}")
        if trigger_reason not in VALID_TRIGGER_REASONS:
            raise ValueError(f"unknown trigger_reason: {trigger_reason}")
        async with self._transaction() as db:
            work_unit = await db.get(
                ActivityInvocationWorkUnit, work_unit_id, with_for_update=True
            )
            if work_unit is None:
                raise ValueError(f"ActivityInvocationWorkUnit not found: {work_unit_id}")
            if work_unit.thread_id is None:
                raise ValueError("context operations require a threaded work unit")
            thread = await db.get(ActivityThread, work_unit.thread_id, with_for_update=True)
            if thread is None or thread.session_id != work_unit.session_id:
                raise ValueError("work unit thread does not belong to its session")
            before = None
            if before_revision_id is not None:
                before = await db.get(
                    ActivityCanonicalContextRevision,
                    before_revision_id,
                    with_for_update=True,
                )
                if before is None or before.thread_id != thread.id:
                    raise ValueError("before revision does not belong to work unit thread")
            operation = ActivityContextOperation(
                work_unit_id=work_unit_id,
                thread_id=thread.id,
                operation_type=operation_type,
                trigger_reason=trigger_reason,
                before_revision_id=before_revision_id,
                status="running",
            )
            db.add(operation)
            await db.flush()
            return operation

    async def complete_operation(
        self,
        operation_id: int,
        after_revision_id: int,
        summary_artifact_id: int | None = None,
        *,
        summary_artifact_reference: str | None = None,
        token: ThreadLeaseToken | None = None,
    ) -> ActivityContextOperation:
        """Complete an operation, allowing only identical completed replays."""
        requested_artifact = (
            summary_artifact_reference
            if summary_artifact_reference is not None
            else (str(summary_artifact_id) if summary_artifact_id is not None else None)
        )
        async with self._transaction() as db:
            operation = await db.get(
                ActivityContextOperation, operation_id, with_for_update=True
            )
            if operation is None:
                raise ValueError(f"ActivityContextOperation not found: {operation_id}")
            thread = await db.get(ActivityThread, operation.thread_id, with_for_update=True)
            if thread is None:
                raise ValueError("operation thread no longer exists")
            work_unit = await self._validate_work_unit(db, thread, operation.work_unit_id)
            if token is not None:
                await self._validate_token(db, thread, token)
                if token.owner_work_unit_id != work_unit.id:
                    raise StaleThreadLeaseError("completion token belongs to another work unit")
            after = await db.get(
                ActivityCanonicalContextRevision, after_revision_id, with_for_update=True
            )
            if after is None or after.thread_id != thread.id:
                raise ValueError("after revision does not belong to operation thread")
            if operation.before_revision_id is not None and after.parent_revision_id != operation.before_revision_id:
                raise ValueError("after revision is not a child of before revision")
            if after.created_context_operation_id != operation.id:
                raise ValueError("after revision is not associated with this operation")
            if thread.current_revision_id != after.id:
                raise StaleThreadLeaseError("thread no longer points to after revision")

            if operation.status == "completed":
                if (
                    operation.after_revision_id != after.id
                    or operation.summary_artifact_reference != requested_artifact
                ):
                    raise ValueError("conflicting replay of completed operation")
                return operation
            if operation.status != "running":
                raise ValueError(f"operation is not running: {operation.status}")
            operation.after_revision_id = after.id
            operation.summary_artifact_reference = requested_artifact
            operation.completed_at = utc_now()
            operation.status = "completed"
            await db.flush()
            return operation

    async def fail_operation(
        self, operation_id: int, error_message: str, *, token: ThreadLeaseToken | None = None
    ) -> ActivityContextOperation:
        if not error_message:
            raise ValueError("error_message must be non-empty")
        async with self._transaction() as db:
            operation = await db.get(ActivityContextOperation, operation_id, with_for_update=True)
            if operation is None:
                raise ValueError(f"ActivityContextOperation not found: {operation_id}")
            thread = await db.get(ActivityThread, operation.thread_id, with_for_update=True)
            if thread is None:
                raise ValueError("operation thread no longer exists")
            if token is not None:
                await self._validate_token(db, thread, token)
            if operation.status == "failed":
                if operation.error_message != error_message:
                    raise ValueError("conflicting replay of failed operation")
                return operation
            if operation.status != "running":
                raise ValueError(f"operation is not running: {operation.status}")
            operation.status = "failed"
            operation.error_message = error_message
            operation.completed_at = utc_now()
            await db.flush()
            return operation

    async def record_snapshot(
        self,
        *,
        attempt_id: int | None,
        operation_id: int | None,
        revision_id: int | None,
        snapshot_kind: str,
        fields: ContextSnapshotFields,
    ) -> ActivityContextSnapshot:
        if (attempt_id is None) == (operation_id is None):
            raise ValueError("snapshot must reference exactly one attempt or operation")
        if revision_id is None:
            raise ValueError("snapshot must reference a context revision")
        if snapshot_kind not in VALID_SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot_kind: {snapshot_kind}")
        async with self._transaction() as db:
            revision = await db.get(
                ActivityCanonicalContextRevision, revision_id, with_for_update=True
            )
            if revision is None:
                raise ValueError(f"ActivityCanonicalContextRevision not found: {revision_id}")
            if attempt_id is not None:
                attempt = await db.get(ActivityModelAttempt, attempt_id, with_for_update=True)
                if attempt is None:
                    raise ValueError(f"ActivityModelAttempt not found: {attempt_id}")
                work_unit = await db.get(ActivityInvocationWorkUnit, attempt.work_unit_id)
                if work_unit is None or work_unit.thread_id != revision.thread_id:
                    raise ValueError("attempt and revision do not share a thread")
            else:
                operation = await db.get(ActivityContextOperation, operation_id, with_for_update=True)
                if operation is None:
                    raise ValueError(f"ActivityContextOperation not found: {operation_id}")
                if operation.thread_id != revision.thread_id:
                    raise ValueError("operation and revision do not share a thread")
            values: dict[str, Any] = {}
            for name in fields.__dataclass_fields__:
                measured = getattr(fields, name)
                values[name] = measured.value
                values[f"{name}_availability"] = measured.availability
                values[f"{name}_source"] = measured.source
            snapshot = ActivityContextSnapshot(
                attempt_id=attempt_id,
                context_operation_id=operation_id,
                context_revision_id=revision_id,
                snapshot_kind=snapshot_kind,
                **values,
            )
            db.add(snapshot)
            await db.flush()
            return snapshot

    async def append_canonical_message(
        self,
        *,
        thread_id: int,
        work_unit_id: int,
        revision_id: int,
        seq: int,
        role: str,
        content: str | None,
        message_json: dict[str, Any],
        artifact_id: int | None = None,
        tool_call_id: str | None = None,
        origin_attempt_id: int | None = None,
    ) -> ActivityObservabilityMessage:
        """Append safe canonical content; provider reasoning is never persisted."""
        if seq < 0:
            raise ValueError("message sequence must be non-negative")
        payload = dict(message_json)
        payload.pop("reasoning_content", None)
        payload.pop("reasoning", None)
        async with self._transaction() as db:
            thread = await db.get(ActivityThread, thread_id, with_for_update=True)
            if thread is None:
                raise ValueError(f"ActivityThread not found: {thread_id}")
            work_unit = await self._validate_work_unit(db, thread, work_unit_id)
            revision = await db.get(ActivityCanonicalContextRevision, revision_id)
            if revision is None or revision.thread_id != thread_id:
                raise ValueError("message revision does not belong to thread")
            if origin_attempt_id is not None:
                attempt = await db.get(ActivityModelAttempt, origin_attempt_id)
                if attempt is None or attempt.work_unit_id != work_unit.id:
                    raise ValueError("origin attempt does not belong to work unit")
            message = ActivityObservabilityMessage(
                thread_id=thread_id,
                work_unit_id=work_unit_id,
                revision_id=revision_id,
                context_revision_id=revision_id,
                origin_attempt_id=origin_attempt_id,
                seq=seq,
                role=role,
                content=content,
                message_json=json.dumps(payload, ensure_ascii=False, default=str),
                artifact_id=artifact_id,
                tool_call_id=tool_call_id,
            )
            db.add(message)
            await db.flush()
            return message
