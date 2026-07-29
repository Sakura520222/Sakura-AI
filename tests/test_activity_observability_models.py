"""Tests for the activity observability ORM models."""

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    event,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityMessage,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityEvent,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityOutbox,
    ActivityPublication,
    ActivityResourceIdentity,
    ActivitySession,
    ActivityThread,
    ActivityThreadLease,
)
from backend.models.database import Base


def _column_names(constraint: UniqueConstraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def _foreign_key_constraint(table, constraint_name: str) -> ForeignKeyConstraint:
    return next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == constraint_name
    )


def _assert_composite_foreign_key(
    table,
    constraint_name: str,
    local_columns: tuple[str, ...],
    target_columns: tuple[str, ...],
    target_table: str,
    ondelete: str | None,
) -> None:
    constraint = _foreign_key_constraint(table, constraint_name)

    assert (
        tuple(element.parent.name for element in constraint.elements) == local_columns
    )
    assert (
        tuple(element.column.name for element in constraint.elements) == target_columns
    )
    assert {element.column.table.name for element in constraint.elements} == {
        target_table
    }
    assert constraint.ondelete == ondelete


def test_publication_marker_column_can_store_full_marker():
    from backend.services.activity_observability.publication_service import (
        publication_marker,
    )

    marker = publication_marker("issue:owner/repo:1")
    assert len(marker) == 89
    assert ActivityPublication.__table__.c.marker.type.length >= len(marker)

    """The resource identity is complete and unique within the observability schema."""
    table = ActivityResourceIdentity.__table__

    for column_name in (
        "source_system_instance",
        "repository_external_id",
        "resource_type",
        "resource_number",
        "repo_full_name",
    ):
        assert table.c[column_name].nullable is False

    assert any(
        constraint.name == "uq_activity_observability_resource_identity"
        for constraint in table.constraints
    )


def test_role_binding_snapshot_exists_and_work_unit_requires_it():
    snapshot_table = ActivityObservabilityRoleBindingSnapshot.__table__
    work_unit_table = ActivityInvocationWorkUnit.__table__

    assert snapshot_table.name == "activity_observability_role_binding_snapshots"
    assert snapshot_table.c.candidate_chain_json.nullable is False
    assert snapshot_table.c.captured_at.nullable is False

    snapshot_column = work_unit_table.c.role_binding_snapshot_id
    assert snapshot_column.nullable is False
    assert {
        foreign_key.target_fullname for foreign_key in snapshot_column.foreign_keys
    } == {"activity_observability_role_binding_snapshots.id"}


def test_session_event_sequence_is_non_nullable_with_zero_default():
    column = ActivitySession.__table__.c.session_event_sequence

    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg == 0


def test_parent_scoped_composite_foreign_keys_are_declared():
    session_table = ActivitySession.__table__
    invocation_table = ActivitySession.metadata.tables[
        "activity_observability_invocations"
    ]
    lease_table = ActivitySession.metadata.tables[
        "activity_observability_thread_leases"
    ]
    event_table = ActivityObservabilityEvent.__table__
    outbox_table = ActivityOutbox.__table__

    _assert_composite_foreign_key(
        session_table,
        "fk_activity_observability_session_last_invocation",
        ("last_invocation_id", "id"),
        ("id", "session_id"),
        "activity_observability_invocations",
        None,
    )
    _assert_composite_foreign_key(
        invocation_table,
        "fk_activity_observability_invocation_primary_work_unit",
        ("primary_work_unit_id", "id"),
        ("id", "invocation_id"),
        "activity_observability_invocation_work_units",
        None,
    )
    _assert_composite_foreign_key(
        lease_table,
        "fk_activity_observability_thread_lease_owner_work_unit",
        ("owner_work_unit_id", "thread_id"),
        ("id", "thread_id"),
        "activity_observability_invocation_work_units",
        "CASCADE",
    )
    _assert_composite_foreign_key(
        outbox_table,
        "fk_activity_observability_outbox_event_session",
        ("event_uuid", "session_id"),
        ("event_uuid", "session_id"),
        "activity_observability_events",
        "CASCADE",
    )

    assert event_table.constraints


def test_composite_summary_foreign_keys_require_explicit_pointer_clear_before_delete():
    session = _sqlite_observability_session()
    activity_session = _session(session, 30)
    invocation = _invocation(session, activity_session)
    work_unit = _work_unit(session, invocation)
    session.commit()

    session.execute(
        update(ActivitySession)
        .where(ActivitySession.id == activity_session.id)
        .values(last_invocation_id=invocation.id)
    )
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            ActivityInvocation.__table__.delete().where(
                ActivityInvocation.id == invocation.id
            )
        )
        session.commit()
    session.rollback()

    session.execute(
        update(ActivitySession)
        .where(ActivitySession.id == activity_session.id)
        .values(last_invocation_id=None)
    )
    session.commit()
    session.execute(
        ActivityInvocation.__table__.delete().where(
            ActivityInvocation.id == invocation.id
        )
    )
    session.commit()

    activity_session = _session(session, 31)
    invocation = _invocation(session, activity_session)
    work_unit = _work_unit(session, invocation)
    session.commit()
    session.execute(
        update(ActivityInvocation)
        .where(ActivityInvocation.id == invocation.id)
        .values(primary_work_unit_id=work_unit.id)
    )
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            ActivityInvocationWorkUnit.__table__.delete().where(
                ActivityInvocationWorkUnit.id == work_unit.id
            )
        )
        session.commit()
    session.rollback()

    session.execute(
        update(ActivityInvocation)
        .where(ActivityInvocation.id == invocation.id)
        .values(primary_work_unit_id=None)
    )
    session.commit()
    session.execute(
        ActivityInvocationWorkUnit.__table__.delete().where(
            ActivityInvocationWorkUnit.id == work_unit.id
        )
    )
    session.commit()
    session.close()


def test_attempt_requires_context_revision_or_contextless_reason():
    table = ActivityModelAttempt.__table__
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert table.c.context_revision_id.nullable is True
    assert table.c.contextless_reason.nullable is True
    assert (
        constraints["ck_activity_observability_attempt_context"]
        == "context_revision_id IS NOT NULL OR contextless_reason IS NOT NULL"
    )
    for column_name in (
        "requested_effort",
        "effective_effort",
        "max_output_tokens",
        "temperature",
        "top_p",
        "top_k",
        "tool_choice",
    ):
        assert table.c[column_name].nullable is True


def test_native_artifact_records_metadata_only_capture_failure():
    table = ActivityNativeArtifact.__table__

    assert table.c.capture_error.nullable is True
    assert table.c.payload_ciphertext.nullable is True


def test_event_visibility_is_non_nullable_and_constrained():
    table = ActivityObservabilityEvent.__table__
    visibility_constraints = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_activity_observability_event_visibility"
    ]

    assert table.c.visibility.nullable is False
    assert visibility_constraints == [
        "visibility IN ('public', 'admin_only', 'internal', 'hidden')"
    ]


def test_outbox_is_user_scoped_and_keeps_unique_event_uuid():
    table = ActivityOutbox.__table__
    unique_constraints = {
        constraint.name: _column_names(constraint)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert table.c.target_user_id.nullable is False
    assert table.c.projection_version.nullable is False
    assert table.c.event_uuid.nullable is False
    assert {
        foreign_key.target_fullname for foreign_key in table.c.event_uuid.foreign_keys
    } == {
        "activity_observability_events.event_uuid",
    }
    assert unique_constraints["uq_activity_observability_outbox_event_uuid"] == (
        "event_uuid",
    )


def test_message_can_reference_native_artifact_with_deferred_foreign_key():
    artifact_column = ActivityMessage.__table__.c.artifact_id
    foreign_key = next(iter(artifact_column.foreign_keys))

    assert artifact_column.nullable is True
    assert foreign_key.target_fullname == "activity_observability_native_artifacts.id"
    assert foreign_key.use_alter is True


def test_thread_current_revision_foreign_key_keeps_same_thread():
    _assert_composite_foreign_key(
        ActivityThread.__table__,
        "fk_activity_observability_thread_current_revision",
        ("id", "current_revision_id"),
        ("thread_id", "id"),
        "activity_observability_canonical_context_revisions",
        None,
    )


def test_work_unit_has_non_nullable_session_and_cross_parent_foreign_keys():
    table = ActivityInvocationWorkUnit.__table__

    assert table.c.session_id.nullable is False

    _assert_composite_foreign_key(
        table,
        "fk_activity_observability_work_unit_invocation_session",
        ("invocation_id", "session_id"),
        ("id", "session_id"),
        "activity_observability_invocations",
        "CASCADE",
    )
    _assert_composite_foreign_key(
        table,
        "fk_activity_observability_work_unit_thread_session",
        ("session_id", "thread_id"),
        ("session_id", "id"),
        "activity_observability_threads",
        None,
    )


def test_event_has_cross_parent_composite_foreign_keys():
    table = ActivityObservabilityEvent.__table__

    _assert_composite_foreign_key(
        table,
        "fk_activity_observability_event_invocation_session",
        ("invocation_id", "session_id"),
        ("id", "session_id"),
        "activity_observability_invocations",
        None,
    )
    _assert_composite_foreign_key(
        table,
        "fk_activity_observability_event_work_unit_session",
        ("work_unit_id", "session_id"),
        ("id", "session_id"),
        "activity_observability_invocation_work_units",
        None,
    )


def test_threadless_child_rows_keep_direct_work_unit_foreign_key():
    for table_name in (
        "activity_observability_work_unit_results",
        "activity_observability_tool_executions",
    ):
        column = ActivitySession.metadata.tables[table_name].c.work_unit_id

        assert "activity_observability_invocation_work_units.id" in {
            foreign_key.target_fullname for foreign_key in column.foreign_keys
        }


def _sqlite_observability_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _):
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

    tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name.startswith("activity_observability_")
    ]
    Base.metadata.create_all(engine, tables=tables)
    return Session(engine)


def _identity(session: Session, number: int, resource_type: str = "pr"):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id=str(number),
        resource_type=resource_type,
        resource_number=str(number),
        repo_full_name=f"owner/repo-{number}",
    )
    session.add(identity)
    session.flush()
    return identity


def _session(session: Session, number: int):
    activity_session = ActivitySession(
        resource_identity_id=_identity(session, number).id,
        session_kind="long_lived",
        status="open",
    )
    session.add(activity_session)
    session.flush()
    return activity_session


def _invocation(session: Session, activity_session: ActivitySession):
    invocation = ActivityInvocation(session_id=activity_session.id, status="queued")
    session.add(invocation)
    session.flush()
    return invocation


def _snapshot(session: Session):
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="anthropic",
        requested_model="model",
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="anthropic_native",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _work_unit(
    session: Session,
    invocation: ActivityInvocation,
    *,
    thread_id: int | None = None,
):
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=invocation.session_id,
        thread_id=thread_id,
        role_binding_snapshot_id=_snapshot(session).id,
        purpose="reviewer",
        requirement="primary_required",
        status="queued",
        is_primary=True,
    )
    session.add(work_unit)
    session.flush()
    return work_unit


def test_sqlite_rejects_cross_parent_scoped_references():
    session = _sqlite_observability_session()
    first_session = _session(session, 1)
    second_session = _session(session, 2)
    first_invocation = _invocation(session, first_session)
    second_invocation = _invocation(session, second_session)
    first_work_unit = _work_unit(session, first_invocation)
    second_work_unit = _work_unit(session, second_invocation)

    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            update(ActivitySession)
            .where(ActivitySession.id == first_session.id)
            .values(last_invocation_id=second_invocation.id)
        )
        session.flush()
    first_invocation_id = first_invocation.id
    second_work_unit_id = second_work_unit.id
    session.rollback()
    session.expire_all()
    first_invocation = session.get(ActivityInvocation, first_invocation_id)
    second_work_unit = session.get(ActivityInvocationWorkUnit, second_work_unit_id)

    with pytest.raises(IntegrityError):
        session.execute(
            update(ActivityInvocation)
            .where(ActivityInvocation.id == first_invocation.id)
            .values(primary_work_unit_id=second_work_unit.id)
        )
        session.flush()
    session.rollback()

    with pytest.raises(IntegrityError):
        session.add(
            ActivityThreadLease(
                thread_id=first_work_unit.thread_id or 999,
                owner_work_unit_id=second_work_unit.id,
                fencing_token=1,
                expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.flush()
    session.rollback()

    event_uuid = "00000000-0000-0000-0000-000000000001"
    event_row = ActivityObservabilityEvent(
        event_uuid=event_uuid,
        session_id=first_session.id,
        event_sequence=1,
        event_type="status",
        visibility="public",
    )
    session.add(event_row)
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(
            ActivityOutbox(
                event_uuid=event_uuid,
                target_user_id="user",
                session_id=second_session.id,
                event_sequence=1,
                projection_version=1,
                payload_json="{}",
            )
        )
        session.flush()
    session.close()


def test_activity_observability_tables_create_on_sqlite():
    """Observability tables use types that SQLite can compile for tests."""
    from sqlalchemy import inspect

    session = _sqlite_observability_session()
    tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name.startswith("activity_observability_")
    ]

    assert set(inspect(session.bind).get_table_names()) == {
        table.name for table in tables
    }
    session.close()


def test_activity_observability_mappers_configure_without_error():
    configure_mappers()
