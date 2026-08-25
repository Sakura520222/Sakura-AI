"""Integration tests for the worker-facing observability admission boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationTrigger,
    ActivityInvocationWorkUnit,
    ActivityObservabilitySession,
    ActivityThreadLease,
)
from backend.models.database import Base
from backend.services.activity_observability.context_service import (
    ContextService,
    StaleThreadLeaseError,
)
from backend.services.activity_observability.contracts import RoleConfigSnapshot
from backend.services.activity_observability.integration_service import (
    ActivityIntegrationService,
    AdmissionError,
)


@compiles(LONGTEXT, "sqlite")
def _compile_longtext(_type, _compiler, **_kwargs):
    return "TEXT"


class _AsyncNested:
    def __init__(self, transaction):
        self.transaction = transaction

    async def __aenter__(self):
        self.transaction.__enter__()
        return self

    async def __aexit__(self, exc_type, value, traceback):
        return self.transaction.__exit__(exc_type, value, traceback)


class _AsyncAdapter:
    def __init__(self, session: Session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _value, _traceback):
        if exc_type:
            self.session.rollback()
        return False

    def add(self, value):
        self.session.add(value)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, ident, **kwargs):
        return self.session.get(model, ident, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def refresh(self, value):
        self.session.refresh(value)

    async def delete(self, value):
        self.session.delete(value)

    def begin_nested(self):
        return _AsyncNested(self.session.begin_nested())


@pytest.fixture
def db():
    engine = __import__("sqlalchemy").create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _configure(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_collation("ascii_bin", lambda a, b: (a > b) - (a < b))
        connection.create_function("regexp", 2, lambda pattern, value: True)

    tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name.startswith("activity_observability_")
    ]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield _AsyncAdapter(session)
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def resource():
    return {
        "source_host": "github.com",
        "repository_external_id": "repo-42",
        "repo_full_name": "old-owner/repo",
        "pr_number": 7,
    }


@pytest.fixture
def snapshot():
    return RoleConfigSnapshot(
        role="reviewer",
        requested_provider="test",
        requested_model="review-model",
        requested_thinking_mode=None,
        candidate_chain=(("test", "review-model"),),
        account_id="account",
        protocol_family="test",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
        captured_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_bundle_retry_converges_invocation_after_partial_finish_failure(
    db, resource, snapshot
):
    """真实 SQLite 状态：finish 失败后 release 仅释放 lease，重试可聚合 Invocation。"""
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(resource, delivery_id="partial-1")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role_snapshot=snapshot,
    )
    bundle = await service.build_execution_bundle(started)

    original_finish = bundle.observability.finish_work_unit
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient finish failure")
        return await original_finish(*args, **kwargs)

    bundle.observability.finish_work_unit = fail_once
    with pytest.raises(RuntimeError, match="transient finish failure"):
        await bundle.finish("completed")

    work_unit = await db.get(ActivityInvocationWorkUnit, started.work_unit.id)
    invocation = await db.get(ActivityInvocation, started.invocation.id)
    lease = await db.get(ActivityThreadLease, started.thread.id)
    assert work_unit.status == "queued"
    assert invocation.status == "queued"
    assert lease is None

    await bundle.finish("completed")
    work_unit = await db.get(ActivityInvocationWorkUnit, started.work_unit.id)
    invocation = await db.get(ActivityInvocation, started.invocation.id)
    assert work_unit.status == "completed"
    assert invocation.status == "completed"
    assert await db.get(ActivityThreadLease, started.thread.id) is None


@pytest.mark.asyncio
async def test_bundle_release_failure_is_retryable_after_work_unit_aggregation(
    db, resource, snapshot
):
    """finish 成功但首次 release 失败时，WorkUnit/Invocation 仍已聚合且可重试释放。"""
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(resource, delivery_id="partial-2")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role_snapshot=snapshot,
    )
    bundle = await service.build_execution_bundle(started)
    original_release = bundle.context_service.release_lease
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient release failure")
        return await original_release(*args, **kwargs)

    bundle.context_service.release_lease = fail_once
    with pytest.raises(RuntimeError, match="transient release failure"):
        await bundle.finish("completed")

    invocation = await db.get(ActivityInvocation, started.invocation.id)
    work_unit = await db.get(ActivityInvocationWorkUnit, started.work_unit.id)
    assert work_unit.status == "completed"
    assert invocation.status == "completed"
    assert await db.get(ActivityThreadLease, started.thread.id) is not None

    await bundle.finish("completed")
    assert await db.get(ActivityThreadLease, started.thread.id) is None


@pytest.mark.asyncio
async def test_started_execution_renews_lease_until_finish(db, resource, snapshot):
    """A long-running execution keeps its canonical context lease alive."""
    context = ContextService(db=db, lease_duration=timedelta(milliseconds=90))
    service = ActivityIntegrationService(db=db, lease_context=context)
    admitted = await service.admit_synchronize(resource, delivery_id="heartbeat-1")

    execution = await service.start_execution(
        session_id=admitted.session_id,
        trigger_ids=[admitted.trigger_id],
        role_snapshot=snapshot,
        role="reviewer",
        task_type="pr",
    )
    initial_expiry = execution.lease.expires_at

    try:
        await asyncio.sleep(0.18)
        stored_lease = await db.get(ActivityThreadLease, execution.thread.id)

        assert stored_lease is not None
        assert stored_lease.expires_at > initial_expiry
        assert execution.lease.expires_at == stored_lease.expires_at
        assert execution.observer.lease == execution.lease
    finally:
        await execution.finish("completed")


@pytest.mark.asyncio
async def test_cancelled_execution_stops_lease_heartbeat_and_releases_lease(
    db, resource, snapshot
):
    """取消终态必须停止 detached heartbeat 并释放当前 lease。"""
    context = ContextService(db=db, lease_duration=timedelta(milliseconds=90))
    service = ActivityIntegrationService(db=db, lease_context=context)
    admitted = await service.admit_synchronize(
        resource, delivery_id="heartbeat-cancelled-1"
    )

    execution = await service.start_execution(
        session_id=admitted.session_id,
        trigger_ids=[admitted.trigger_id],
        role_snapshot=snapshot,
        role="reviewer",
        task_type="pr",
    )
    heartbeat_task = execution._lease_heartbeat_task
    assert heartbeat_task is not None

    await execution.finish("cancelled")

    assert heartbeat_task.done()
    stored_work_unit = await db.get(ActivityInvocationWorkUnit, execution.work_unit.id)
    assert stored_work_unit.status == "cancelled"
    assert await db.get(ActivityThreadLease, execution.thread.id) is None


@pytest.mark.asyncio
async def test_bundle_converges_after_stale_release_once_work_unit_is_terminal(
    db, resource, snapshot
):
    """A stale old token must not make terminal finish retry forever."""
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(resource, delivery_id="stale-release-1")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role_snapshot=snapshot,
    )
    bundle = await service.build_execution_bundle(started)
    release_calls = 0

    async def stale_release(_token):
        nonlocal release_calls
        release_calls += 1
        raise StaleThreadLeaseError("lease expired, released, or fenced")

    bundle.context_service.release_lease = stale_release

    await bundle.finish("completed")
    await bundle.finish("completed")

    stored_work_unit = await db.get(ActivityInvocationWorkUnit, started.work_unit.id)
    stored_invocation = await db.get(ActivityInvocation, started.invocation.id)
    assert stored_work_unit.status == "completed"
    assert stored_invocation.status == "completed"
    assert release_calls == 1


@pytest.mark.asyncio
async def test_auxiliary_execution_uses_separate_detached_thread_and_observer(
    db, resource, snapshot
):
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(
        resource,
        delivery_id="auxiliary-summary",
    )
    primary = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role="main",
        role_snapshot=snapshot,
    )
    primary_bundle = await service.build_execution_bundle(primary)
    summary_snapshot = RoleConfigSnapshot(
        role="summary",
        requested_provider="test",
        requested_model="summary-model",
        requested_thinking_mode=None,
        candidate_chain=(("test", "summary-model"),),
        account_id="summary-account",
        protocol_family="test",
        endpoint_fingerprint="b" * 64,
        config_snapshot_version=1,
        captured_at=datetime.now(UTC),
    )

    auxiliary = await service.start_auxiliary_execution(
        session_id=primary.session.id,
        invocation_id=primary.invocation.id,
        role="summary",
        role_snapshot=summary_snapshot,
        requirement="detached",
    )

    assert auxiliary.session.id == primary.session.id
    assert auxiliary.invocation.id == primary.invocation.id
    assert auxiliary.work_unit.id != primary.work_unit.id
    assert auxiliary.work_unit.requirement == "detached"
    assert auxiliary.work_unit.is_primary is False
    assert auxiliary.thread.id != primary.thread.id
    assert auxiliary.thread.thread_purpose == "summary"
    assert auxiliary.invocation_context.work_unit_id == auxiliary.work_unit.id
    assert auxiliary.invocation_context.thread_id == auxiliary.thread.id
    assert auxiliary.invocation_context.role_snapshot == summary_snapshot
    assert auxiliary.observer is not primary_bundle.observer

    await auxiliary.finish("completed")
    stored_auxiliary = await db.get(
        ActivityInvocationWorkUnit,
        auxiliary.work_unit.id,
    )
    stored_invocation = await db.get(ActivityInvocation, primary.invocation.id)
    assert stored_auxiliary.status == "completed"
    assert stored_invocation.status not in {"completed", "failed", "partial"}
    assert await db.get(ActivityThreadLease, auxiliary.thread.id) is None
    await primary_bundle.finish("completed")


@pytest.mark.asyncio
async def test_active_review_lease_join_follows_invocation_and_owner_work_unit(
    db, resource, snapshot
):
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(resource, delivery_id="join-chain")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role_snapshot=snapshot,
    )

    competing_invocation = ActivityInvocation(
        session_id=started.session.id,
        status="queued",
    )
    db.add(competing_invocation)
    await db.flush()
    competing_work_unit = ActivityInvocationWorkUnit(
        invocation_id=competing_invocation.id,
        session_id=started.session.id,
        thread_id=started.thread.id,
        role_binding_snapshot_id=started.work_unit.role_binding_snapshot_id,
        purpose="reviewer",
        requirement="primary_required",
        status="queued",
        is_primary=True,
    )
    db.add(competing_work_unit)
    await db.flush()
    lease = await db.get(ActivityThreadLease, started.thread.id)
    lease.owner_work_unit_id = competing_work_unit.id
    await db.commit()

    active = await service._active_review_with_live_lease(
        db, started.session.id, "reviewer"
    )
    assert active is not None
    invocation, _thread, work_unit, lease_token = active
    assert invocation.id == work_unit.invocation_id
    assert lease_token.owner_work_unit_id == work_unit.id
    assert invocation.id == competing_invocation.id
    assert work_unit.id == competing_work_unit.id


@pytest.mark.asyncio
async def test_active_review_requires_matching_work_unit_purpose(
    db, resource, snapshot
):
    service = ActivityIntegrationService(db=db)
    admitted = await service.admit_synchronize(resource, delivery_id="purpose-mismatch")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id, role_snapshot=snapshot
    )
    started.work_unit.purpose = "issue_analyzer"
    await db.commit()
    assert (
        await service._active_review_with_live_lease(db, started.session.id, "reviewer")
        is None
    )
    started.work_unit.purpose = "reviewer"
    await db.commit()
    assert (
        await service._active_review_with_live_lease(db, started.session.id, "reviewer")
        is not None
    )


@pytest.mark.asyncio
async def test_two_synchronize_events_share_session_and_merge(db, resource, snapshot):
    service = ActivityIntegrationService(db=db)
    first = await service.admit_synchronize(
        resource, delivery_id="d-1", base_sha="a", head_sha="b"
    )
    second = await service.admit_synchronize(
        resource | {"repo_full_name": "new-owner/repo"},
        delivery_id="d-2",
        base_sha="b",
        head_sha="c",
    )
    assert first.session_id == second.session_id
    started = await service.start_or_merge_review(
        session_id=first.session_id,
        trigger_ids=[first.trigger_id],
        role_snapshot=snapshot,
        task_id=1,
    )
    merged = await service.start_or_merge_review(
        session_id=first.session_id,
        trigger_ids=[second.trigger_id],
        role_snapshot=snapshot,
        task_id=1,
    )
    assert not started.merged
    assert merged.merged
    assert merged.invocation_id == started.invocation_id
    links = (await db.execute(select(ActivityInvocationTrigger))).scalars().all()
    assert len(links) == 2
    assert merged.invocation.final_head_sha == "c"


@pytest.mark.asyncio
async def test_redelivery_is_same_trigger_and_rename_keeps_identity(db, resource):
    service = ActivityIntegrationService(db=db)
    first = await service.admit_synchronize(resource, delivery_id="delivery-1")
    retry = await service.admit_synchronize(
        resource | {"repo_full_name": "renamed/repo"}, delivery_id="delivery-1"
    )
    assert retry.duplicate
    assert first.trigger_id == retry.trigger_id
    sessions = (await db.execute(select(ActivityObservabilitySession))).scalars().all()
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_bundle_keeps_persisted_role_snapshot_without_resolving_purpose(
    db, resource, snapshot
):
    calls = []

    async def resolver(role):
        calls.append(role)
        return snapshot

    service = ActivityIntegrationService(db=db, role_snapshot_resolver=resolver)
    admitted = await service.admit_synchronize(resource, delivery_id="role-main")
    started = await service.start_or_merge_review(
        session_id=admitted.session_id,
        role="reviewer",
        role_snapshot=snapshot,
    )
    bundle = await service.build_execution_bundle(started)
    assert bundle.invocation_context.role_snapshot == snapshot
    assert calls == []
    with pytest.raises(AdmissionError):
        ActivityIntegrationService.normalize_resource(
            resource | {"repository_external_id": None}
        )


@pytest.mark.asyncio
async def test_active_lease_mismatch_keeps_next_trigger_pending(db, resource, snapshot):
    service = ActivityIntegrationService(db=db, lease_context=ContextService(db=db))
    admitted = await service.admit_synchronize(
        resource, delivery_id="d-1", base_sha="a", head_sha="b"
    )
    started = await service.start_or_merge_review(
        session_id=admitted.session_id, role_snapshot=snapshot
    )
    await service.admit_synchronize(
        resource, delivery_id="d-2", base_sha="b", head_sha="c"
    )
    # A finished invocation cannot merge a new trigger; this is the worker crash/recovery boundary.
    await service._lease_context.release_lease(started.lease, "completed")
    next_run = await service.start_or_merge_review(
        session_id=admitted.session_id, role_snapshot=snapshot
    )
    assert next_run.invocation_id != started.invocation_id
    assert next_run.merged is False


@pytest.mark.asyncio
async def test_scan_uses_ephemeral_unthreaded_work_unit(db):
    service = ActivityIntegrationService(db=db)
    session, _invocation, work_unit, triggers = await service.start_scan(
        {"task_id": "scan-1", "repo_full_name": "owner/repo", "delivery_id": "scan-1"},
        role_snapshot=None,
        task_id=1,
    )
    assert session.session_kind == "ephemeral"
    assert work_unit.thread_id is None
    assert len(triggers) == 1
