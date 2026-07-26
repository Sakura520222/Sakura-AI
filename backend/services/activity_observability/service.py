"""Transactional writes for the activity observability domain."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationTrigger,
    ActivityInvocationWorkUnit,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
    ActivityThread,
    ActivityTrigger,
)
from backend.models.database import utc_now
from backend.services.activity_observability.contracts import (
    RoleConfigSnapshot,
    _SENSITIVE_SNAPSHOT_PATTERN,
)
from backend.services.activity_observability.event_service import append_lifecycle_event


class ConflictError(RuntimeError):
    """The requested write conflicts with already persisted domain state."""


_REQUIREMENTS = frozenset({"primary_required", "required", "best_effort", "detached"})
_WORK_UNIT_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "abandoned"}
)
_INVOCATION_TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled"}
)
_GATING_REQUIREMENTS = frozenset({"primary_required", "required"})
_FAILED_TERMINAL_STATUSES = frozenset({"failed", "cancelled", "abandoned"})
_ENDPOINT_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _identity_query(
    identity_values: dict[str, str], *, for_update: bool = False
):
    statement = select(ActivityResourceIdentity).where(
        *(
            getattr(ActivityResourceIdentity, field) == value
            for field, value in identity_values.items()
        )
    )
    return statement.with_for_update() if for_update else statement


def _session_query(
    identity_values: dict[str, str], *, for_update: bool = False
):
    statement = (
        select(ActivityObservabilitySession)
        .join(ActivityObservabilitySession.resource_identity)
        .options(contains_eager(ActivityObservabilitySession.resource_identity))
        .where(
            *(
                getattr(ActivityResourceIdentity, field) == value
                for field, value in identity_values.items()
            )
        )
    )
    return statement.with_for_update() if for_update else statement


def _trigger_query(dedupe_key: str, *, for_update: bool = False):
    statement = select(ActivityTrigger).where(ActivityTrigger.dedupe_key == dedupe_key)
    return statement.with_for_update() if for_update else statement


def _validate_safe_snapshot_text(
    field_name: str, value: object, *, optional: bool = False
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if _SENSITIVE_SNAPSHOT_PATTERN.search(value):
        raise ValueError(f"{field_name} contains unsafe data")
    return value


class ActivityObservabilityService:
    """Create and aggregate observability records in short transactions."""

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._db is not None:
            yield self._db
            return
        if db_module.async_session is None:
            raise RuntimeError("异步数据库会话尚未初始化")
        async with db_module.async_session() as db:
            yield db

    async def _commit_if_owned(self, db: AsyncSession) -> None:
        """Commit only sessions opened by this service.

        Injected sessions participate in the caller's transaction; flushing keeps
        generated identifiers available without taking ownership of commit/rollback.
        """
        if self._db is None:
            await db.commit()

    async def _refresh_if_owned_or_flush(
        self, db: AsyncSession, instance: object
    ) -> None:
        await db.flush()
        if self._db is None:
            await db.refresh(instance)

    async def get_or_create_session(
        self,
        *,
        source_system_instance: str,
        repository_external_id: str,
        resource_type: str,
        resource_number: int | str,
        repo_full_name: str,
    ) -> ActivityObservabilitySession:
        """Return the long-lived session for a normalized resource identity."""

        source_instance = _normalize_text(
            source_system_instance, "source_system_instance", lowercase=True
        )
        repository_id = _normalize_text(
            repository_external_id, "repository_external_id"
        )
        normalized_resource_type = _normalize_text(
            resource_type, "resource_type", lowercase=True
        )
        normalized_resource_number = _normalize_text(resource_number, "resource_number")
        normalized_repo_full_name = _normalize_text(repo_full_name, "repo_full_name")
        identity_values = {
            "source_system_instance": source_instance,
            "repository_external_id": repository_id,
            "resource_type": normalized_resource_type,
            "resource_number": normalized_resource_number,
        }

        async with self._session_scope() as db:
            existing = (
                await db.execute(_session_query(identity_values))
            ).unique().scalar_one_or_none()
            if existing is not None:
                if existing.resource_identity.repo_full_name != normalized_repo_full_name:
                    existing.resource_identity.repo_full_name = normalized_repo_full_name
                    await db.flush()
                    await self._commit_if_owned(db)
                return existing

            statement = _identity_query(identity_values)
            identity = (await db.execute(statement)).scalar_one_or_none()
            if identity is None:
                candidate_identity = ActivityResourceIdentity(
                    **identity_values, repo_full_name=normalized_repo_full_name
                )
                try:
                    async with db.begin_nested():
                        db.add(candidate_identity)
                        await db.flush()
                except IntegrityError:
                    identity = (
                        await db.execute(_identity_query(identity_values, for_update=True))
                    ).scalar_one_or_none()
                    if identity is None:
                        raise
                else:
                    identity = candidate_identity

            if identity.repo_full_name != normalized_repo_full_name:
                identity.repo_full_name = normalized_repo_full_name
                await db.flush()

            session = ActivityObservabilitySession(
                resource_identity_id=identity.id,
                session_kind=(
                    "ephemeral"
                    if normalized_resource_type == "ephemeral"
                    else "long_lived"
                ),
                status="open",
                session_event_sequence=0,
            )
            try:
                async with db.begin_nested():
                    db.add(session)
                    await db.flush()
            except IntegrityError:
                concurrent = (
                    await db.execute(_session_query(identity_values, for_update=True))
                ).unique().scalar_one_or_none()
                if concurrent is None:
                    raise
                if concurrent.resource_identity.repo_full_name != normalized_repo_full_name:
                    concurrent.resource_identity.repo_full_name = normalized_repo_full_name
                    await db.flush()
                await self._commit_if_owned(db)
                return concurrent
            await self._commit_if_owned(db)
            if self._db is None:
                await db.refresh(session)
            return session

    async def create_role_binding_snapshot(
        self, snapshot: RoleConfigSnapshot
    ) -> ActivityObservabilityRoleBindingSnapshot:
        """Persist one immutable, credential-free role configuration snapshot."""

        async with self._session_scope() as db:
            stored = self._build_role_binding_snapshot(snapshot)
            db.add(stored)
            await self._refresh_if_owned_or_flush(db, stored)
            await self._commit_if_owned(db)
            if self._db is None:
                await db.refresh(stored)
            return stored

    async def create_delivery_trigger(
        self,
        session_id: int,
        delivery_id: str,
        base_sha: str | None,
        head_sha: str | None,
        *,
        source_system_instance: str | None = None,
    ) -> ActivityTrigger:
        """Create an idempotent delivery trigger.

        ``source_system_instance`` is optional: when omitted it is derived from
        the session's resource identity so callers that already hold the
        session id do not have to thread the source instance through. When
        provided, it must match the derived value to avoid accidental
        cross-instance dedupe keys.
        """

        normalized_delivery_id = _normalize_ascii(delivery_id, "delivery_id")
        async with self._session_scope() as db:
            source_instance = await self._resolve_source_instance(
                db,
                session_id=session_id,
                source_system_instance=source_system_instance,
            )
            dedupe_key = f"{source_instance}:delivery:{normalized_delivery_id}"
            return await self._create_deduplicated_trigger(
                db,
                session_id=session_id,
                trigger_kind="delivery",
                dedupe_key=dedupe_key,
                source_delivery_id=normalized_delivery_id,
                base_sha=_normalize_optional_text(base_sha, "base_sha"),
                head_sha=_normalize_optional_text(head_sha, "head_sha"),
            )

    async def get_trigger_by_dedupe_key(
        self, dedupe_key: str
    ) -> ActivityTrigger | None:
        """Load a trigger by its idempotency key without reading legacy tables."""
        normalized = _normalize_ascii(dedupe_key, "dedupe_key")
        async with self._session_scope() as db:
            return (
                await db.execute(_trigger_query(normalized))
            ).scalar_one_or_none()

    async def create_comment_trigger(
        self,
        session_id: int,
        comment_id: str,
        purpose: str,
        *,
        base_sha: str | None = None,
        head_sha: str | None = None,
        actor_id: str | None = None,
        source_system_instance: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ActivityTrigger:
        """Create an idempotent trigger for a comment command."""
        normalized_comment_id = _normalize_ascii(comment_id, "comment_id")
        normalized_purpose = _normalize_ascii(purpose, "purpose")
        async with self._session_scope() as db:
            source_instance = await self._resolve_source_instance(
                db, session_id=session_id, source_system_instance=source_system_instance
            )
            dedupe_key = f"{source_instance}:comment:{normalized_comment_id}:{normalized_purpose}"
            return await self._create_deduplicated_trigger(
                db,
                session_id=session_id,
                trigger_kind="comment",
                dedupe_key=dedupe_key,
                source_comment_id=normalized_comment_id,
                actor_external_id=(
                    _normalize_ascii(actor_id, "actor_id") if actor_id else None
                ),
                base_sha=_normalize_optional_text(base_sha, "base_sha"),
                head_sha=_normalize_optional_text(head_sha, "head_sha"),
                metadata_json=(
                    json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                    if metadata else None
                ),
            )

    async def create_manual_trigger(
        self,
        session_id: int,
        actor_id: str,
        nonce: str,
        purpose: str,
    ) -> ActivityTrigger:
        """Create an idempotent manual trigger.

        The dedupe key is ``manual:{actor_id}:{nonce}`` so changing the
        human-readable ``purpose`` does not create a new trigger; ``purpose``
        is preserved in ``metadata_json`` for auditability.
        """

        normalized_actor_id = _normalize_ascii(actor_id, "actor_id")
        normalized_nonce = _normalize_ascii(nonce, "nonce")
        normalized_purpose = _normalize_ascii(purpose, "purpose")
        dedupe_key = f"manual:{normalized_actor_id}:{normalized_nonce}"
        async with self._session_scope() as db:
            return await self._create_deduplicated_trigger(
                db,
                session_id=session_id,
                trigger_kind="manual",
                dedupe_key=dedupe_key,
                actor_external_id=normalized_actor_id,
                metadata_json=json.dumps(
                    {"purpose": normalized_purpose},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )

    async def create_invocation(
        self, session_id: int, trigger_ids: Sequence[int]
    ) -> ActivityInvocation:
        """Consume pending triggers and merge them into one invocation."""

        normalized_trigger_ids = tuple(dict.fromkeys(trigger_ids))
        if not normalized_trigger_ids:
            raise ValueError("trigger_ids must not be empty")
        if len(normalized_trigger_ids) != len(trigger_ids):
            raise ValueError("trigger_ids must not contain duplicates")

        async with self._session_scope() as db:
            session = await db.get(
                ActivityObservabilitySession, session_id, with_for_update=True
            )
            if session is None:
                raise ValueError(
                    f"ActivityObservabilitySession not found: {session_id}"
                )

            triggers = (
                (
                    await db.execute(
                        select(ActivityTrigger)
                        .where(
                            ActivityTrigger.id.in_(normalized_trigger_ids),
                            ActivityTrigger.session_id == session_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            trigger_by_id = {trigger.id: trigger for trigger in triggers}
            if len(trigger_by_id) != len(normalized_trigger_ids):
                raise ValueError("one or more triggers do not belong to the session")
            ordered_triggers = [
                trigger_by_id[trigger_id] for trigger_id in normalized_trigger_ids
            ]
            consumed = [
                trigger.id
                for trigger in ordered_triggers
                if trigger.status != "pending"
            ]
            if consumed:
                raise ConflictError(
                    f"triggers already consumed or unavailable: {consumed}"
                )

            try:
                async with db.begin_nested():
                    invocation = ActivityInvocation(
                        session_id=session_id,
                        primary_work_unit_id=None,
                        status="queued",
                        current_phase="admitted",
                        base_sha=ordered_triggers[0].base_sha,
                        initial_head_sha=ordered_triggers[0].head_sha,
                        final_head_sha=ordered_triggers[-1].head_sha,
                    )
                    db.add(invocation)
                    await db.flush()

                    consumed_at = utc_now()
                    for trigger in ordered_triggers:
                        db.add(
                            ActivityInvocationTrigger(
                                invocation_id=invocation.id,
                                trigger_id=trigger.id,
                            )
                        )
                        trigger.status = "consumed"
                        trigger.consumed_at = consumed_at
                    session.last_invocation_id = invocation.id
                    session.last_active_at = consumed_at
                    await db.flush()
                    await append_lifecycle_event(
                        db,
                        session_id=session.id,
                        invocation_id=invocation.id,
                        event_type="invocation_started",
                        payload={
                            "status": invocation.status,
                            "phase": invocation.current_phase,
                        },
                    )
            except IntegrityError as exc:
                raise ConflictError(
                    "one or more triggers were already consumed"
                ) from exc
            await self._commit_if_owned(db)
            return invocation

    async def merge_invocation_triggers(
        self, invocation_id: int, trigger_ids: Sequence[int]
    ) -> ActivityInvocation:
        """Atomically attach pending triggers to an active invocation.

        The unique ``ActivityInvocationTrigger.trigger_id`` constraint remains
        the final single-consumer fence; the invocation lock makes head SHA
        aggregation deterministic for concurrent synchronize deliveries.
        """
        normalized_ids = tuple(dict.fromkeys(int(trigger_id) for trigger_id in trigger_ids))
        if not normalized_ids:
            raise ValueError("trigger_ids must not be empty")
        if len(normalized_ids) != len(tuple(trigger_ids)):
            raise ValueError("trigger_ids must not contain duplicates")
        async with self._session_scope() as db:
            invocation = await db.get(ActivityInvocation, invocation_id, with_for_update=True)
            if invocation is None:
                raise ValueError(f"ActivityInvocation not found: {invocation_id}")
            if invocation.status not in {"queued", "running"}:
                raise ConflictError(f"invocation {invocation_id} is terminal: {invocation.status}")
            rows = list((await db.execute(
                select(ActivityTrigger)
                .where(
                    ActivityTrigger.id.in_(normalized_ids),
                    ActivityTrigger.session_id == invocation.session_id,
                )
                .with_for_update()
            )).scalars().all())
            by_id = {row.id: row for row in rows}
            if len(by_id) != len(normalized_ids):
                raise ValueError("one or more triggers do not belong to invocation session")
            existing_ids = set((await db.execute(
                select(ActivityInvocationTrigger.trigger_id).where(
                    ActivityInvocationTrigger.invocation_id == invocation_id,
                    ActivityInvocationTrigger.trigger_id.in_(normalized_ids),
                )
            )).scalars().all())
            for trigger_id in normalized_ids:
                trigger = by_id[trigger_id]
                if trigger_id in existing_ids:
                    continue
                if trigger.status != "pending":
                    raise ConflictError(f"trigger {trigger_id} is already consumed or unavailable")
                db.add(ActivityInvocationTrigger(invocation_id=invocation_id, trigger_id=trigger_id))
                trigger.status = "consumed"
                trigger.consumed_at = utc_now()
            ordered = [by_id[item] for item in normalized_ids]
            if ordered:
                if invocation.base_sha is None:
                    invocation.base_sha = ordered[0].base_sha
                if invocation.initial_head_sha is None:
                    invocation.initial_head_sha = ordered[0].head_sha
                final = ordered[-1].head_sha
                if final is not None:
                    invocation.final_head_sha = final
            await db.flush()
            await append_lifecycle_event(
                db,
                session_id=invocation.session_id,
                invocation_id=invocation.id,
                event_type="triggers_merged",
                payload={
                    "status": invocation.status,
                    "phase": invocation.current_phase,
                },
            )
            await self._commit_if_owned(db)
            return invocation

    async def create_work_unit(
        self,
        invocation_id: int,
        purpose: str,
        requirement: str,
        is_primary: bool,
        role_snapshot: RoleConfigSnapshot,
        *,
        thread_id: int | None = None,
    ) -> ActivityInvocationWorkUnit:
        """Create a work unit and enforce one primary unit per invocation."""

        normalized_purpose = _normalize_text(purpose, "purpose")
        if not isinstance(role_snapshot, RoleConfigSnapshot):
            raise TypeError("role_snapshot must be a RoleConfigSnapshot")
        normalized_requirement = _normalize_text(
            requirement, "requirement", lowercase=True
        )
        if normalized_requirement not in _REQUIREMENTS:
            allowed = ", ".join(sorted(_REQUIREMENTS))
            raise ValueError(f"requirement must be one of: {allowed}")
        if is_primary != (normalized_requirement == "primary_required"):
            raise ValueError(
                "is_primary must equal (requirement == 'primary_required')"
            )

        async with self._session_scope() as db:
            invocation = await db.get(
                ActivityInvocation, invocation_id, with_for_update=True
            )
            if invocation is None:
                raise ValueError(f"ActivityInvocation not found: {invocation_id}")

            if (
                normalized_requirement != "detached"
                and invocation.status in _INVOCATION_TERMINAL_STATUSES
            ):
                raise ConflictError(
                    f"invocation {invocation_id} is terminal: {invocation.status}"
                )

            if is_primary:
                existing_primary = (
                    await db.execute(
                        select(ActivityInvocationWorkUnit)
                        .where(
                            ActivityInvocationWorkUnit.invocation_id == invocation_id,
                            ActivityInvocationWorkUnit.is_primary.is_(True),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing_primary is not None:
                    raise ConflictError(
                        f"invocation {invocation_id} already has a primary work unit"
                    )

            if thread_id is not None:
                thread = await db.get(ActivityThread, thread_id)
                if thread is None or thread.session_id != invocation.session_id:
                    raise ValueError("thread does not belong to the invocation session")

            stored_snapshot = self._build_role_binding_snapshot(role_snapshot)
            db.add(stored_snapshot)
            await db.flush()
            work_unit = ActivityInvocationWorkUnit(
                invocation_id=invocation_id,
                session_id=invocation.session_id,
                thread_id=thread_id,
                role_binding_snapshot_id=stored_snapshot.id,
                purpose=normalized_purpose,
                requirement=normalized_requirement,
                status="queued",
                current_phase="preparing_context",
                requested_provider=role_snapshot.requested_provider,
                requested_model=role_snapshot.requested_model,
                requested_thinking_mode=role_snapshot.requested_thinking_mode,
                is_primary=is_primary,
            )
            db.add(work_unit)
            await db.flush()
            if is_primary:
                invocation.primary_work_unit_id = work_unit.id
                invocation.current_phase = "preparing_context"
                invocation.primary_requested_provider = work_unit.requested_provider
                invocation.primary_requested_model = work_unit.requested_model
                invocation.primary_requested_thinking_mode = (
                    work_unit.requested_thinking_mode
                )
            await db.flush()
            await append_lifecycle_event(
                db,
                session_id=invocation.session_id,
                invocation_id=invocation.id,
                work_unit_id=work_unit.id,
                event_type="work_unit_created",
                payload={
                    "status": work_unit.status,
                    "work_unit_status": work_unit.status,
                    "phase": work_unit.current_phase,
                    "purpose": work_unit.purpose,
                },
            )
            await self._commit_if_owned(db)
            if self._db is None:
                await db.refresh(work_unit)
            return work_unit

    async def finish_work_unit(
        self,
        work_unit_id: int,
        status: str,
        error_message: str | None = None,
        final_provider: str | None = None,
        final_model: str | None = None,
        final_thinking_mode: str | None = None,
    ) -> None:
        """Finish one work unit and aggregate its invocation when eligible."""

        normalized_status = _normalize_text(status, "status", lowercase=True)
        if normalized_status not in _WORK_UNIT_TERMINAL_STATUSES:
            allowed = ", ".join(sorted(_WORK_UNIT_TERMINAL_STATUSES))
            raise ValueError(f"terminal status must be one of: {allowed}")

        async with self._session_scope() as db:
            # Lock the parent first to serialize all transitions for this invocation.
            work_unit_ref = await db.execute(
                select(ActivityInvocationWorkUnit.invocation_id).where(
                    ActivityInvocationWorkUnit.id == work_unit_id
                )
            )
            invocation_id = work_unit_ref.scalar_one_or_none()
            if invocation_id is None:
                raise ValueError(
                    f"ActivityInvocationWorkUnit not found: {work_unit_id}"
                )
            invocation = await db.get(
                ActivityInvocation, invocation_id, with_for_update=True
            )
            if invocation is None:
                raise ValueError(f"ActivityInvocation not found: {invocation_id}")
            work_unit = await db.get(
                ActivityInvocationWorkUnit, work_unit_id, with_for_update=True
            )
            if work_unit is None:
                raise ValueError(
                    f"ActivityInvocationWorkUnit not found: {work_unit_id}"
                )

            if work_unit.status in _WORK_UNIT_TERMINAL_STATUSES:
                same_payload = (
                    work_unit.status == normalized_status
                    and work_unit.error_message == error_message
                    and work_unit.final_provider == final_provider
                    and work_unit.final_model == final_model
                    and work_unit.final_thinking_mode == final_thinking_mode
                )
                if same_payload:
                    return
                raise ConflictError(
                    f"work_unit {work_unit_id} is already terminal: {work_unit.status}"
                )

            work_unit.status = normalized_status
            work_unit.current_phase = None
            work_unit.error_message = error_message
            work_unit.final_provider = final_provider
            work_unit.final_model = final_model
            work_unit.final_thinking_mode = final_thinking_mode
            finished_at = utc_now()
            work_unit.completed_at = finished_at
            if normalized_status == "cancelled":
                work_unit.cancelled_at = finished_at
            else:
                work_unit.cancelled_at = None

            if work_unit.is_primary:
                invocation.primary_final_provider = final_provider
                invocation.primary_final_model = final_model
                invocation.primary_final_thinking_mode = final_thinking_mode

            await db.flush()
            work_units = (
                (
                    await db.execute(
                        select(ActivityInvocationWorkUnit)
                        .where(
                            ActivityInvocationWorkUnit.invocation_id == invocation.id
                        )
                        .order_by(ActivityInvocationWorkUnit.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            self._aggregate_invocation(invocation, work_units, finished_at)
            if invocation.status in _INVOCATION_TERMINAL_STATUSES:
                invocation.current_phase = None
            await db.flush()
            await append_lifecycle_event(
                db,
                session_id=invocation.session_id,
                invocation_id=invocation.id,
                work_unit_id=work_unit.id,
                event_type="work_unit_finished",
                payload={
                    "status": invocation.status,
                    "work_unit_status": work_unit.status,
                    "phase": invocation.current_phase,
                    "purpose": work_unit.purpose,
                },
            )
            await self._commit_if_owned(db)

    async def get_invocation(self, invocation_id: int) -> ActivityInvocation:
        """Load one invocation by primary key."""

        async with self._session_scope() as db:
            invocation = await db.get(ActivityInvocation, invocation_id)
            if invocation is None:
                raise ValueError(f"ActivityInvocation not found: {invocation_id}")
            return invocation

    async def get_work_unit(self, work_unit_id: int) -> ActivityInvocationWorkUnit:
        """Load one work unit by primary key."""

        async with self._session_scope() as db:
            work_unit = await db.get(ActivityInvocationWorkUnit, work_unit_id)
            if work_unit is None:
                raise ValueError(
                    f"ActivityInvocationWorkUnit not found: {work_unit_id}"
                )
            return work_unit

    @staticmethod
    async def _find_identity(
        db: AsyncSession,
        identity_values: dict[str, str],
        *,
        for_update: bool = False,
    ) -> ActivityResourceIdentity | None:
        return (await db.execute(_identity_query(identity_values, for_update=for_update))).scalar_one_or_none()

    @staticmethod
    async def _find_session(
        db: AsyncSession,
        identity_values: dict[str, str],
        *,
        for_update: bool = False,
    ) -> ActivityObservabilitySession | None:
        return (await db.execute(_session_query(identity_values, for_update=for_update))).unique().scalar_one_or_none()

    @staticmethod
    async def _resolve_source_instance(
        db: AsyncSession,
        *,
        session_id: int,
        source_system_instance: str | None,
    ) -> str:
        """Return the canonical source instance for ``session_id``.

        When ``source_system_instance`` is provided it must match the value
        derived from the session's resource identity; otherwise callers could
        silently build dedupe keys under the wrong instance.
        """

        derived = (
            await db.execute(
                select(ActivityResourceIdentity.source_system_instance)
                .join(
                    ActivityObservabilitySession,
                    ActivityObservabilitySession.resource_identity_id
                    == ActivityResourceIdentity.id,
                )
                .where(ActivityObservabilitySession.id == session_id)
            )
        ).scalar_one_or_none()
        if derived is None:
            raise ValueError(f"ActivityObservabilitySession not found: {session_id}")
        if source_system_instance is None:
            return derived
        normalized = _normalize_ascii(
            source_system_instance,
            "source_system_instance",
            lowercase=True,
        )
        if normalized != derived:
            raise ValueError(
                "source_system_instance must match the session's "
                f"resource identity: expected {derived!r}, got {normalized!r}"
            )
        return derived

    async def _create_deduplicated_trigger(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        trigger_kind: str,
        dedupe_key: str,
        **values: object,
    ) -> ActivityTrigger:
        existing = await db.execute(_trigger_query(dedupe_key))
        existing = existing.scalar_one_or_none()
        if existing is not None:
            if existing.session_id != session_id:
                raise ConflictError(
                    "trigger dedupe_key belongs to a different session: "
                    f"{existing.session_id} != {session_id}"
                )
            if self._db is None:
                await db.refresh(existing)
            return existing
        if await db.get(ActivityObservabilitySession, session_id) is None:
            raise ValueError(f"ActivityObservabilitySession not found: {session_id}")

        trigger = ActivityTrigger(
            session_id=session_id,
            trigger_kind=trigger_kind,
            status="pending",
            dedupe_key=dedupe_key,
            **values,
        )
        try:
            async with db.begin_nested():
                db.add(trigger)
                await db.flush()
        except IntegrityError:
            concurrent = (
                await db.execute(_trigger_query(dedupe_key, for_update=True))
            ).scalar_one_or_none()
            if concurrent is None:
                raise
            if concurrent.session_id != session_id:
                raise ConflictError(
                    "trigger dedupe_key belongs to a different session: "
                    f"{concurrent.session_id} != {session_id}"
                )
            if self._db is None:
                await db.refresh(concurrent)
            return concurrent
        await self._commit_if_owned(db)
        if self._db is None:
            await db.refresh(trigger)
        return trigger

    @staticmethod
    def _contains_sensitive_candidate_data(value: object) -> bool:
        sensitive_key = re.compile(
            r"(?:secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)",
            re.IGNORECASE,
        )
        if isinstance(value, dict):
            return any(
                sensitive_key.search(str(key))
                or ActivityObservabilityService._contains_sensitive_candidate_data(item)
                for key, item in value.items()
            )
        if isinstance(value, (tuple, list)):
            return any(
                ActivityObservabilityService._contains_sensitive_candidate_data(item)
                for item in value
            )
        return bool(
            _SENSITIVE_SNAPSHOT_PATTERN.search(str(value))
        )

    @staticmethod
    def _build_role_binding_snapshot(
        snapshot: RoleConfigSnapshot,
    ) -> ActivityObservabilityRoleBindingSnapshot:
        if not _ENDPOINT_FINGERPRINT_PATTERN.fullmatch(snapshot.endpoint_fingerprint):
            raise ValueError(
                "endpoint_fingerprint must be a lowercase 64-character SHA-256 hex digest"
            )
        field_values = {
            "role": _validate_safe_snapshot_text("role", snapshot.role),
            "requested_provider": _validate_safe_snapshot_text(
                "requested_provider", snapshot.requested_provider
            ),
            "requested_model": _validate_safe_snapshot_text(
                "requested_model", snapshot.requested_model
            ),
            "requested_thinking_mode": _validate_safe_snapshot_text(
                "requested_thinking_mode", snapshot.requested_thinking_mode, optional=True
            ),
            "account_id": _validate_safe_snapshot_text("account_id", snapshot.account_id),
            "protocol_family": _validate_safe_snapshot_text(
                "protocol_family", snapshot.protocol_family
            ),
        }
        if ActivityObservabilityService._contains_sensitive_candidate_data(
            snapshot.candidate_chain
        ):
            raise ValueError("candidate_chain contains sensitive endpoint or credential data")
        candidate_chain_json = json.dumps(
            snapshot.candidate_chain,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ActivityObservabilityRoleBindingSnapshot(
            role=field_values["role"],
            requested_provider=field_values["requested_provider"],
            requested_model=field_values["requested_model"],
            requested_thinking_mode=field_values["requested_thinking_mode"],
            candidate_chain_json=candidate_chain_json,
            account_id=field_values["account_id"],
            protocol_family=field_values["protocol_family"],
            endpoint_fingerprint=snapshot.endpoint_fingerprint,
            config_snapshot_version=snapshot.config_snapshot_version,
            captured_at=snapshot.captured_at.replace(tzinfo=None),
        )

    @staticmethod
    def _aggregate_invocation(
        invocation: ActivityInvocation,
        work_units: Sequence[ActivityInvocationWorkUnit],
        finished_at,
    ) -> None:
        # Once the invocation has reached a terminal state, completing more
        # work units (including detached ones) must not rewrite the terminal
        # status, completed_at, or cancelled_at.
        if invocation.status in _INVOCATION_TERMINAL_STATUSES:
            return

        participating = [unit for unit in work_units if unit.requirement != "detached"]
        if not participating or any(
            unit.status not in _WORK_UNIT_TERMINAL_STATUSES for unit in participating
        ):
            return

        primary_units = [unit for unit in participating if unit.is_primary]
        if len(primary_units) != 1:
            return

        gating = [
            unit for unit in participating if unit.requirement in _GATING_REQUIREMENTS
        ]
        if gating and all(
            unit.status == "cancelled" and unit.started_at is None for unit in gating
        ):
            invocation.status = "cancelled"
            invocation.cancelled_at = finished_at
            invocation.completed_at = None
            return
        if any(unit.status in _FAILED_TERMINAL_STATUSES for unit in gating):
            invocation.status = "failed"
        elif any(
            unit.requirement == "best_effort"
            and unit.status in _FAILED_TERMINAL_STATUSES
            for unit in participating
        ):
            invocation.status = "partial"
        else:
            invocation.status = "completed"
        invocation.completed_at = finished_at
        invocation.cancelled_at = None


def _normalize_text(value: object, field: str, *, lowercase: bool = False) -> str:
    if value is None:
        raise ValueError(f"{field} must not be empty")
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized.lower() if lowercase else normalized


def _normalize_optional_text(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, field)


def _normalize_ascii(value: object, field: str, *, lowercase: bool = False) -> str:
    normalized = _normalize_text(value, field, lowercase=lowercase)
    if not normalized.isascii():
        raise ValueError(f"{field} must contain ASCII characters only")
    return normalized
