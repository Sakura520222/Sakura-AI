"""Canonical context revision, lease, snapshot and operation service tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.database import Base
from backend.services.activity_observability.context_service import (
    AVAILABILITY_UNAVAILABLE,
    SOURCE_PROVIDER,
    ContextService,
    ContextSnapshotFields,
    MeasuredValue,
    StaleThreadLeaseError,
)


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_for_sqlite(_type, _compiler, **_kwargs):  # pragma: no cover
    return "TEXT"


class _BeginContext:
    """Mimic AsyncSession.begin(): commit on clean exit, rollback on exception."""

    def __init__(self, session: Session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self._session.rollback()
        else:
            self._session.commit()
        return False


class _AsyncSessionAdapter:
    """Expose the AsyncSession subset over SQLite's synchronous test session."""

    def __init__(self, session: Session):
        self._session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self._session.rollback()
        return False

    def add(self, instance):
        self._session.add(instance)

    async def execute(self, statement):
        return self._session.execute(statement)

    async def scalar(self, statement):
        return self._session.scalar(statement)

    async def get(self, model, object_id, **kwargs):
        return self._session.get(model, object_id, **kwargs)

    async def delete(self, instance):
        self._session.delete(instance)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def refresh(self, instance):
        self._session.refresh(instance)

    def begin(self):
        return _BeginContext(self._session)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        dbapi_connection.create_collation(
            "ascii_bin", lambda left, right: (left > right) - (left < right)
        )
        dbapi_connection.create_function(
            "regexp",
            2,
            lambda pattern, value: bool(
                value is not None and re.search(pattern, value)
            ),
        )
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    observability_tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name.startswith("activity_observability_")
    ]
    Base.metadata.create_all(engine, tables=observability_tables)
    sync_session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield _AsyncSessionAdapter(sync_session)
    finally:
        sync_session.close()
        engine.dispose()


@pytest.fixture
def context_service(db_session):
    return ContextService(db=db_session)


def _make_thread(db_session, session_id=None):
    """直接用 ORM 构造 Session+Thread，绕过 ActivityObservabilityService 依赖。"""
    from backend.models.activity_observability_models import (
        ActivityObservabilitySession,
        ActivityResourceIdentity,
        ActivityThread,
    )

    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="987654",
        resource_type="pr",
        resource_number=42,
        repo_full_name="owner/repo",
    )
    db_session._session.add(identity)
    db_session._session.flush()
    session = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    db_session._session.add(session)
    db_session._session.flush()
    thread = ActivityThread(
        session_id=session.id,
        thread_purpose="reviewer",
        last_seq=0,
    )
    db_session._session.add(thread)
    db_session._session.commit()
    return thread


def _make_work_unit(db_session, thread):
    from backend.models.activity_observability_models import (
        ActivityInvocation,
        ActivityInvocationWorkUnit,
        ActivityObservabilityRoleBindingSnapshot,
    )

    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="anthropic",
        requested_model="model",
        requested_thinking_mode=None,
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="anthropic_native",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
        captured_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    db_session._session.add(snapshot)
    db_session._session.flush()
    invocation = ActivityInvocation(
        session_id=thread.session_id,
        status="queued",
        current_phase="preparing_context",
        primary_work_unit_id=None,
        session_event_sequence=0,
    )
    db_session._session.add(invocation)
    db_session._session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        thread_id=thread.id,
        purpose="reviewer",
        requirement="primary_required",
        is_primary=True,
        status="queued",
        role_binding_snapshot_id=snapshot.id,
        requested_provider="anthropic",
        requested_model="model",
    )
    db_session._session.add(work_unit)
    db_session._session.commit()
    return work_unit


def _make_messages(db_session, thread, work_unit, count):
    """Create canonical message fixtures so manifests use real message IDs."""
    from backend.models.activity_observability_models import (
        ActivityObservabilityMessage,
    )

    messages = []
    for seq in range(count):
        message = ActivityObservabilityMessage(
            thread_id=thread.id,
            work_unit_id=work_unit.id,
            seq=seq,
            role="user",
            content=f"message-{seq}",
            message_json=f'{{"role":"user","content":"message-{seq}"}}',
        )
        db_session._session.add(message)
        messages.append(message)
    db_session._session.flush()
    return messages


# ---------------------------------------------------------------------------


def test_measured_value_requires_unavailable_pairing_for_null():
    with pytest.raises(ValueError):
        MeasuredValue(value=None, availability="reported", source=SOURCE_PROVIDER)


def test_measured_value_rejects_non_null_marked_unavailable():
    with pytest.raises(ValueError):
        MeasuredValue(
            value=100, availability=AVAILABILITY_UNAVAILABLE, source=SOURCE_PROVIDER
        )


@pytest.mark.asyncio
async def test_stale_fencing_token_cannot_replace_thread_current_revision(
    context_service, db_session
):
    thread = _make_thread(db_session)
    work_unit = _make_work_unit(db_session, thread)
    messages = _make_messages(db_session, thread, work_unit, 2)

    first = await context_service.acquire_lease(thread.id, work_unit.id)
    await context_service.expire_lease_for_test(thread.id)
    second = await context_service.acquire_lease(thread.id, work_unit.id)
    revision_two = await context_service.create_revision(
        thread.id,
        second,
        expected_parent_revision_id=None,
        message_manifest=[messages[0].id, messages[1].id],
        reason="initial",
    )

    with pytest.raises(StaleThreadLeaseError):
        await context_service.create_revision(
            thread.id,
            first,
            expected_parent_revision_id=None,
            message_manifest=[1, 9],
            reason="late",
        )

    refreshed = await context_service.get_thread(thread.id)
    assert refreshed.current_revision_id == revision_two.id


@pytest.mark.asyncio
async def test_completed_compaction_replaces_revision_for_every_following_attempt(
    context_service, db_session
):
    thread = _make_thread(db_session)
    work_unit = _make_work_unit(db_session, thread)
    messages = _make_messages(db_session, thread, work_unit, 3)

    lease = await context_service.acquire_lease(thread.id, work_unit.id)
    before = await context_service.create_revision(
        thread.id,
        lease,
        expected_parent_revision_id=None,
        message_manifest=[m.id for m in messages],
        reason="initial",
    )
    operation = await context_service.begin_operation(
        work_unit.id, "canonical_summary", "threshold", before.id
    )
    after = await context_service.create_revision(
        thread.id,
        lease,
        expected_parent_revision_id=before.id,
        message_manifest=[messages[0].id],
        reason="compaction",
        context_operation_id=operation.id,
    )
    await context_service.complete_operation(operation.id, after.id)

    next_revision = await context_service.context_revision_for_next_attempt(thread.id)
    assert next_revision == after.id


@pytest.mark.asyncio
async def test_snapshot_requires_revision_and_owner(context_service):
    fields = ContextSnapshotFields(
        context_tokens=MeasuredValue(1000, "reported", SOURCE_PROVIDER),
        context_window_tokens=MeasuredValue(128000, "reported", SOURCE_PROVIDER),
    )
    with pytest.raises(ValueError):
        await context_service.record_snapshot(
            attempt_id=None,
            operation_id=None,
            revision_id=None,
            snapshot_kind="before_request",
            fields=fields,
        )


@pytest.mark.asyncio
async def test_canonical_message_excludes_reasoning_content(
    context_service, db_session
):
    thread = _make_thread(db_session)
    work_unit = _make_work_unit(db_session, thread)
    messages = _make_messages(db_session, thread, work_unit, 1)
    lease = await context_service.acquire_lease(thread.id, work_unit.id)
    revision = await context_service.create_revision(
        thread.id,
        lease,
        expected_parent_revision_id=None,
        message_manifest=[messages[0].id],
        reason="initial",
    )

    message = await context_service.append_canonical_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        revision_id=revision.id,
        seq=1,
        role="assistant",
        content="review result",
        message_json={
            "role": "assistant",
            "content": "review result",
            "reasoning_content": "MUST NOT BE PERSISTED",
        },
    )
    assert "MUST NOT BE PERSISTED" not in (message.message_json or "")
