"""Database-backed execution slots, independent of per-user billing admission.

Every mutation first writes the feature gate. This is a short row lock on
MySQL/PostgreSQL and a database write lock on SQLite, whose FOR UPDATE is a
no-op. Provider requests never hold this transaction. Expired leases are fenced
by their unique token and cannot be renewed or release a replacement lease.
"""

from __future__ import annotations

import asyncio
import math
import os
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models import database
from backend.models.database import AppConfig
from backend.models.service_execution_models import (
    ServiceExecutionGate,
    ServiceExecutionLease,
    ServiceExecutionOwnership,
)
from backend.services.billing_context import get_billing_context

FEATURE_CONFIG_KEYS = {
    "pr_review": "max_concurrent_reviews",
    "issue_analysis": "max_concurrent_issues",
    "agent": "agent_team_max_concurrent",
}


class ServiceExecutionCapacityError(RuntimeError):
    """Capacity coordination is unavailable; never fall back to a local lock."""


class ServiceExecutionLeaseLost(ServiceExecutionCapacityError):
    """Execution must stop because its service slot can no longer be proved."""


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    token: str
    feature: str
    owner_id: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ExecutionOwnership:
    token: str
    operation_id: str
    feature: str
    owner_id: str
    duration_seconds: float


@dataclass(slots=True)
class _TaskHold:
    task: asyncio.Task
    feature: str
    lease: ExecutionLease | None = None
    failure: BaseException | None = None


_task_holds: ContextVar[tuple[_TaskHold, ...]] = ContextVar(
    "sakura_service_execution_holds", default=()
)


def current_service_execution_failure() -> BaseException | None:
    """Let the worker distinguish coordination loss before catching cancel.

    The heartbeat writes the shared hold before cancelling its owner. Child
    tasks inherit the context but must not observe another task's failure as
    their own cancellation reason.
    """
    task = asyncio.current_task()
    return next(
        (
            hold.failure
            for hold in reversed(_task_holds.get())
            if hold.task is task and hold.failure is not None
        ),
        None,
    )


async def has_live_service_execution_ownership(
    session, operation_id, *, instant=None, for_update=False
):
    """Read worker liveness without locking financial rows or execution gates."""
    statement = (
        select(ServiceExecutionOwnership.token)
        .where(
            ServiceExecutionOwnership.operation_id == operation_id,
            ServiceExecutionOwnership.state.in_(("queued", "running")),
            ServiceExecutionOwnership.expires_at > (instant or now_utc()),
        )
        .limit(1)
    )
    if for_update:
        # A recovery batch may have established an older REPEATABLE READ
        # snapshot. A locking/current read must observe the latest heartbeat.
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


async def _join_without_cancelling(task):
    """Observe a child result after any number of owner cancellation requests.

    Return whether the owner was interrupted so acquisition can retain its
    committed result for cleanup before propagating cancellation. Awaiting the
    child directly in an except block would let a second cancel interrupt it.
    """
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            # task.result() below observes and propagates the child exception.
            break
    return task.result(), interrupted


class _GateInsertConflict(Exception):
    """The other worker created the same feature's gate; retry the transaction."""


def _retryable_database_conflict(exc: DBAPIError) -> bool:
    original = exc.orig
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if code in {"40001", "40P01"}:
        return True
    args = getattr(original, "args", ())
    if args and args[0] in {1205, 1213}:  # MySQL lock timeout / deadlock.
        return True
    return "database is locked" in str(original).lower()


class ServiceExecutionCapacity:
    """Coordinate leases through the project's short-lived SQL sessions.

    Constructor overrides are for isolated integrations and tests. Production
    reads the feature maximum from app_config, with Settings as its default.
    Existing live slots drain normally after a maximum is reduced.
    """

    def __init__(
        self,
        *,
        session_factory=None,
        settings_provider=get_settings,
        max_concurrency: int | None = None,
        lease_seconds: float | None = None,
        poll_seconds: float | None = None,
        heartbeat_seconds: float | None = None,
        clock=now_utc,
    ):
        self.session_factory = session_factory
        self.settings_provider = settings_provider
        self.max_concurrency = max_concurrency
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.owner_id = f"pid:{os.getpid()}:{uuid4().hex}"

    def _timings(self, saved=None) -> tuple[float, float, float]:
        settings = self.settings_provider()
        saved = saved or {}
        lease_value = (
            self.lease_seconds
            if self.lease_seconds is not None
            else saved.get(
                "service_execution_lease_seconds",
                settings.service_execution_lease_seconds,
            )
        )
        poll_value = (
            self.poll_seconds
            if self.poll_seconds is not None
            else saved.get(
                "service_execution_poll_seconds",
                settings.service_execution_poll_seconds,
            )
        )
        try:
            if isinstance(lease_value, bool) or isinstance(poll_value, bool):
                raise ValueError("Boolean timing")
            lease, poll = float(lease_value), float(poll_value)
            heartbeat = self._heartbeat_period(lease)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ServiceExecutionCapacityError(
                "Invalid execution lease timing"
            ) from exc
        if (
            not all(
                math.isfinite(value) and value > 0 for value in (lease, poll, heartbeat)
            )
            or heartbeat >= lease
        ):
            raise ServiceExecutionCapacityError("Invalid execution lease timing")
        return lease, poll, heartbeat

    def _heartbeat_period(self, duration):
        if isinstance(self.heartbeat_seconds, bool):
            raise ValueError("Boolean heartbeat")
        return float(
            self.heartbeat_seconds
            if self.heartbeat_seconds is not None
            else duration / 3
        )

    async def _fresh_timings(self, session=None):
        if session is None:
            async with self._factory()() as fresh:
                return await self._fresh_timings(fresh)
        rows = await session.execute(
            select(AppConfig.key_name, AppConfig.key_value).where(
                AppConfig.key_name.in_(
                    (
                        "service_execution_lease_seconds",
                        "service_execution_poll_seconds",
                    )
                )
            )
        )
        return self._timings(dict(rows.all()))

    def _factory(self):
        factory = self.session_factory or database.async_session
        if factory is None:
            raise ServiceExecutionCapacityError(
                "Execution capacity database unavailable"
            )
        return factory

    async def _lock_gate(self, session, feature: str) -> None:
        result = await session.execute(
            update(ServiceExecutionGate)
            .where(ServiceExecutionGate.feature == feature)
            .values(revision=ServiceExecutionGate.revision + 1)
        )
        if result.rowcount == 0:
            session.add(ServiceExecutionGate(feature=feature, revision=1))
            try:
                await session.flush()
            except IntegrityError as exc:
                raise _GateInsertConflict from exc

    async def _transaction(self, feature, action):
        if feature not in FEATURE_CONFIG_KEYS:
            raise ValueError("Unsupported service execution feature")
        factory = self._factory()
        # Retry only transaction conflicts, never an unavailable database or an
        # arbitrary constraint failure. Three attempts bound lock contention.
        for attempt in range(3):
            try:
                async with factory() as session:
                    async with session.begin():
                        await self._lock_gate(session, feature)
                        return await action(session)
            except _GateInsertConflict:
                if attempt == 2:
                    raise ServiceExecutionCapacityError(
                        "Execution capacity admission conflicted"
                    ) from None
            except DBAPIError as exc:
                if not _retryable_database_conflict(exc) or attempt == 2:
                    raise ServiceExecutionCapacityError(
                        "Execution capacity database transaction failed"
                    ) from exc
            await asyncio.sleep(self._timings()[1])
        raise AssertionError("Unreachable capacity transaction")

    async def _maximum(self, session, feature) -> int:
        if self.max_concurrency is not None:
            value = self.max_concurrency
        else:
            key = FEATURE_CONFIG_KEYS[feature]
            result = await session.execute(
                select(AppConfig.key_value).where(AppConfig.key_name == key)
            )
            value = result.scalar_one_or_none()
            if value is None:
                value = getattr(self.settings_provider(), key)
        try:
            maximum = int(value)
        except (TypeError, ValueError) as exc:
            raise ServiceExecutionCapacityError(
                "Invalid service execution maximum"
            ) from exc
        if isinstance(value, bool) or maximum < 1 or str(value).strip() != str(maximum):
            raise ServiceExecutionCapacityError("Invalid service execution maximum")
        return maximum

    async def try_acquire(self, feature: str) -> ExecutionLease | None:
        context = get_billing_context()

        async def acquire(session):
            lease_seconds, _, _ = await self._fresh_timings(session)
            lease = ExecutionLease(uuid4().hex, feature, self.owner_id, lease_seconds)
            instant = self.clock()
            await session.execute(
                delete(ServiceExecutionLease).where(
                    ServiceExecutionLease.feature == feature,
                    ServiceExecutionLease.expires_at <= instant,
                )
            )
            maximum = await self._maximum(session, feature)
            result = await session.execute(
                select(func.count())
                .select_from(ServiceExecutionLease)
                .where(ServiceExecutionLease.feature == feature)
            )
            if result.scalar_one() >= maximum:
                return None
            session.add(
                ServiceExecutionLease(
                    token=lease.token,
                    feature=feature,
                    owner_id=lease.owner_id,
                    operation_id=context.operation_id if context else None,
                    user_id=context.user_id if context else None,
                    acquired_at=instant,
                    expires_at=instant + timedelta(seconds=lease_seconds),
                )
            )
            await session.flush()
            return lease

        return await self._transaction(feature, acquire)

    async def acquire_ownership(self, feature: str) -> ExecutionOwnership | None:
        context = get_billing_context()
        if context is None:
            return None

        async def acquire(session):
            duration, _, _ = await self._fresh_timings(session)
            instant = self.clock()
            ownership = ExecutionOwnership(
                uuid4().hex, context.operation_id, feature, self.owner_id, duration
            )
            session.add(
                ServiceExecutionOwnership(
                    token=ownership.token,
                    operation_id=ownership.operation_id,
                    feature=feature,
                    owner_id=self.owner_id,
                    user_id=context.user_id,
                    state="queued",
                    acquired_at=instant,
                    renewed_at=instant,
                    expires_at=instant + timedelta(seconds=duration),
                )
            )
            await session.flush()
            return ownership

        return await self._transaction(feature, acquire)

    async def renew_ownership(
        self, ownership: ExecutionOwnership, *, running=False
    ) -> bool:
        async def renewal(session):
            instant = self.clock()
            values = {
                "renewed_at": instant,
                "expires_at": instant + timedelta(seconds=ownership.duration_seconds),
            }
            if running:
                values["state"] = "running"
            result = await session.execute(
                update(ServiceExecutionOwnership)
                .where(
                    ServiceExecutionOwnership.token == ownership.token,
                    ServiceExecutionOwnership.operation_id == ownership.operation_id,
                    ServiceExecutionOwnership.feature == ownership.feature,
                    ServiceExecutionOwnership.owner_id == ownership.owner_id,
                    ServiceExecutionOwnership.state.in_(("queued", "running")),
                    ServiceExecutionOwnership.expires_at > instant,
                )
                .values(**values)
            )
            return result.rowcount == 1

        return await self._transaction(ownership.feature, renewal)

    async def release_ownership(self, ownership: ExecutionOwnership) -> None:
        async def removal(session):
            instant = self.clock()
            await session.execute(
                update(ServiceExecutionOwnership)
                .where(
                    ServiceExecutionOwnership.token == ownership.token,
                    ServiceExecutionOwnership.operation_id == ownership.operation_id,
                    ServiceExecutionOwnership.feature == ownership.feature,
                    ServiceExecutionOwnership.owner_id == ownership.owner_id,
                    ServiceExecutionOwnership.state.in_(("queued", "running")),
                )
                .values(state="released", renewed_at=instant, expires_at=instant)
            )

        await self._transaction(ownership.feature, removal)

    async def renew(self, lease: ExecutionLease) -> bool:
        # Keep the acquired lease's duration stable while it is running. A
        # shorter new configuration must not expire a live lease before its
        # already scheduled heartbeat; new tasks receive the new duration.
        lease_seconds = lease.duration_seconds

        async def renewal(session):
            instant = self.clock()
            result = await session.execute(
                update(ServiceExecutionLease)
                .where(
                    ServiceExecutionLease.token == lease.token,
                    ServiceExecutionLease.feature == lease.feature,
                    ServiceExecutionLease.owner_id == lease.owner_id,
                    ServiceExecutionLease.expires_at > instant,
                )
                .values(expires_at=instant + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1

        return await self._transaction(lease.feature, renewal)

    async def release(self, lease: ExecutionLease) -> None:
        async def removal(session):
            await session.execute(
                delete(ServiceExecutionLease).where(
                    ServiceExecutionLease.token == lease.token,
                    ServiceExecutionLease.feature == lease.feature,
                    ServiceExecutionLease.owner_id == lease.owner_id,
                )
            )

        await self._transaction(lease.feature, removal)

    @asynccontextmanager
    async def slot(self, feature: str, cancel_event=None):
        owner = asyncio.current_task()
        if owner is None:
            raise ServiceExecutionCapacityError("Execution slot needs an asyncio task")
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        hold = next(
            (
                hold
                for hold in _task_holds.get()
                if hold.task is owner and hold.feature == feature
            ),
            None,
        )
        if hold is not None:
            if hold.failure is not None:
                raise ServiceExecutionLeaseLost(
                    "Execution stopped because its capacity lease was lost"
                ) from hold.failure
            yield hold.lease
            return
        # A user's queued execution occupies their purchased admission headroom
        # before any shared worker slot or provider request is available.
        from backend.services.ai_usage_service import admit_billing_operation

        await admit_billing_operation(session_factory=self.session_factory)
        _, poll, _ = await self._fresh_timings()
        lease = None
        ownership = None
        heartbeat_stop = asyncio.Event()
        heartbeats = []
        hold_state = _TaskHold(owner, feature)

        async def keep_alive(resource, renew):
            duration = resource.duration_seconds
            heartbeat_period = self._heartbeat_period(duration)
            try:
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(
                            heartbeat_stop.wait(), timeout=heartbeat_period
                        )
                    except TimeoutError:
                        pass
                    else:
                        return
                    async with asyncio.timeout(duration / 3):
                        renewed = await renew(resource)
                    if not renewed:
                        raise ServiceExecutionLeaseLost(
                            "Execution capacity ownership or lease expired"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if hold_state.failure is None:
                    hold_state.failure = exc
                logger.error(
                    "Service execution ownership lost for {} token {}",
                    feature,
                    resource.token,
                )
                if not heartbeat_stop.is_set():
                    owner.cancel()

        token = _task_holds.set((*_task_holds.get(), hold_state))
        try:
            # Acquisition runs in an observed child task so cancellation during
            # commit cannot discard a newly persisted resource's fenced token.
            ownership, interrupted = await _join_without_cancelling(
                asyncio.create_task(self.acquire_ownership(feature))
            )
            if interrupted:
                raise asyncio.CancelledError
            if ownership is not None:
                heartbeats.append(
                    asyncio.create_task(keep_alive(ownership, self.renew_ownership))
                )
            while lease is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError
                lease, interrupted = await _join_without_cancelling(
                    asyncio.create_task(self.try_acquire(feature))
                )
                hold_state.lease = lease
                if interrupted:
                    raise asyncio.CancelledError
                if lease is None:
                    if cancel_event is None:
                        await asyncio.sleep(poll)
                    else:
                        with suppress(TimeoutError):
                            await asyncio.wait_for(cancel_event.wait(), timeout=poll)
            if ownership is not None:
                activated, interrupted = await _join_without_cancelling(
                    asyncio.create_task(self.renew_ownership(ownership, running=True))
                )
                if not activated:
                    raise ServiceExecutionLeaseLost(
                        "Execution ownership expired before dispatch"
                    )
                if interrupted:
                    raise asyncio.CancelledError
            # A queued owner and its newly acquired slot may have different
            # durations after an administrator changes TTL. Independent loops
            # keep both at the heartbeat cadence frozen for their own resource.
            heartbeats.append(asyncio.create_task(keep_alive(lease, self.renew)))
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            yield lease
        except asyncio.CancelledError:
            if hold_state.failure is not None:
                raise ServiceExecutionLeaseLost(
                    "Execution stopped because its capacity lease was lost"
                ) from hold_state.failure
            raise
        finally:
            # Stop the interval wait immediately, but let an in-flight renewal
            # finish its bounded short transaction. Cancelling async database
            # I/O during normal completion can invalidate the connection and
            # leak an unobserved rollback/close failure.
            heartbeat_stop.set()

            async def cleanup_resources():
                try:
                    results = await asyncio.gather(*heartbeats, return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
                finally:
                    try:
                        if lease is not None:
                            await self.release(lease)
                    finally:
                        if ownership is not None:
                            try:
                                # No gate/ownership transaction is held while
                                # financial rows are touched. Protect the short
                                # handoff to outer worker bill finalization.
                                from backend.services.ai_usage_service import (
                                    refresh_billing_admission_liveness,
                                )

                                await refresh_billing_admission_liveness(
                                    session_factory=self.session_factory
                                )
                            finally:
                                await self.release_ownership(ownership)

            # One independent cleanup task owns joining AND releasing, and is
            # never awaited without shield. Repeated cancels cannot skip release
            # or abandon a renewal/rollback exception. Process crashes retain
            # finite expiry as the recovery boundary.
            cleanup = asyncio.create_task(cleanup_resources())
            try:
                _, interrupted = await _join_without_cancelling(cleanup)
            finally:
                _task_holds.reset(token)
            if interrupted:
                if hold_state.failure is not None:
                    raise ServiceExecutionLeaseLost(
                        "Execution stopped because its capacity lease was lost"
                    ) from hold_state.failure
                raise asyncio.CancelledError
        if hold_state.failure is not None:
            raise ServiceExecutionLeaseLost(
                "Execution stopped because its capacity lease was lost"
            ) from hold_state.failure


@asynccontextmanager
async def service_execution_slot(feature: str, cancel_event=None):
    async with ServiceExecutionCapacity().slot(
        feature, cancel_event=cancel_event
    ) as lease:
        yield lease


class ServiceExecutionLimiter:
    """Async-with compatibility for workers previously using Semaphore."""

    def __init__(self, feature: str):
        if feature not in FEATURE_CONFIG_KEYS:
            raise ValueError("Unsupported service execution feature")
        self.feature = feature
        self._contexts: dict[asyncio.Task, list[Any]] = {}

    def slot(self, cancel_event=None):
        return service_execution_slot(self.feature, cancel_event=cancel_event)

    async def __aenter__(self):
        task = asyncio.current_task()
        context = self.slot()
        result = await context.__aenter__()
        self._contexts.setdefault(task, []).append(context)
        return result

    async def __aexit__(self, *args):
        task = asyncio.current_task()
        contexts = self._contexts[task]
        context = contexts.pop()
        if not contexts:
            del self._contexts[task]
        return await context.__aexit__(*args)
