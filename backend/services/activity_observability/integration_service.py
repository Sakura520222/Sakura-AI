"""Business admission and orchestration boundary for activity observability.

The worker-facing methods in this module deliberately operate on the new
observability tables only.  Legacy ``activity_*`` checkpoint/event tables are
not consulted and are not written here.
"""

from __future__ import annotations

import hashlib
import json
import re
from builtins import BaseExceptionGroup, ExceptionGroup
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityObservabilitySession,
    ActivityThread,
    ActivityThreadLease,
    ActivityTrigger,
)
from backend.models.database import utc_now
from backend.services.activity_observability.context_service import (
    ContextService,
    ThreadLeaseToken,
)
from backend.services.activity_observability.contracts import (
    InvocationContext,
    RoleConfigSnapshot,
)
from backend.services.activity_observability.observer import ObservedModelSender
from backend.services.activity_observability.attempt_service import AttemptService
from backend.services.activity_observability.legacy_scope_authorizer import (
    LegacyRepositoryScopeAuthorizer,
)
from backend.services.activity_observability.publication_service import (
    PublicationService,
    WorkUnitResultCoordinator,
)
from backend.services.activity_observability.service import ActivityObservabilityService


_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,}$")


class AdmissionError(ValueError):
    """Raised when an external event cannot be safely admitted."""


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    """Validated immutable resource identity plus current display metadata."""

    source_system_instance: str
    repository_external_id: str
    repo_full_name: str
    resource_type: str
    resource_number: str
    task_id: str | None = None

    @property
    def number(self) -> int | str:
        return (
            int(self.resource_number)
            if self.resource_number.isdigit()
            else self.resource_number
        )


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Result of admitting one external/manual event."""

    session: ActivityObservabilitySession
    trigger: ActivityTrigger
    duplicate: bool

    @property
    def session_id(self) -> int:
        return int(self.session.id)

    @property
    def trigger_id(self) -> int:
        return int(self.trigger.id)


@dataclass(frozen=True, slots=True)
class ReviewStartResult:
    """Invocation and reviewer lane selected for one admitted PR run."""

    session: ActivityObservabilitySession
    invocation: ActivityInvocation
    triggers: tuple[ActivityTrigger, ...]
    thread: ActivityThread
    work_unit: ActivityInvocationWorkUnit
    lease: ThreadLeaseToken
    merged: bool

    @property
    def invocation_id(self) -> int:
        return int(self.invocation.id)


@dataclass(frozen=True, slots=True)
class ObservedExecutionBundle:
    """Stable worker execution handle for one admitted model lane.

    The bundle deliberately contains scalar identifiers and short-lived service
    dependencies rather than relying on lazy-loaded ORM relationships after the
    admission session closes.  Workers pass ``invocation_context`` and
    ``observer`` to every real model call, and use ``finish`` in all terminal
    paths before releasing the thread lease.
    """

    session: ActivityObservabilitySession
    invocation: ActivityInvocation
    work_unit: ActivityInvocationWorkUnit
    thread: ActivityThread | None
    lease: ThreadLeaseToken | None
    revision_id: int | None
    merged: bool
    invocation_context: InvocationContext
    observer: ObservedModelSender
    publication_service: PublicationService
    publication_coordinator: Any
    observability: ActivityObservabilityService
    context_service: ContextService
    attempt_service: AttemptService
    tool_service: Any
    _finish_work_unit_succeeded: bool = field(
        default=False, init=False, repr=False, compare=False
    )
    _lease_released: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def invocation_id(self) -> int:
        return int(self.invocation.id)

    @property
    def work_unit_id(self) -> int:
        return int(self.work_unit.id)

    async def finish(self, status: str, *, error_message: str | None = None) -> None:
        """Persist terminal state and always attempt to release the thread lease.

        ``finish_work_unit`` and ``release_lease`` use separate transactions when
        the bundle owns its database session.  A failure in either operation must
        not prevent the other operation from running: the first failure is
        re-raised, while two failures are reported together so callers can retry
        without losing the partial-success information.
        """
        errors: list[BaseException] = []
        if not getattr(self, "_finish_work_unit_succeeded", False):
            try:
                await self.observability.finish_work_unit(
                    self.work_unit_id,
                    status,
                    error_message=error_message,
                )
                object.__setattr__(self, "_finish_work_unit_succeeded", True)
            except BaseException as exc:
                errors.append(exc)

        if self.lease is not None and not getattr(self, "_lease_released", False):
            try:
                await self.context_service.release_lease(self.lease)
                object.__setattr__(self, "_lease_released", True)
            except BaseException as exc:
                errors.append(exc)

        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]
        if any(
            isinstance(error, BaseException) and not isinstance(error, Exception)
            for error in errors
        ):
            raise BaseExceptionGroup(
                "execution finish and lease release failed", errors
            )
        raise ExceptionGroup("execution finish and lease release failed", errors)


RoleSnapshotResolver = Callable[[str], Awaitable[RoleConfigSnapshot]]


class ActivityIntegrationService:
    """Admission/orchestration facade shared by webhook and workers.

    ``db`` can be injected by a webhook/worker transaction.  In that mode all
    writes are flushed but the caller owns commit/rollback.  This is important
    because a queue row, trigger and outbox/event write may otherwise cross
    independent sessions and be observed as partially committed state.
    """

    def __init__(
        self,
        db: AsyncSession | None = None,
        *,
        role_snapshot_resolver: RoleSnapshotResolver | None = None,
        lease_context: ContextService | None = None,
    ) -> None:
        self._db = db
        self._role_snapshot_resolver = role_snapshot_resolver
        self._lease_context = lease_context

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
        if self._db is None:
            await db.commit()

    @staticmethod
    def normalize_resource(
        resource: Mapping[str, Any], *, resource_type: str | None = None
    ) -> NormalizedResource:
        """Validate provider identity; never manufacture identity from owner/name."""
        data = dict(resource)
        kind = (
            str(resource_type or data.get("resource_type") or data.get("type") or "")
            .strip()
            .lower()
        )
        if kind in {"scan", "repo_scan", "repository_scan"}:
            task_id = data.get("task_id") or data.get("scan_id")
            if task_id is None or not str(task_id).strip():
                raise AdmissionError("scan admission requires task_id or scan_id")
            task = str(task_id).strip()
            return NormalizedResource(
                source_system_instance="internal:ephemeral",
                repository_external_id=f"scan-task:{task}",
                repo_full_name=str(
                    data.get("repo_full_name") or data.get("repository") or task
                ),
                resource_type="ephemeral",
                resource_number=task,
                task_id=task,
            )

        if kind not in {"pr", "issue"}:
            raise AdmissionError("resource_type must be pr, issue, or scan")
        source = (
            data.get("source_system_instance")
            or data.get("source_host")
            or data.get("host")
        )
        repo_id = (
            data.get("repository_external_id")
            or data.get("repository_id")
            or data.get("repo_id")
            or data.get("repo_external_id")
        )
        full_name = data.get("repo_full_name") or data.get("repository_full_name")
        number = data.get("resource_number")
        if number is None:
            number = data.get("pr_number") if kind == "pr" else data.get("issue_number")
        if not isinstance(source, str) or not source.strip():
            raise AdmissionError("source host/system instance is required")
        source = source.strip().lower()
        if source.startswith("https://") or source.startswith("http://"):
            source = source.split("://", 1)[1].split("/", 1)[0]
        if not _HOST_RE.fullmatch(source):
            raise AdmissionError("source host/system instance is invalid")
        if repo_id is None or not str(repo_id).strip():
            raise AdmissionError(
                "immutable repository external ID is required; owner/name cannot be used as identity"
            )
        if (
            not isinstance(full_name, str)
            or not full_name.strip()
            or "/" not in full_name
        ):
            raise AdmissionError("repo_full_name is required")
        if number is None or not str(number).strip():
            raise AdmissionError("resource number is required")
        normalized_number = str(number).strip()
        if normalized_number.startswith("-"):
            raise AdmissionError("resource number must be positive")
        return NormalizedResource(
            source_system_instance=source,
            repository_external_id=str(repo_id).strip(),
            repo_full_name=full_name.strip(),
            resource_type=kind,
            resource_number=normalized_number,
        )

    async def admit(
        self,
        resource: Mapping[str, Any],
        *,
        trigger_kind: str,
        purpose: str = "review",
        resource_type: str | None = None,
        delivery_id: str | None = None,
        comment_id: str | None = None,
        actor_id: str | None = None,
        manual_nonce: str | None = None,
        base_sha: str | None = None,
        head_sha: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AdmissionResult:
        """Validate, get/create the resource Session, and create one Trigger."""
        kind = trigger_kind.strip().lower()
        if not kind:
            raise AdmissionError("trigger_kind is required")
        if resource_type is None and kind in {
            "synchronize",
            "reopen",
            "comment",
            "review_comment",
            "manual",
            "manual_review",
            "manual_analyze",
        }:
            resource_type = "pr"
        normalized = self.normalize_resource(resource, resource_type=resource_type)
        async with self._session_scope() as db:
            obs = ActivityObservabilityService(db=db)
            session = await obs.get_or_create_session(
                source_system_instance=normalized.source_system_instance,
                repository_external_id=normalized.repository_external_id,
                resource_type=normalized.resource_type,
                resource_number=normalized.number,
                repo_full_name=normalized.repo_full_name,
            )
            duplicate = False
            if kind in {"synchronize", "reopen", "delivery", "issue", "scan"}:
                if not delivery_id:
                    raise AdmissionError(f"{kind} admission requires delivery_id")
                dedupe_key = f"{normalized.source_system_instance}:delivery:{str(delivery_id).strip()}"
                duplicate = await obs.get_trigger_by_dedupe_key(dedupe_key) is not None
                trigger = await obs.create_delivery_trigger(
                    session.id,
                    delivery_id,
                    base_sha,
                    head_sha,
                    source_system_instance=normalized.source_system_instance,
                )
            elif kind in {"comment", "review_comment"}:
                if not comment_id:
                    raise AdmissionError("comment admission requires comment_id")
                dedupe_key = f"{normalized.source_system_instance}:comment:{str(comment_id).strip()}:{str(purpose).strip()}"
                duplicate = await obs.get_trigger_by_dedupe_key(dedupe_key) is not None
                trigger = await obs.create_comment_trigger(
                    session.id,
                    comment_id,
                    purpose,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    actor_id=actor_id,
                    source_system_instance=normalized.source_system_instance,
                    metadata=metadata,
                )
            elif kind in {"manual", "manual_review", "manual_analyze"}:
                if not actor_id or not manual_nonce:
                    raise AdmissionError("manual admission requires actor_id and nonce")
                dedupe_key = (
                    f"manual:{str(actor_id).strip()}:{str(manual_nonce).strip()}"
                )
                duplicate = await obs.get_trigger_by_dedupe_key(dedupe_key) is not None
                trigger = await obs.create_manual_trigger(
                    session.id, actor_id, manual_nonce, purpose
                )
            else:
                raise AdmissionError(f"unsupported trigger_kind: {trigger_kind}")
            await self._commit_if_owned(db)
            return AdmissionResult(session, trigger, duplicate)

    async def admit_synchronize(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(resource, trigger_kind="synchronize", **kwargs)

    async def admit_comment(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(resource, trigger_kind="comment", **kwargs)

    async def admit_reopen(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(resource, trigger_kind="reopen", **kwargs)

    async def admit_manual(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(resource, trigger_kind="manual", **kwargs)

    async def admit_issue(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(
            resource, trigger_kind="issue", resource_type="issue", **kwargs
        )

    async def admit_scan(
        self, resource: Mapping[str, Any], **kwargs: Any
    ) -> AdmissionResult:
        return await self.admit(
            resource, trigger_kind="scan", resource_type="scan", **kwargs
        )

    async def start_or_merge_review(
        self,
        resource: Mapping[str, Any] | None = None,
        *,
        session_id: int | None = None,
        trigger_ids: Sequence[int] | None = None,
        role_snapshot: RoleConfigSnapshot | None = None,
        role: str = "reviewer",
        task_type: str = "pr",
        task_id: int | None = None,
        lease_ttl: Any = None,
    ) -> ReviewStartResult:
        """Consume pending PR triggers, merging only under the active lease.

        The session lock and unique trigger-consumer constraint make this safe
        against two workers racing to consume the same synchronize delivery.
        """
        async with self._session_scope() as db:
            if session_id is None:
                if resource is None:
                    raise AdmissionError("resource or session_id is required")
                normalized = self.normalize_resource(resource, resource_type="pr")
                obs = ActivityObservabilityService(db=db)
                session = await obs.get_or_create_session(
                    source_system_instance=normalized.source_system_instance,
                    repository_external_id=normalized.repository_external_id,
                    resource_type=normalized.resource_type,
                    resource_number=normalized.number,
                    repo_full_name=normalized.repo_full_name,
                )
            else:
                session = await db.get(
                    ActivityObservabilitySession, session_id, with_for_update=True
                )
                if session is None:
                    raise AdmissionError(
                        f"ActivityObservabilitySession not found: {session_id}"
                    )
            # Lock parent before selecting pending triggers or active invocations.
            session = await db.get(
                ActivityObservabilitySession, session.id, with_for_update=True
            )
            selected = await self._select_pending_triggers(db, session.id, trigger_ids)
            if not selected:
                raise AdmissionError("no pending triggers available for review")

            active = await self._active_review_with_live_lease(db, session.id, role)
            obs = ActivityObservabilityService(db=db)
            if active is not None:
                invocation, thread, work_unit, _lease = active
                await obs.merge_invocation_triggers(
                    invocation.id, [t.id for t in selected]
                )
                refreshed = await db.get(ActivityInvocation, invocation.id)
                selected = await self._load_triggers(db, [t.id for t in selected])
                await self._commit_if_owned(db)
                return ReviewStartResult(
                    session=session,
                    invocation=refreshed or invocation,
                    triggers=tuple(selected),
                    thread=thread,
                    work_unit=work_unit,
                    lease=_lease,
                    merged=True,
                )

            invocation = await obs.create_invocation(
                session.id, [t.id for t in selected]
            )
            if task_type:
                invocation.task_type = task_type
            invocation.task_id = task_id
            thread = await self._ensure_thread(db, session.id, role)
            snapshot = role_snapshot or await self._resolve_snapshot(role)
            work_unit = await obs.create_work_unit(
                invocation.id,
                purpose=role,
                requirement="primary_required",
                is_primary=True,
                role_snapshot=snapshot,
                thread_id=thread.id,
            )
            context = self._lease_context or ContextService(db=db)
            lease = await context.acquire_lease(thread.id, work_unit.id, ttl=lease_ttl)
            if thread.current_revision_id is None:
                await context.create_revision(
                    thread.id,
                    lease,
                    expected_parent_revision_id=None,
                    message_manifest=[],
                    reason="initial",
                    created_invocation_id=invocation.id,
                    created_work_unit_id=work_unit.id,
                )
                thread = await db.get(ActivityThread, thread.id) or thread
            await db.flush()
            await self._commit_if_owned(db)
            return ReviewStartResult(
                session=session,
                invocation=invocation,
                triggers=tuple(selected),
                thread=thread,
                work_unit=work_unit,
                lease=lease,
                merged=False,
            )

    async def start_or_merge_issue(
        self, *args: Any, **kwargs: Any
    ) -> ReviewStartResult:
        kwargs.setdefault("role", "issue_analyzer")
        kwargs.setdefault("task_type", "issue")
        return await self.start_or_merge_review(*args, **kwargs)

    async def start_scan(
        self,
        resource: Mapping[str, Any],
        *,
        trigger_ids: Sequence[int] | None = None,
        role_snapshot: RoleConfigSnapshot | None = None,
        role: str = "scan",
        task_id: int | None = None,
    ) -> tuple[
        ActivityObservabilitySession,
        ActivityInvocation,
        ActivityInvocationWorkUnit,
        tuple[ActivityTrigger, ...],
    ]:
        """Start an ephemeral scan invocation with an unthreaded work unit."""
        admitted = await self.admit_scan(
            resource,
            delivery_id=str(
                resource.get("delivery_id") or resource.get("task_id") or task_id
            ),
            head_sha=resource.get("head_sha"),
        )
        async with self._session_scope() as db:
            session = await db.get(
                ActivityObservabilitySession, admitted.session_id, with_for_update=True
            )
            if session is None:
                raise AdmissionError("scan session disappeared")
            selected = await self._select_pending_triggers(
                db, session.id, trigger_ids or [admitted.trigger_id]
            )
            obs = ActivityObservabilityService(db=db)
            invocation = await obs.create_invocation(
                session.id, [t.id for t in selected]
            )
            invocation.task_type = "scan"
            invocation.task_id = task_id
            snapshot = role_snapshot or await self._resolve_snapshot(role)
            work_unit = await obs.create_work_unit(
                invocation.id, role, "primary_required", True, snapshot, thread_id=None
            )
            await db.flush()
            await self._commit_if_owned(db)
            return session, invocation, work_unit, tuple(selected)

    async def build_execution_bundle(
        self,
        started: ReviewStartResult,
        *,
        publication_coordinator: Any = None,
        role_snapshot: RoleConfigSnapshot | None = None,
    ) -> ObservedExecutionBundle:
        """Bind the admitted lane to context/attempt/publication dependencies."""
        snapshot = role_snapshot or await self._resolve_snapshot_from_work_unit(
            started.work_unit
        )
        context = InvocationContext(
            invocation_id=int(started.invocation.id),
            work_unit_id=int(started.work_unit.id),
            thread_id=int(started.thread.id) if started.thread is not None else None,
            role_snapshot=snapshot,
        )
        # Each dependency shares the currently injected DB session where one is
        # available; otherwise each operation opens its own short-lived session.
        attempts = AttemptService(db=self._db)
        context_service = self._lease_context or ContextService(db=self._db)
        revision_resolver = None
        if started.thread is not None:
            thread_id = int(started.thread.id)

            async def revision_resolver() -> int | None:
                return await context_service.context_revision_for_next_attempt(
                    thread_id
                )

        from backend.services.activity_observability.tool_service import (
            DefaultArtifactEncryptionProvider,
            ToolService,
        )

        tool_service = ToolService(
            db=self._db,
            encryption_provider=DefaultArtifactEncryptionProvider(),
        )
        observer = ObservedModelSender(
            attempts,
            context=context,
            revision_resolver=revision_resolver,
            context_service=context_service,
            tool_service=tool_service,
            lease=started.lease,
        )
        publication = PublicationService(
            db=self._db,
            recipient_resolver=LegacyRepositoryScopeAuthorizer(),
        )
        effective_publication_coordinator = (
            publication_coordinator
            if publication_coordinator is not None
            else WorkUnitResultCoordinator(publication)
        )
        revision_id = (
            int(started.lease.base_revision_id)
            if started.lease is not None and started.lease.base_revision_id is not None
            else (
                int(started.thread.current_revision_id)
                if started.thread and started.thread.current_revision_id
                else None
            )
        )
        return ObservedExecutionBundle(
            session=started.session,
            invocation=started.invocation,
            work_unit=started.work_unit,
            thread=started.thread,
            lease=started.lease,
            revision_id=revision_id,
            merged=started.merged,
            invocation_context=context,
            observer=observer,
            publication_service=publication,
            publication_coordinator=effective_publication_coordinator,
            observability=ActivityObservabilityService(db=self._db),
            context_service=context_service,
            attempt_service=attempts,
            tool_service=tool_service,
        )

    async def _resolve_snapshot_from_work_unit(
        self, work_unit: ActivityInvocationWorkUnit
    ) -> RoleConfigSnapshot:
        """Return the immutable snapshot used to create a Work Unit."""
        stored = getattr(work_unit, "role_binding_snapshot", None)
        if stored is not None:
            try:
                candidates = tuple(
                    tuple(item) for item in json.loads(stored.candidate_chain_json)
                )
                return RoleConfigSnapshot(
                    role=stored.role,
                    requested_provider=stored.requested_provider,
                    requested_model=stored.requested_model,
                    requested_thinking_mode=stored.requested_thinking_mode,
                    candidate_chain=candidates,
                    account_id=stored.account_id,
                    protocol_family=stored.protocol_family,
                    endpoint_fingerprint=stored.endpoint_fingerprint,
                    config_snapshot_version=stored.config_snapshot_version,
                    captured_at=stored.captured_at.replace(tzinfo=timezone.utc),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if self._role_snapshot_resolver is not None:
            return await self._role_snapshot_resolver(str(work_unit.purpose))
        return await self._resolve_snapshot(str(work_unit.purpose))

    async def start_execution(
        self,
        resource: Mapping[str, Any] | None = None,
        *,
        session_id: int | None = None,
        trigger_ids: Sequence[int] | None = None,
        role_snapshot: RoleConfigSnapshot | None = None,
        role: str = "reviewer",
        task_type: str = "pr",
        task_id: int | None = None,
        lease_ttl: Any = None,
        publication_coordinator: Any = None,
    ) -> ObservedExecutionBundle:
        """Start/merge a threaded execution and return its complete bundle."""
        started = await self.start_or_merge_review(
            resource,
            session_id=session_id,
            trigger_ids=trigger_ids,
            role_snapshot=role_snapshot,
            role=role,
            task_type=task_type,
            task_id=task_id,
            lease_ttl=lease_ttl,
        )
        return await self.build_execution_bundle(
            started,
            publication_coordinator=publication_coordinator,
            role_snapshot=role_snapshot,
        )

    async def start_auxiliary_execution(
        self,
        *,
        session_id: int,
        invocation_id: int,
        role: str,
        role_snapshot: RoleConfigSnapshot | None = None,
        requirement: str = "detached",
        lease_ttl: Any = None,
    ) -> ObservedExecutionBundle:
        """Attach a non-primary observed model lane to an active invocation.

        Optional work such as PR label recommendation must not borrow the main
        reviewer observer: it has a different role binding and may run in
        parallel. A dedicated Thread, Work Unit, lease, and observer keep
        Attempt ownership and context revisions isolated while retaining the
        same long-lived resource Session and Invocation.
        """

        snapshot = role_snapshot or await self._resolve_snapshot(role)
        async with self._session_scope() as db:
            invocation = await db.get(
                ActivityInvocation,
                invocation_id,
                with_for_update=True,
            )
            if invocation is None:
                raise AdmissionError(
                    f"ActivityInvocation not found: {invocation_id}"
                )
            if int(invocation.session_id) != int(session_id):
                raise AdmissionError(
                    "auxiliary invocation does not belong to the requested session"
                )
            session = await db.get(ActivityObservabilitySession, session_id)
            if session is None:
                raise AdmissionError(
                    f"ActivityObservabilitySession not found: {session_id}"
                )

            thread = await self._ensure_thread(db, session_id, role)
            observability = ActivityObservabilityService(db=db)
            work_unit = await observability.create_work_unit(
                invocation.id,
                purpose=role,
                requirement=requirement,
                is_primary=False,
                role_snapshot=snapshot,
                thread_id=thread.id,
            )
            context_service = self._lease_context or ContextService(db=db)
            lease = await context_service.acquire_lease(
                thread.id,
                work_unit.id,
                ttl=lease_ttl,
            )
            if thread.current_revision_id is None:
                await context_service.create_revision(
                    thread.id,
                    lease,
                    expected_parent_revision_id=None,
                    message_manifest=[],
                    reason="initial",
                    created_invocation_id=invocation.id,
                    created_work_unit_id=work_unit.id,
                )
                thread = await db.get(ActivityThread, thread.id) or thread
            await db.flush()
            await self._commit_if_owned(db)
            started = ReviewStartResult(
                session=session,
                invocation=invocation,
                triggers=(),
                thread=thread,
                work_unit=work_unit,
                lease=lease,
                merged=False,
            )

        return await self.build_execution_bundle(
            started,
            role_snapshot=snapshot,
        )

    async def start_scan_execution(
        self,
        resource: Mapping[str, Any],
        *,
        trigger_ids: Sequence[int] | None = None,
        role_snapshot: RoleConfigSnapshot | None = None,
        role: str = "scan",
        task_id: int | None = None,
        publication_coordinator: Any = None,
    ) -> ObservedExecutionBundle:
        """Start a threadless scan execution with embedding-safe context."""
        session, invocation, work_unit, triggers = await self.start_scan(
            resource,
            trigger_ids=trigger_ids,
            role_snapshot=role_snapshot,
            role=role,
            task_id=task_id,
        )
        started = ReviewStartResult(
            session=session,
            invocation=invocation,
            triggers=triggers,
            thread=None,
            work_unit=work_unit,
            lease=None,
            merged=False,
        )
        return await self.build_execution_bundle(
            started,
            publication_coordinator=publication_coordinator,
            role_snapshot=role_snapshot,
        )

    async def _resolve_snapshot(self, role: str) -> RoleConfigSnapshot:
        if self._role_snapshot_resolver is not None:
            return await self._role_snapshot_resolver(role)
        now = datetime.now(timezone.utc)
        model = "unresolved"
        return RoleConfigSnapshot(
            role=role,
            requested_provider="unresolved",
            requested_model=model,
            requested_thinking_mode=None,
            candidate_chain=(("unresolved", model),),
            account_id="unresolved",
            protocol_family="unknown",
            endpoint_fingerprint=hashlib.sha256(b"unresolved").hexdigest(),
            config_snapshot_version=0,
            captured_at=now,
        )

    @staticmethod
    async def _ensure_thread(
        db: AsyncSession, session_id: int, purpose: str
    ) -> ActivityThread:
        thread = (
            await db.execute(
                select(ActivityThread)
                .where(
                    ActivityThread.session_id == session_id,
                    ActivityThread.thread_purpose == purpose,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if thread is None:
            thread = ActivityThread(
                session_id=session_id, thread_purpose=purpose, last_seq=0
            )
            db.add(thread)
            await db.flush()
        return thread

    @staticmethod
    async def _load_triggers(
        db: AsyncSession, ids: Sequence[int]
    ) -> list[ActivityTrigger]:
        rows = (
            (
                await db.execute(
                    select(ActivityTrigger).where(ActivityTrigger.id.in_(list(ids)))
                )
            )
            .scalars()
            .all()
        )
        by_id = {row.id: row for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    @classmethod
    async def _select_pending_triggers(
        cls, db: AsyncSession, session_id: int, ids: Sequence[int] | None
    ) -> list[ActivityTrigger]:
        statement = (
            select(ActivityTrigger)
            .where(
                ActivityTrigger.session_id == session_id,
                ActivityTrigger.status == "pending",
            )
            .order_by(ActivityTrigger.created_at, ActivityTrigger.id)
        )
        if ids is not None:
            wanted = list(dict.fromkeys(int(item) for item in ids))
            if not wanted:
                raise AdmissionError("trigger_ids must not be empty")
            statement = statement.where(ActivityTrigger.id.in_(wanted))
        return list((await db.execute(statement.with_for_update())).scalars().all())

    @staticmethod
    async def _active_review_with_live_lease(
        db: AsyncSession, session_id: int, purpose: str
    ):
        rows = (
            await db.execute(
                select(
                    ActivityInvocation,
                    ActivityThread,
                    ActivityInvocationWorkUnit,
                    ActivityThreadLease,
                )
                .join(
                    ActivityThread,
                    ActivityThread.session_id == ActivityInvocation.session_id,
                )
                .join(
                    ActivityInvocationWorkUnit,
                    and_(
                        ActivityInvocationWorkUnit.thread_id == ActivityThread.id,
                        ActivityInvocationWorkUnit.invocation_id
                        == ActivityInvocation.id,
                    ),
                )
                .join(
                    ActivityThreadLease,
                    and_(
                        ActivityThreadLease.thread_id == ActivityThread.id,
                        ActivityThreadLease.owner_work_unit_id
                        == ActivityInvocationWorkUnit.id,
                    ),
                )
                .where(
                    ActivityInvocation.session_id == session_id,
                    ActivityThread.thread_purpose == purpose,
                    ActivityInvocation.status.in_(("queued", "running")),
                    ActivityInvocationWorkUnit.is_primary.is_(True),
                    ActivityInvocationWorkUnit.purpose == purpose,
                )
                .with_for_update()
            )
        ).all()
        now = utc_now()
        for invocation, thread, work_unit, lease in rows:
            expires = lease.expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires is not None and expires > now:
                return (
                    invocation,
                    thread,
                    work_unit,
                    ThreadLeaseToken(
                        thread_id=thread.id,
                        owner_work_unit_id=work_unit.id,
                        fencing_token=lease.fencing_token,
                        base_revision_id=lease.base_revision_id,
                        expires_at=expires,
                    ),
                )
        return None


# Short name used by integrations that treat this as the admission boundary.
IntegrationService = ActivityIntegrationService

__all__ = [
    "AdmissionError",
    "ActivityIntegrationService",
    "IntegrationService",
    "AdmissionResult",
    "NormalizedResource",
    "ReviewStartResult",
]
