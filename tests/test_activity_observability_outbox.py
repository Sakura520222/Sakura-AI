"""Task 7 transactional outbox and dispatcher contract tests."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityObservabilityEvent,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityOutbox,
    ActivityResourceIdentity,
)
from backend.models.database import Base, utc_now
from backend.services.activity_observability.contracts import PublicActivityNotification
from backend.services.activity_observability.outbox_service import (
    ActivityOutboxService,
    OutboxDispatcher,
    OutboxDispatcherConfig,
    OutboxRetryPolicy,
    append_event_and_outbox,
)


class AsyncSqliteAdapter:
    """Small AsyncSession-shaped adapter used when aiosqlite is unavailable."""

    def __init__(self, session: Session):
        self.session = session
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _value, _traceback):
        del _value, _traceback
        if exc_type is not None:
            self.session.rollback()
        return False

    def add(self, row):
        self.session.add(row)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, object_id, **kwargs):
        return self.session.get(model, object_id, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.commit_calls += 1
        self.session.commit()

    async def rollback(self):
        self.rollback_calls += 1
        self.session.rollback()

    async def refresh(self, row):
        self.session.refresh(row)


class SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, value, traceback):
        return await self.db.__aexit__(exc_type, value, traceback)


class TrackingSessionContext:
    def __init__(self, db, contexts):
        self.db = db
        self.contexts = contexts
        self.entered = False
        self.exited = False
        self.exit_exception = None
        contexts.append(self)

    async def __aenter__(self):
        self.entered = True
        return self.db

    async def __aexit__(self, exc_type, value, traceback):
        self.exited = True
        self.exit_exception = exc_type
        return await self.db.__aexit__(exc_type, value, traceback)


class _ClaimStatementSpy(AsyncSqliteAdapter):
    def __init__(self, session: Session):
        super().__init__(session)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return await super().execute(statement)


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _configure(connection, _record):
        connection.create_collation(
            "ascii_bin", lambda left, right: (left > right) - (left < right)
        )
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name.startswith("activity_observability_")
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def _seed_session(db: AsyncSqliteAdapter, number: int = 1):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id=str(number),
        resource_type="pr",
        resource_number=str(number),
        repo_full_name=f"owner/repo-{number}",
    )
    db.add(identity)
    db.session.flush()
    activity_session = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    db.add(activity_session)
    db.session.flush()
    return activity_session


def _seed_parent_chain(db: AsyncSqliteAdapter, activity_session):
    invocation = ActivityInvocation(session_id=activity_session.id, status="queued")
    db.add(invocation)
    db.session.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="openai",
        requested_model="review-model",
        candidate_chain_json='[["openai","review-model"]]',
        account_id="account",
        protocol_family="openai_compatible",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    db.add(snapshot)
    db.session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=activity_session.id,
        role_binding_snapshot_id=snapshot.id,
        purpose="reviewer",
        requirement="primary_required",
        status="queued",
        is_primary=True,
    )
    db.add(work_unit)
    db.session.flush()
    return invocation, work_unit


@pytest.fixture
def db():
    engine, factory = _database()
    session = factory()
    adapter = AsyncSqliteAdapter(session)
    try:
        yield adapter
    finally:
        session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_append_is_one_outer_transaction_and_rollback_removes_both_rows(db):
    activity_session = _seed_session(db)
    event_row = await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="phase",
        visibility="public",
        payload={"status": "running", "api_key": "must-not-be-in-envelope"},
        recipient_user_ids=["user-a"],
    )

    assert db.commit_calls == 0
    outbox = (
        (
            await db.execute(
                select(ActivityOutbox).where(
                    ActivityOutbox.event_uuid == event_row.event_uuid
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1
    assert json.loads(outbox[0].payload_json) == {
        "event_id": event_row.event_uuid,
        "sequence": 1,
        "projection_version": 1,
    }

    await db.rollback()
    assert db.rollback_calls == 1
    assert (await db.execute(select(ActivityObservabilityEvent))).scalars().all() == []
    assert (await db.execute(select(ActivityOutbox))).scalars().all() == []


@pytest.mark.asyncio
async def test_cross_session_parent_chain_is_rejected_without_sequence_or_rows(db):
    first = _seed_session(db, 1)
    second = _seed_session(db, 2)
    invocation, work_unit = _seed_parent_chain(db, second)
    before = first.session_event_sequence

    with pytest.raises(ValueError, match="invocation"):
        await append_event_and_outbox(
            db,
            session_id=first.id,
            invocation_id=invocation.id,
            event_type="phase",
            visibility="public",
            payload={"status": "running"},
            recipient_user_ids=["user-a"],
        )
    assert first.session_event_sequence == before
    assert (await db.execute(select(ActivityObservabilityEvent))).scalars().all() == []

    with pytest.raises(ValueError, match="work unit"):
        await append_event_and_outbox(
            db,
            session_id=first.id,
            work_unit_id=work_unit.id,
            event_type="phase",
            visibility="public",
            payload={"status": "running"},
            recipient_user_ids=["user-a"],
        )
    assert first.session_event_sequence == before
    assert (await db.execute(select(ActivityObservabilityEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_sequence_uuid_and_one_outbox_row_per_recipient_and_empty_audience_is_audit_only(
    db,
):
    activity_session = _seed_session(db)
    first = await ActivityOutboxService(db).record_event_for_session(
        activity_session.id,
        {"status": "started"},
        recipient_user_ids=["user-a", "user-b", "user-a"],
    )
    second = await ActivityOutboxService(db).record_event_for_session(
        activity_session.id,
        {"status": "finished"},
        recipient_user_ids=[],
    )

    rows = (
        (await db.execute(select(ActivityOutbox).order_by(ActivityOutbox.id)))
        .scalars()
        .all()
    )
    assert [row.target_user_id for row in rows] == ["user-a", "user-b"]
    assert {row.event_uuid for row in rows} == {first.event_uuid}
    assert all(row.event_sequence == 1 for row in rows)
    assert second.event_sequence == 2
    assert (
        await db.execute(
            select(ActivityOutbox).where(ActivityOutbox.event_uuid == second.event_uuid)
        )
    ).scalars().all() == []
    assert activity_session.session_event_sequence == 2


@pytest.mark.asyncio
async def test_dispatch_reauthorizes_and_publishes_exact_three_field_sse_projection(
    db, monkeypatch
):
    activity_session = _seed_session(db)
    event_row = await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="secret-bearing-event",
        visibility="public",
        payload={
            "status": "running",
            "prompt": "raw prompt",
            "tool_args": {"token": "secret"},
            "endpoint": "https://provider.invalid",
        },
        recipient_user_ids=["user:a"],
    )
    await db.commit()

    published: list[tuple[str, PublicActivityNotification]] = []

    async def publisher(user_id, notification):
        published.append((user_id, notification))

    authorizer_calls = []

    async def authorizer(*, db, user_id, session_id):
        authorizer_calls.append((user_id, session_id))
        return user_id == "user:a"

    dispatcher = OutboxDispatcher(
        lambda: SessionContext(db),
        authorizer=authorizer,
        publisher=publisher,
        config=OutboxDispatcherConfig(
            batch_size=10,
            retry_policy=OutboxRetryPolicy(initial_delay_seconds=0),
        ),
    )
    assert await dispatcher.dispatch_once() == 1
    assert authorizer_calls == [("user:a", activity_session.id)]
    assert len(published) == 1
    assert published[0][0] == "user:a"
    assert published[0][1].to_sse_data() == {
        "event_id": event_row.event_uuid,
        "sequence": 1,
        "projection_version": 1,
    }
    row = (await db.execute(select(ActivityOutbox))).scalars().one()
    assert row.status == "published"
    assert row.event_uuid == published[0][1].event_id

    calls = []

    async def legacy_publish(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("backend.webui.sse.publish_event", legacy_publish)
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_cancellation_exits_claim_and_delivery_sessions(db):
    activity_session = _seed_session(db)
    await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="cancelled-publish",
        visibility="public",
        payload={"status": "running"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()

    contexts = []
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    async def publisher(_user_id, _notification):
        publish_started.set()
        await release_publish.wait()

    def session_factory():
        return TrackingSessionContext(db, contexts)

    dispatcher = OutboxDispatcher(
        session_factory,
        authorizer=lambda **_kwargs: True,
        publisher=publisher,
        config=OutboxDispatcherConfig(
            retry_policy=OutboxRetryPolicy(initial_delay_seconds=0),
        ),
    )
    task = asyncio.create_task(dispatcher.dispatch_once())
    await asyncio.wait_for(publish_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert contexts
    assert all(context.entered for context in contexts)
    assert all(context.exited for context in contexts)
    assert any(context.exit_exception is asyncio.CancelledError for context in contexts)


@pytest.mark.asyncio
async def test_dispatcher_run_stops_after_stop_without_cancelling_current_session(db):
    contexts = []
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    class Dispatcher(OutboxDispatcher):
        async def dispatch_once(self):
            dispatch_started.set()
            await release_dispatch.wait()
            return 0

    dispatcher = Dispatcher(
        lambda: TrackingSessionContext(db, contexts),
        authorizer=lambda **_kwargs: True,
    )
    task = asyncio.create_task(dispatcher.run())
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)
    dispatcher.stop()
    release_dispatch.set()
    await asyncio.wait_for(task, timeout=1)
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_dispatch_failure_retries_with_configured_backoff_and_at_least_once_event_id(
    db,
):
    activity_session = _seed_session(db)
    event_row = await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="retry",
        visibility="public",
        payload={"status": "running"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()
    attempts: list[str] = []

    async def publisher(_user_id, notification):
        attempts.append(notification.event_id)
        if len(attempts) == 1:
            raise RuntimeError("provider unavailable")

    dispatcher = OutboxDispatcher(
        lambda: SessionContext(db),
        authorizer=lambda **_kwargs: True,
        publisher=publisher,
        config=OutboxDispatcherConfig(
            retry_policy=OutboxRetryPolicy(
                max_attempts=3,
                initial_delay_seconds=0,
                backoff_factor=3,
            )
        ),
    )
    assert await dispatcher.dispatch_once() == 0
    row = (await db.execute(select(ActivityOutbox))).scalars().one()
    assert row.status == "pending"
    assert row.last_error == "RuntimeError"
    assert row.attempt_count == 1

    assert await dispatcher.dispatch_once() == 1
    row = (await db.execute(select(ActivityOutbox))).scalars().one()
    assert row.status == "published"
    assert attempts == [event_row.event_uuid, event_row.event_uuid]


@pytest.mark.asyncio
async def test_dispatch_revocation_cancels_without_publishing_or_payload_leak(db):
    activity_session = _seed_session(db)
    await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="revoked",
        visibility="public",
        payload={"error_detail": "credential=secret-value", "prompt": "raw"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()
    published = []

    async def publisher(*args):
        published.append(args)

    dispatcher = OutboxDispatcher(
        lambda: SessionContext(db),
        authorizer=lambda **_kwargs: False,
        publisher=publisher,
    )
    assert await dispatcher.dispatch_once() == 0
    row = (await db.execute(select(ActivityOutbox))).scalars().one()
    assert row.status == "cancelled"
    assert published == []
    assert "secret" not in (row.last_error or "").lower()


def test_claim_query_compiles_actual_claim_statement_for_mysql_skip_locked(db):
    dispatcher = OutboxDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(batch_size=7, claim_timeout_seconds=30),
    )
    spy = _ClaimStatementSpy(db.session)

    import asyncio

    asyncio.run(dispatcher._claim(spy))
    statement = spy.statements[0]
    sql = str(statement.compile(dialect=mysql_dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "status" in sql and "claimed_at" in sql
    assert "?" not in sql


def test_outbox_claim_timeout_has_finite_safe_default():
    from backend.core.config import Settings

    assert OutboxDispatcherConfig().claim_timeout_seconds == 300.0
    assert Settings().activity_outbox_claim_timeout_seconds == 300.0
    assert OutboxDispatcherConfig().artifact_purge_interval_seconds == 3600.0
    assert Settings().activity_artifact_purge_interval_seconds == 3600.0


@pytest.mark.asyncio
async def test_dispatcher_purges_on_start_and_throttles_until_interval():
    calls = 0

    class Dispatcher(OutboxDispatcher):
        async def _purge_expired_artifacts(self):
            nonlocal calls
            calls += 1
            return 1

        async def dispatch_once(self):
            self.stop()
            return 0

    dispatcher = Dispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(artifact_purge_interval_seconds=60),
    )

    await dispatcher.run()
    assert calls == 1
    assert await dispatcher._maybe_purge_expired_artifacts() == 0


@pytest.mark.asyncio
async def test_dispatcher_purge_failure_is_isolated_but_cancellation_propagates():
    class FailingDispatcher(OutboxDispatcher):
        async def _purge_expired_artifacts(self):
            raise RuntimeError("purge unavailable")

        async def dispatch_once(self):
            self.stop()
            return 0

    dispatcher = FailingDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(artifact_purge_interval_seconds=60),
    )
    await dispatcher.run()

    class CancelledDispatcher(OutboxDispatcher):
        async def _purge_expired_artifacts(self):
            raise asyncio.CancelledError

    cancelled = CancelledDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled._maybe_purge_expired_artifacts(force=True)


@pytest.mark.parametrize("user_id", ["", "a\nb", "a\rb", "a\tb", "a" * 256])
def test_outbox_recipient_rejects_control_or_oversized_identifiers(db, user_id):
    activity_session = _seed_session(db)

    async def run():
        await append_event_and_outbox(
            db,
            session_id=activity_session.id,
            event_type="bad-recipient",
            visibility="public",
            payload={"status": "running"},
            recipient_user_ids=[user_id],
        )

    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(run())


@pytest.mark.asyncio
async def test_claim_does_not_reclaim_live_claim_or_future_backoff(db):
    activity_session = _seed_session(db)
    await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="claim-boundaries",
        visibility="public",
        payload={"status": "running"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()
    dispatcher = OutboxDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(claim_timeout_seconds=30),
    )
    claimed = await dispatcher._claim(db)
    row = claimed[0]
    assert await dispatcher._claim(db) == []
    row.status = "pending"
    row.claim_token = None
    row.claimed_at = None
    from datetime import timedelta

    row.next_attempt_at = utc_now() + timedelta(seconds=60)
    await db.commit()
    assert await dispatcher._claim(db) == []
    row.next_attempt_at = utc_now()
    await db.commit()
    assert len(await dispatcher._claim(db)) == 1


@pytest.mark.asyncio
async def test_dispatch_reclaims_stale_claim_with_fencing_and_attempt_increment(db):
    activity_session = _seed_session(db, number=2)
    await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="stale-claim",
        visibility="public",
        payload={"status": "running"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()
    dispatcher = OutboxDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(claim_timeout_seconds=30),
    )

    first = await dispatcher._claim(db)
    assert len(first) == 1
    row = first[0]
    old_token = row.claim_token
    first_attempts = row.attempt_count
    row.claimed_at = row.claimed_at.replace(year=2020)
    await db.commit()

    second = await dispatcher._claim(db)
    assert len(second) == 1
    reclaimed = second[0]
    assert reclaimed.id == row.id
    assert reclaimed.claim_token != old_token
    assert reclaimed.attempt_count == first_attempts + 1

    await dispatcher._finish_success(db, reclaimed.id, old_token)
    still_claimed = await db.get(ActivityOutbox, reclaimed.id)
    assert still_claimed.status == "claimed"
    assert still_claimed.claim_token == reclaimed.claim_token

    await dispatcher._finish_failure(
        db, reclaimed.id, old_token, RuntimeError("stale worker")
    )
    still_claimed = await db.get(ActivityOutbox, reclaimed.id)
    assert still_claimed.status == "claimed"
    assert still_claimed.claim_token == reclaimed.claim_token

    await dispatcher._finish_success(db, reclaimed.id, reclaimed.claim_token)
    assert (await db.get(ActivityOutbox, reclaimed.id)).status == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_kind", ["success", "failure", "cancel"])
async def test_outbox_old_session_token_cannot_fence_new_claim(
    finish_kind,
):
    engine, factory = _database()
    first_session = factory()
    second_session = factory()
    first = AsyncSqliteAdapter(first_session)
    second = AsyncSqliteAdapter(second_session)
    try:
        activity_session = _seed_session(first, number=77)
        await append_event_and_outbox(
            first,
            session_id=activity_session.id,
            event_type="cross-session-fencing",
            visibility="public",
            payload={"status": "running"},
            recipient_user_ids=["user-a"],
        )
        await first.commit()
        config = OutboxDispatcherConfig(
            claim_timeout_seconds=30,
            retry_policy=OutboxRetryPolicy(initial_delay_seconds=0),
        )
        old_dispatcher = OutboxDispatcher(
            lambda: None, authorizer=lambda **_kwargs: True, config=config
        )
        new_dispatcher = OutboxDispatcher(
            lambda: None, authorizer=lambda **_kwargs: True, config=config
        )
        old_claim = (await old_dispatcher._claim(first))[0]
        old_claim.claimed_at = old_claim.claimed_at.replace(year=2020)
        await first.commit()
        new_claim = (await new_dispatcher._claim(second))[0]
        assert new_claim.claim_token != old_claim.claim_token

        if finish_kind == "success":
            await old_dispatcher._finish_success(
                first, old_claim.id, old_claim.claim_token
            )
        elif finish_kind == "failure":
            await old_dispatcher._finish_failure(
                first, old_claim.id, old_claim.claim_token, RuntimeError("late")
            )
        else:
            await old_dispatcher._cancel_unauthorised(
                first, old_claim.id, old_claim.claim_token
            )

        current = await second.get(ActivityOutbox, new_claim.id)
        assert current.status == "claimed"
        assert current.claim_token == new_claim.claim_token
        await new_dispatcher._finish_success(
            second, new_claim.id, new_claim.claim_token
        )
        assert (await second.get(ActivityOutbox, new_claim.id)).status == "published"
    finally:
        first_session.close()
        second_session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_batch_continues_after_stale_row_rollback():
    engine, factory = _database()
    claim_session = factory()
    rival_session = factory()
    finish_session = factory()
    claim_db = AsyncSqliteAdapter(claim_session)
    finish_db = AsyncSqliteAdapter(finish_session)
    try:
        activity_session = _seed_session(claim_db, number=99)
        await append_event_and_outbox(
            claim_db,
            session_id=activity_session.id,
            event_type="batch-one",
            visibility="public",
            payload={"status": "one"},
            recipient_user_ids=["user-a"],
        )
        await append_event_and_outbox(
            claim_db,
            session_id=activity_session.id,
            event_type="batch-two",
            visibility="public",
            payload={"status": "two"},
            recipient_user_ids=["user-b"],
        )
        await claim_db.commit()
        first_seen = False

        async def authorizer(*, user_id, **_kwargs):
            nonlocal first_seen
            if not first_seen:
                first_seen = True
                row = (
                    rival_session.query(ActivityOutbox)
                    .order_by(ActivityOutbox.id)
                    .first()
                )
                row.claim_token = "rival-token"
                rival_session.commit()
            return True

        published = []

        async def publisher(user_id, notification):
            published.append((user_id, notification.event_id))

        dispatcher = OutboxDispatcher(
            lambda: SessionContext(finish_db),
            authorizer=authorizer,
            publisher=publisher,
            config=OutboxDispatcherConfig(batch_size=2),
        )
        assert await dispatcher.dispatch_once() == 1
        rows = rival_session.query(ActivityOutbox).order_by(ActivityOutbox.id).all()
        assert rows[0].status == "claimed"
        assert rows[1].status == "published"
        assert len(published) == 2
    finally:
        claim_session.close()
        rival_session.close()
        finish_session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_does_not_reclaim_claim_when_timeout_is_disabled(db):
    activity_session = _seed_session(db, number=100)
    await append_event_and_outbox(
        db,
        session_id=activity_session.id,
        event_type="stale-claim-disabled",
        visibility="public",
        payload={"status": "running"},
        recipient_user_ids=["user-a"],
    )
    await db.commit()
    dispatcher = OutboxDispatcher(
        lambda: None,
        authorizer=lambda **_kwargs: True,
        config=OutboxDispatcherConfig(claim_timeout_seconds=None),
    )
    first = await dispatcher._claim(db)
    first[0].claimed_at = first[0].claimed_at.replace(year=2020)
    await db.commit()

    assert await dispatcher._claim(db) == []
    row = await db.get(ActivityOutbox, first[0].id)
    assert row.status == "claimed"
    assert row.attempt_count == 1
