"""Activity observability write and aggregation service tests."""

from __future__ import annotations

import asyncio
import re
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityInvocationTrigger,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
    ActivityThread,
    ActivityTrigger,
)
from backend.models.database import Base
from backend.services.activity_observability.contracts import (
    InvocationContext,
    PublicActivityNotification,
    RoleConfigSnapshot,
)
from backend.services.activity_observability.service import (
    ActivityObservabilityService,
    ConflictError,
)


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_for_sqlite(_type, _compiler, **_kwargs):
    return "TEXT"


class _AsyncSessionAdapter:
    """Expose the AsyncSession subset over SQLite's synchronous test session."""

    def __init__(self, session: Session):
        self._session = session
        self.events: list[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self._fault_model = None
        self._winner_factory = None
        self._fault_candidate = None
        self._fault_consumed = False
        self._seeding_winner = False

    def inject_unique_race(self, model, winner_factory):
        self._fault_model = model
        self._winner_factory = winner_factory
        self._fault_candidate = None
        self._fault_consumed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self._session.rollback()
        return False

    def add(self, instance):
        self._session.add(instance)
        if not self._seeding_winner:
            self.events.append(f"add:{type(instance).__name__}")

    async def execute(self, statement):
        if not self._seeding_winner:
            entity = statement.column_descriptions[0].get("entity")
            name = statement.column_descriptions[0].get("name")
            if name == "source_system_instance":
                query_name = "resource_source"
            elif entity is ActivityResourceIdentity:
                query_name = "identity"
            elif entity is ActivityObservabilitySession:
                query_name = "session"
            elif entity is ActivityTrigger:
                query_name = "trigger"
            else:
                query_name = "other"
            suffix = (
                "recovery_for_update"
                if statement._for_update_arg is not None
                else "initial"
            )
            self.events.append(f"execute:{query_name}:{suffix}")
        return self._session.execute(statement)

    async def get(self, model, object_id, **kwargs):
        return self._session.get(model, object_id, **kwargs)

    async def flush(self):
        if self._fault_model is not None and not self._fault_consumed:
            for candidate in tuple(self._session.new):
                if isinstance(candidate, self._fault_model):
                    self._session.expunge(candidate)
                    self._fault_candidate = candidate
                    self._fault_consumed = True
                    self.events.append(f"flush:raise:{type(candidate).__name__}")
                    raise IntegrityError("statement", {}, Exception("duplicate"))
        self._session.flush()
        if not self._seeding_winner:
            self.events.append("flush:ok")

    async def commit(self):
        self.commit_calls += 1
        self._session.commit()

    async def rollback(self):
        self.rollback_calls += 1
        self._session.rollback()

    async def refresh(self, instance):
        self._session.refresh(instance)

    def begin_nested(self):
        return _AsyncNestedTransaction(self, self._session.begin_nested())


class _AsyncNestedTransaction:
    def __init__(self, adapter, transaction):
        self._adapter = adapter
        self._transaction = transaction

    async def __aenter__(self):
        self._transaction.__enter__()
        self._adapter.events.append("nested_enter")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        result = self._transaction.__exit__(exc_type, exc, traceback)
        if exc_type is not None:
            self._adapter.events.append("nested_rollback")
            if self._adapter._fault_candidate is not None:
                winner = self._adapter._winner_factory()
                self._adapter._fault_candidate = None
                self._adapter._seeding_winner = True
                try:
                    self._adapter._session.add(winner)
                    self._adapter._session.flush()
                finally:
                    self._adapter._seeding_winner = False
        else:
            self._adapter.events.append("nested_commit")
        return result


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
def service(db_session):
    return ActivityObservabilityService(db=db_session)


@pytest.fixture
def reviewer_snapshot():
    return RoleConfigSnapshot(
        role="reviewer",
        requested_provider="anthropic",
        requested_model="requested-review-model",
        requested_thinking_mode="adaptive",
        candidate_chain=(("anthropic", "requested-review-model"),),
        account_id="review-account",
        protocol_family="anthropic_native",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=3,
        captured_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


@pytest.fixture
def label_snapshot():
    return RoleConfigSnapshot(
        role="label_recommender",
        requested_provider="openai",
        requested_model="requested-label-model",
        requested_thinking_mode=None,
        candidate_chain=(
            ("openai", "requested-label-model"),
            ("openai", "fallback-label-model"),
        ),
        account_id="label-account",
        protocol_family="openai_compatible",
        endpoint_fingerprint="b" * 64,
        config_snapshot_version=4,
        captured_at=datetime(2026, 7, 18, 1, tzinfo=UTC),
    )


async def _create_session(service):
    return await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="987654",
        resource_type="pr",
        resource_number=42,
        repo_full_name="owner/repo",
    )


def test_role_config_snapshot_is_deeply_immutable(reviewer_snapshot):
    with pytest.raises(FrozenInstanceError):
        reviewer_snapshot.role = "other"

    with pytest.raises(TypeError, match="candidate_chain"):
        RoleConfigSnapshot(
            role="reviewer",
            requested_provider="anthropic",
            requested_model="model",
            requested_thinking_mode=None,
            candidate_chain=[["anthropic", "model"]],
            account_id="account",
            protocol_family="anthropic_native",
            endpoint_fingerprint="c" * 64,
            config_snapshot_version=1,
            captured_at=datetime.now(UTC),
        )


def test_role_config_snapshot_rejects_mutable_values():
    valid = {
        "role": "reviewer",
        "requested_provider": "anthropic",
        "requested_model": "model",
        "requested_thinking_mode": None,
        "candidate_chain": (("anthropic", "model"),),
        "account_id": "account",
        "protocol_family": "anthropic_native",
        "endpoint_fingerprint": "c" * 64,
        "config_snapshot_version": 1,
        "captured_at": datetime.now(UTC),
    }

    for field_name in (
        "role",
        "requested_provider",
        "requested_model",
        "requested_thinking_mode",
        "account_id",
        "protocol_family",
        "endpoint_fingerprint",
    ):
        values = valid | {field_name: []}
        with pytest.raises(TypeError, match=field_name):
            RoleConfigSnapshot(**values)

    with pytest.raises(ValueError, match="endpoint_fingerprint"):
        RoleConfigSnapshot(**(valid | {"endpoint_fingerprint": "not-a-digest"}))


@pytest.mark.parametrize(
    ("overrides", "error_field"),
    [
        ({"invocation_id": []}, "invocation_id"),
        ({"work_unit_id": {}}, "work_unit_id"),
        ({"thread_id": []}, "thread_id"),
        ({"role_snapshot": {}}, "role_snapshot"),
    ],
)
def test_invocation_context_rejects_mutable_values(
    reviewer_snapshot, overrides, error_field
):
    values = {
        "invocation_id": 1,
        "work_unit_id": 2,
        "thread_id": None,
        "role_snapshot": reviewer_snapshot,
    }

    with pytest.raises(TypeError, match=error_field):
        InvocationContext(**(values | overrides))


def test_public_notification_exposes_internal_routing_contract():
    notification = PublicActivityNotification(
        event_id="event-1",
        session_id=9,
        invocation_id=10,
        work_unit_id=None,
        sequence=7,
        projection_version=2,
        created_at=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert {field.name for field in fields(notification)} == {
        "event_id",
        "session_id",
        "invocation_id",
        "work_unit_id",
        "sequence",
        "projection_version",
        "created_at",
    }
    with pytest.raises(FrozenInstanceError):
        notification.sequence = 8


def test_public_notification_rejects_mutable_values():
    valid = {
        "event_id": "event-1",
        "session_id": 9,
        "invocation_id": 10,
        "work_unit_id": None,
        "sequence": 7,
        "projection_version": 2,
        "created_at": datetime(2026, 7, 18, tzinfo=UTC),
    }

    for field_name in valid:
        with pytest.raises(TypeError, match=field_name):
            PublicActivityNotification(**(valid | {field_name: []}))


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("role", "Bearer secret-value"),
        ("requested_provider", "https://provider.example"),
        ("requested_model", "api_key=secret-value"),
        ("requested_thinking_mode", "Authorization: Bearer secret-value"),
        ("account_id", "https://account.example"),
        ("protocol_family", "credential=secret-value"),
    ],
)
def test_role_snapshot_rejects_sensitive_values_in_every_persisted_text_field(
    reviewer_snapshot, field_name, unsafe_value
):
    from dataclasses import replace

    unsafe = replace(reviewer_snapshot, **{field_name: unsafe_value})
    with pytest.raises(ValueError, match=field_name):
        ActivityObservabilityService._build_role_binding_snapshot(unsafe)


@pytest.mark.asyncio
async def test_persisted_role_snapshot_contains_no_url_or_credentials_and_belongs_to_work_unit(
    service, db_session, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "snapshot-safe", "b", "h"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    work_unit = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    stored = await db_session.get(
        ActivityObservabilityRoleBindingSnapshot, work_unit.role_binding_snapshot_id
    )
    assert stored is not None
    assert stored.id == work_unit.role_binding_snapshot_id
    serialized = " ".join(
        str(getattr(stored, field))
        for field in (
            "role",
            "requested_provider",
            "requested_model",
            "requested_thinking_mode",
            "candidate_chain_json",
            "account_id",
            "protocol_family",
            "endpoint_fingerprint",
        )
        if getattr(stored, field) is not None
    )
    assert not re.search(
        r"https?://|authorization|bearer|api[_-]?key|access[_-]?token|password|secret|credential",
        serialized,
        re.IGNORECASE,
    )
    assert stored.candidate_chain_json == '[["anthropic","requested-review-model"]]'


@pytest.mark.asyncio
async def test_create_work_unit_rejects_thread_from_another_session(
    service, db_session, reviewer_snapshot
):
    from backend.models.activity_observability_models import ActivityThread

    first = await _create_session(service)
    second = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="thread-other",
        resource_type="pr",
        resource_number=43,
        repo_full_name="owner/other",
    )
    thread = ActivityThread(session_id=second.id, thread_purpose="reviewer", last_seq=0)
    db_session.add(thread)
    await db_session.flush()
    trigger = await service.create_delivery_trigger(first.id, "thread-owner", "b", "h")
    invocation = await service.create_invocation(first.id, (trigger.id,))
    with pytest.raises(ValueError, match="thread"):
        await service.create_work_unit(
            invocation.id,
            "reviewer",
            "primary_required",
            True,
            role_snapshot=reviewer_snapshot,
            thread_id=thread.id,
        )


@pytest.mark.asyncio
async def test_create_invocation_flushes_all_trigger_links_and_session_state_without_committing_caller_transaction(
    service, db_session
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "flush-invocation", "b", "h"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    db_session._session.expire_all()
    links = (
        (
            await db_session.execute(
                select(ActivityInvocationTrigger).where(
                    ActivityInvocationTrigger.invocation_id == invocation.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert (
        db_session._session.get(ActivityResourceIdentity, session.resource_identity_id)
        is not None
    )


@pytest.mark.asyncio
async def test_create_work_unit_flushes_snapshot_primary_summary_without_committing_caller_transaction(
    service, db_session, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "flush-work-unit", "b", "h"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    work_unit = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    db_session._session.expire_all()
    stored = db_session._session.get(
        ActivityObservabilityRoleBindingSnapshot, work_unit.role_binding_snapshot_id
    )
    refreshed_invocation = await service.get_invocation(invocation.id)
    assert stored is not None
    assert refreshed_invocation.primary_work_unit_id == work_unit.id


@pytest.mark.asyncio
async def test_finish_work_unit_flushes_aggregate_without_committing_caller_transaction(
    service, db_session, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "flush-finish", "b", "h"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    work_unit = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    await service.finish_work_unit(work_unit.id, "completed")
    db_session._session.expire_all()
    refreshed_invocation = await service.get_invocation(invocation.id)
    assert refreshed_invocation.status == "completed"


@pytest.mark.asyncio
async def test_identity_nested_insert_fault_preserves_outer_transaction(
    service, db_session
):
    winner = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="identity-race",
        resource_type="pr",
        resource_number="42",
        repo_full_name="winner/repo",
    )
    db_session.inject_unique_race(
        ActivityResourceIdentity,
        lambda: winner,
    )

    session = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="identity-race",
        resource_type="pr",
        resource_number=42,
        repo_full_name="winner/repo",
    )

    marker = ActivityThread(
        session_id=session.id,
        thread_purpose="identity-race-marker",
        last_seq=0,
    )
    db_session.add(marker)
    await db_session.flush()

    assert session.resource_identity_id == winner.id
    assert db_session.commit_calls == 0
    assert db_session.rollback_calls == 0
    assert db_session.events == [
        "execute:session:initial",
        "execute:identity:initial",
        "nested_enter",
        "add:ActivityResourceIdentity",
        "flush:raise:ActivityResourceIdentity",
        "nested_rollback",
        "execute:identity:recovery_for_update",
        "nested_enter",
        "add:ActivityObservabilitySession",
        "flush:ok",
        "nested_commit",
        "add:ActivityThread",
        "flush:ok",
    ]


@pytest.mark.asyncio
async def test_session_nested_insert_fault_preserves_outer_transaction(
    service, db_session
):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="session-race",
        resource_type="pr",
        resource_number="42",
        repo_full_name="owner/repo",
    )
    db_session.add(identity)
    await db_session.flush()
    db_session.events.clear()
    winner = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    db_session.inject_unique_race(
        ActivityObservabilitySession,
        lambda: winner,
    )

    session = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="session-race",
        resource_type="pr",
        resource_number=42,
        repo_full_name="owner/repo",
    )

    marker = ActivityThread(
        session_id=session.id,
        thread_purpose="session-race-marker",
        last_seq=0,
    )
    db_session.add(marker)
    await db_session.flush()

    assert session is winner
    assert db_session.commit_calls == 0
    assert db_session.rollback_calls == 0
    assert db_session.events == [
        "execute:session:initial",
        "execute:identity:initial",
        "nested_enter",
        "add:ActivityObservabilitySession",
        "flush:raise:ActivityObservabilitySession",
        "nested_rollback",
        "execute:session:recovery_for_update",
        "add:ActivityThread",
        "flush:ok",
    ]


@pytest.mark.asyncio
async def test_trigger_nested_insert_fault_preserves_outer_transaction(
    service, db_session
):
    session = await _create_session(service)
    db_session.events.clear()
    dedupe_key = "github.com:delivery:trigger-race"
    winner = ActivityTrigger(
        session_id=session.id,
        trigger_kind="delivery",
        status="pending",
        dedupe_key=dedupe_key,
        source_delivery_id="trigger-race",
        base_sha="winner-base",
        head_sha="winner-head",
    )
    db_session.inject_unique_race(ActivityTrigger, lambda: winner)

    trigger = await service.create_delivery_trigger(
        session.id, "trigger-race", "different-base", "different-head"
    )

    marker = ActivityThread(
        session_id=session.id,
        thread_purpose="trigger-race-marker",
        last_seq=0,
    )
    db_session.add(marker)
    await db_session.flush()

    assert trigger is winner
    assert db_session.commit_calls == 0
    assert db_session.rollback_calls == 0
    assert db_session.events == [
        "execute:resource_source:initial",
        "execute:trigger:initial",
        "nested_enter",
        "add:ActivityTrigger",
        "flush:raise:ActivityTrigger",
        "nested_rollback",
        "execute:trigger:recovery_for_update",
        "add:ActivityThread",
        "flush:ok",
    ]


@pytest.mark.asyncio
async def test_existing_identity_without_session_updates_repo_name_before_session_create(
    service, db_session
):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="identity-rename-before-session",
        resource_type="pr",
        resource_number="42",
        repo_full_name="old-owner/old-repo",
    )
    db_session.add(identity)
    await db_session.flush()

    session = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="identity-rename-before-session",
        resource_type="pr",
        resource_number=42,
        repo_full_name="new-owner/new-repo",
    )

    assert session.resource_identity_id == identity.id
    assert identity.repo_full_name == "new-owner/new-repo"
    assert (
        await db_session.get(ActivityResourceIdentity, identity.id)
    ).repo_full_name == ("new-owner/new-repo")


@pytest.mark.asyncio
async def test_session_insert_race_updates_winner_repo_name_and_owned_service_commits(
    service, db_session, monkeypatch
):
    from backend.models import database as db_module

    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="session-rename-race",
        resource_type="pr",
        resource_number="42",
        repo_full_name="old-owner/old-repo",
    )
    db_session.add(identity)
    await db_session.flush()
    owned_db = _AsyncSessionAdapter(db_session._session)
    winner = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    owned_db.inject_unique_race(ActivityObservabilitySession, lambda: winner)
    monkeypatch.setattr(db_module, "async_session", lambda: owned_db)

    result = await ActivityObservabilityService().get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="session-rename-race",
        resource_type="pr",
        resource_number=42,
        repo_full_name="new-owner/new-repo",
    )

    assert result is winner
    assert result.resource_identity.repo_full_name == "new-owner/new-repo"
    assert (
        db_session._session.get(ActivityResourceIdentity, identity.id).repo_full_name
        == "new-owner/new-repo"
    )
    assert owned_db.commit_calls == 1
    assert owned_db.rollback_calls == 0


@pytest.mark.asyncio
async def test_snapshot_allows_colon_delimited_non_secret_identifiers(
    reviewer_snapshot,
):
    from dataclasses import replace

    valid = replace(
        reviewer_snapshot,
        requested_model="llama3:8b",
        account_id="tenant:123",
        protocol_family="vendor:v2",
    )

    stored = ActivityObservabilityService._build_role_binding_snapshot(valid)

    assert stored.requested_model == "llama3:8b"
    assert stored.account_id == "tenant:123"
    assert stored.protocol_family == "vendor:v2"


@pytest.mark.parametrize(
    ("query_builder", "arguments"),
    [
        (
            "_identity_query",
            (
                {
                    "source_system_instance": "github.com",
                    "repository_external_id": "1",
                    "resource_type": "pr",
                    "resource_number": "1",
                },
            ),
        ),
        (
            "_session_query",
            (
                {
                    "source_system_instance": "github.com",
                    "repository_external_id": "1",
                    "resource_type": "pr",
                    "resource_number": "1",
                },
            ),
        ),
        ("_trigger_query", ("github.com:delivery:1",)),
    ],
)
def test_recovery_queries_compile_with_mysql_for_update(query_builder, arguments):
    from backend.services.activity_observability import service as service_module

    statement = getattr(service_module, query_builder)(*arguments, for_update=True)
    assert "FOR UPDATE" in str(statement.compile(dialect=mysql_dialect()))


@pytest.mark.asyncio
async def test_required_failure_aggregates_invocation_failed(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "required-failure", "b", "h"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    primary = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    required = await service.create_work_unit(
        invocation.id, "required-check", "required", False, role_snapshot=label_snapshot
    )
    await service.finish_work_unit(primary.id, "completed")
    await service.finish_work_unit(
        required.id, "failed", error_message="required failed"
    )
    assert (await service.get_invocation(invocation.id)).status == "failed"


@pytest.mark.asyncio
async def test_all_non_detached_success_aggregates_completed(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(session.id, "all-success", "b", "h")
    invocation = await service.create_invocation(session.id, (trigger.id,))
    primary = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    required = await service.create_work_unit(
        invocation.id, "required-check", "required", False, role_snapshot=label_snapshot
    )
    await service.finish_work_unit(primary.id, "completed")
    await service.finish_work_unit(required.id, "completed")
    assert (await service.get_invocation(invocation.id)).status == "completed"


@pytest.mark.asyncio
async def test_invocation_without_primary_does_not_reach_terminal_status(
    service, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(session.id, "no-primary", "b", "h")
    invocation = await service.create_invocation(session.id, (trigger.id,))
    required = await service.create_work_unit(
        invocation.id, "required-check", "required", False, role_snapshot=label_snapshot
    )
    await service.finish_work_unit(required.id, "completed")
    assert (await service.get_invocation(invocation.id)).status == "queued"


@pytest.mark.asyncio
async def test_production_invocation_rejects_empty_trigger_ids(service):
    session = await _create_session(service)
    with pytest.raises(ValueError, match="trigger_ids"):
        await service.create_invocation(session.id, ())


@pytest.mark.asyncio
async def test_session_rename_updates_display_name_without_changing_identity(
    service, db_session
):
    first = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="rename-1",
        resource_type="pr",
        resource_number=7,
        repo_full_name="old-owner/old-repo",
    )
    renamed = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="rename-1",
        resource_type="pr",
        resource_number=7,
        repo_full_name="new-owner/new-repo",
    )

    assert renamed.id == first.id
    identity = await db_session.get(
        ActivityResourceIdentity, first.resource_identity_id
    )
    assert identity.repo_full_name == "new-owner/new-repo"


@pytest.mark.asyncio
async def test_invocation_sha_range_follows_trigger_input_order(service):
    session = await _create_session(service)
    first = await service.create_delivery_trigger(
        session.id, "order-first", "base-first", "head-first"
    )
    second = await service.create_delivery_trigger(
        session.id, "order-second", "base-second", "head-second"
    )

    invocation = await service.create_invocation(session.id, (second.id, first.id))

    assert invocation.base_sha == "base-second"
    assert invocation.initial_head_sha == "head-second"
    assert invocation.final_head_sha == "head-first"


@pytest.mark.asyncio
async def test_role_snapshot_rejects_endpoint_url_in_candidate_chain(
    service, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "unsafe-chain", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    unsafe = RoleConfigSnapshot(
        role=reviewer_snapshot.role,
        requested_provider=reviewer_snapshot.requested_provider,
        requested_model=reviewer_snapshot.requested_model,
        requested_thinking_mode=reviewer_snapshot.requested_thinking_mode,
        candidate_chain=(("anthropic", "https://api.example.test/v1"),),
        account_id=reviewer_snapshot.account_id,
        protocol_family=reviewer_snapshot.protocol_family,
        endpoint_fingerprint=reviewer_snapshot.endpoint_fingerprint,
        config_snapshot_version=reviewer_snapshot.config_snapshot_version,
        captured_at=reviewer_snapshot.captured_at,
    )

    with pytest.raises(ValueError, match="candidate_chain"):
        await service.create_work_unit(
            invocation.id,
            "reviewer",
            "primary_required",
            True,
            role_snapshot=unsafe,
        )


@pytest.mark.asyncio
async def test_cross_session_trigger_rejection_does_not_consume_trigger(service):
    first_session = await _create_session(service)
    second_session = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="other-repository",
        resource_type="pr",
        resource_number=42,
        repo_full_name="owner/other-repository",
    )
    trigger = await service.create_delivery_trigger(
        first_session.id, "cross-session", "base", "head"
    )

    with pytest.raises(ValueError, match="session"):
        await service.create_invocation(second_session.id, (trigger.id,))

    assert (
        await service.get_work_unit(trigger.id) if False else trigger
    ).status == "pending"


@pytest.mark.asyncio
async def test_service_flushes_without_owning_caller_transaction(service, db_session):
    session = await _create_session(service)
    assert session.id is not None
    identity = await db_session.get(
        ActivityResourceIdentity, session.resource_identity_id
    )
    assert identity is not None
    db_session._session.rollback()
    assert session.id is not None

    with pytest.raises(ValueError, match="source_system_instance"):
        await service.get_or_create_session(
            source_system_instance="  ",
            repository_external_id="987654",
            resource_type="pr",
            resource_number=42,
            repo_full_name="owner/repo",
        )


@pytest.mark.asyncio
async def test_get_or_create_session_repeat_and_concurrent_calls_return_same_session(
    service, db_session
):
    kwargs = {
        "source_system_instance": " GitHub.COM ",
        "repository_external_id": " 987654 ",
        "resource_type": " PR ",
        "resource_number": 42,
        "repo_full_name": " owner/repo ",
    }

    first, second = await asyncio.gather(
        service.get_or_create_session(**kwargs),
        service.get_or_create_session(**kwargs),
    )
    repeated = await service.get_or_create_session(**kwargs)

    assert first.id == second.id == repeated.id
    assert first is second is repeated
    rows = (await db_session.execute(select(ActivityResourceIdentity))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_system_instance == "github.com"
    assert rows[0].resource_type == "pr"
    assert first.session_kind == "long_lived"


@pytest.mark.asyncio
async def test_create_role_binding_snapshot_persists_stable_candidate_chain(
    service, db_session, label_snapshot
):
    stored = await service.create_role_binding_snapshot(label_snapshot)

    row = await db_session.get(ActivityObservabilityRoleBindingSnapshot, stored.id)
    assert row.candidate_chain_json == (
        '[["openai","requested-label-model"],["openai","fallback-label-model"]]'
    )
    assert row.captured_at == label_snapshot.captured_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_redelivery_dedupe_returns_same_trigger(service, db_session):
    session = await _create_session(service)

    first = await service.create_delivery_trigger(
        session.id,
        " delivery-1 ",
        "base-a",
        "head-a",
        source_system_instance=" GitHub.COM ",
    )
    repeated = await service.create_delivery_trigger(
        session.id,
        "delivery-1",
        "different-base",
        "different-head",
        source_system_instance="github.com",
    )

    assert repeated.id == first.id
    assert repeated.dedupe_key == "github.com:delivery:delivery-1"
    rows = (await db_session.execute(select(ActivityTrigger))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_manual_trigger_dedupe_returns_same_trigger(service):
    session = await _create_session(service)

    first = await service.create_manual_trigger(
        session.id, "actor-7", "nonce-1", "review"
    )
    repeated = await service.create_manual_trigger(
        session.id, " actor-7 ", " nonce-1 ", " review "
    )

    assert repeated.id == first.id
    assert repeated.dedupe_key == "manual:actor-7:nonce-1"
    assert repeated.metadata_json == '{"purpose":"review"}'


@pytest.mark.asyncio
async def test_manual_trigger_purpose_does_not_change_dedupe(service):
    session = await _create_session(service)

    first = await service.create_manual_trigger(
        session.id, "actor-7", "nonce-1", "review"
    )
    repeated = await service.create_manual_trigger(
        session.id, "actor-7", "nonce-1", "retry"
    )

    assert repeated.id == first.id
    assert repeated.dedupe_key == "manual:actor-7:nonce-1"
    assert repeated.metadata_json == '{"purpose":"review"}'


@pytest.mark.asyncio
async def test_many_triggers_merge_into_one_invocation_with_sha_range(
    service, db_session
):
    session = await _create_session(service)
    first = await service.create_delivery_trigger(
        session.id,
        "delivery-1",
        "base-a",
        "head-b",
        source_system_instance="github.com",
    )
    second = await service.create_delivery_trigger(
        session.id,
        "delivery-2",
        "head-b",
        "head-c",
        source_system_instance="github.com",
    )

    invocation = await service.create_invocation(session.id, (first.id, second.id))

    assert invocation.base_sha == "base-a"
    assert invocation.initial_head_sha == "head-b"
    assert invocation.final_head_sha == "head-c"
    assert invocation.primary_work_unit_id is None
    assert first.status == second.status == "consumed"
    links = (
        (
            await db_session.execute(
                select(ActivityInvocationTrigger).where(
                    ActivityInvocationTrigger.invocation_id == invocation.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {link.trigger_id for link in links} == {first.id, second.id}


@pytest.mark.asyncio
async def test_consumed_trigger_cannot_be_used_by_another_invocation(service):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "delivery-1", "base", "head", source_system_instance="github.com"
    )
    await service.create_invocation(session.id, (trigger.id,))

    with pytest.raises(ConflictError, match="consumed"):
        await service.create_invocation(session.id, (trigger.id,))


@pytest.mark.asyncio
async def test_second_primary_work_unit_raises_conflict(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "delivery-1", "base", "head", source_system_instance="github.com"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )

    with pytest.raises(ConflictError, match="primary"):
        await service.create_work_unit(
            invocation.id,
            "other-reviewer",
            "primary_required",
            True,
            role_snapshot=label_snapshot,
        )


@pytest.mark.asyncio
async def test_best_effort_failure_makes_invocation_partial_without_overwriting_primary(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "delivery-1", "base", "head", source_system_instance="github.com"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    reviewer = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    labels = await service.create_work_unit(
        invocation.id,
        "label_recommendation",
        "best_effort",
        False,
        role_snapshot=label_snapshot,
    )

    await service.finish_work_unit(
        reviewer.id,
        "completed",
        final_provider="anthropic",
        final_model="review-model",
        final_thinking_mode="adaptive",
    )
    pending = await service.get_invocation(invocation.id)
    assert pending.status == "queued"

    await service.finish_work_unit(
        labels.id,
        "failed",
        error_message="provider unavailable",
        final_provider="openai",
        final_model="label-model",
    )

    refreshed = await service.get_invocation(invocation.id)
    refreshed_labels = await service.get_work_unit(labels.id)
    assert refreshed.status == "partial"
    assert refreshed.primary_work_unit_id == reviewer.id
    assert refreshed.primary_final_provider == "anthropic"
    assert refreshed.primary_final_model == "review-model"
    assert refreshed.primary_final_thinking_mode == "adaptive"
    assert refreshed_labels.final_model == "label-model"


@pytest.mark.asyncio
async def test_all_unstarted_gating_work_units_cancel_invocation(
    service, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "delivery-1", "base", "head", source_system_instance="github.com"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    reviewer = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )

    await service.finish_work_unit(reviewer.id, "cancelled")

    refreshed = await service.get_invocation(invocation.id)
    assert refreshed.status == "cancelled"
    assert refreshed.cancelled_at is not None


@pytest.mark.asyncio
async def test_delivery_trigger_derives_source_system_instance_from_session(service):
    session = await _create_session(service)

    trigger = await service.create_delivery_trigger(
        session.id, "delivery-derived", "base", "head"
    )

    assert trigger.dedupe_key == "github.com:delivery:delivery-derived"


@pytest.mark.asyncio
async def test_delivery_trigger_rejects_explicit_source_mismatch(service):
    session = await _create_session(service)

    with pytest.raises(ValueError, match="source_system_instance"):
        await service.create_delivery_trigger(
            session.id,
            "delivery-mismatch",
            "base",
            "head",
            source_system_instance="gitlab.com",
        )


@pytest.mark.asyncio
async def test_dedupe_trigger_cannot_be_reused_by_another_session(service):
    first_session = await _create_session(service)
    second_session = await service.get_or_create_session(
        source_system_instance="github.com",
        repository_external_id="987655",
        resource_type="pr",
        resource_number=42,
        repo_full_name="owner/other-repo",
    )
    await service.create_delivery_trigger(
        first_session.id, "shared-delivery", "base", "head"
    )

    with pytest.raises(ConflictError, match="session"):
        await service.create_delivery_trigger(
            second_session.id, "shared-delivery", "base", "head"
        )


@pytest.mark.asyncio
async def test_primary_requirement_and_flag_must_match(service, reviewer_snapshot):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "primary-invariant", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))

    with pytest.raises(ValueError, match="primary"):
        await service.create_work_unit(
            invocation.id,
            "reviewer",
            "primary_required",
            False,
            role_snapshot=reviewer_snapshot,
        )
    with pytest.raises(ValueError, match="primary"):
        await service.create_work_unit(
            invocation.id,
            "reviewer",
            "required",
            True,
            role_snapshot=reviewer_snapshot,
        )


@pytest.mark.asyncio
async def test_role_snapshot_is_required_at_call_boundary(service):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "missing-snapshot", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))

    with pytest.raises(TypeError, match="role_snapshot"):
        await service.create_work_unit(
            invocation.id, "reviewer", "primary_required", True
        )
    with pytest.raises(TypeError, match="role_snapshot"):
        await service.create_work_unit(
            invocation.id,
            "reviewer",
            "primary_required",
            True,
            role_snapshot=None,
        )


@pytest.mark.asyncio
async def test_finish_work_unit_same_terminal_payload_is_idempotent(
    service, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "finish-idempotent", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    work_unit = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )

    await service.finish_work_unit(
        work_unit.id,
        "completed",
        final_provider="anthropic",
        final_model="model",
        final_thinking_mode="adaptive",
    )
    first = await service.get_work_unit(work_unit.id)
    await service.finish_work_unit(
        work_unit.id,
        "completed",
        final_provider="anthropic",
        final_model="model",
        final_thinking_mode="adaptive",
    )
    repeated = await service.get_work_unit(work_unit.id)

    assert repeated.completed_at == first.completed_at
    assert repeated.error_message == first.error_message


@pytest.mark.asyncio
async def test_finish_work_unit_different_terminal_payload_conflicts(
    service, reviewer_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "finish-conflict", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    work_unit = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    await service.finish_work_unit(work_unit.id, "completed")

    with pytest.raises(ConflictError, match="terminal"):
        await service.finish_work_unit(work_unit.id, "failed", error_message="late")

    unchanged = await service.get_work_unit(work_unit.id)
    assert unchanged.status == "completed"
    assert unchanged.error_message is None


@pytest.mark.asyncio
async def test_detached_finish_does_not_rewrite_completed_invocation(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "detached-after-complete", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    primary = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    await service.finish_work_unit(primary.id, "completed")
    completed = await service.get_invocation(invocation.id)
    detached = await service.create_work_unit(
        invocation.id,
        "telemetry",
        "detached",
        False,
        role_snapshot=label_snapshot,
    )

    await service.finish_work_unit(detached.id, "completed")
    refreshed = await service.get_invocation(invocation.id)

    assert refreshed.status == "completed"
    assert refreshed.completed_at == completed.completed_at


@pytest.mark.asyncio
async def test_non_detached_work_unit_cannot_be_added_to_terminal_invocation(
    service, reviewer_snapshot, label_snapshot
):
    session = await _create_session(service)
    trigger = await service.create_delivery_trigger(
        session.id, "late-work-unit", "base", "head"
    )
    invocation = await service.create_invocation(session.id, (trigger.id,))
    primary = await service.create_work_unit(
        invocation.id,
        "reviewer",
        "primary_required",
        True,
        role_snapshot=reviewer_snapshot,
    )
    await service.finish_work_unit(primary.id, "completed")

    with pytest.raises(ConflictError, match="terminal"):
        await service.create_work_unit(
            invocation.id,
            "labels",
            "best_effort",
            False,
            role_snapshot=label_snapshot,
        )
