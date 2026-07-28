"""Focused tests for the Task 8 publication authority."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityObservabilityEvent,
    ActivityOutbox,
    ActivityPublication,
    ActivityResourceIdentity,
    ActivityWorkUnitResult,
)
from backend.models.database import Base

from backend.services.activity_observability.publication_service import (
    PublicationConflictError,
    PublicationLimits,
    PublicationService,
    WorkUnitResultCoordinator,
    build_publication_body,
    coordinate_publication,
    publication_marker,
    request_fingerprint,
    validate_external_key,
)


def _publication_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


class _AsyncNested:
    def __init__(self, transaction):
        self.transaction = transaction

    async def __aenter__(self):
        self.transaction.__enter__()
        return self

    async def __aexit__(self, exc_type, value, traceback):
        return self.transaction.__exit__(exc_type, value, traceback)


class _AsyncDbAdapter:
    def __init__(self, session: Session):
        self.session = session

    async def get(self, model, object_id, **kwargs):
        return self.session.get(model, object_id, **kwargs)

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    def begin_nested(self):
        return _AsyncNested(self.session.begin_nested())



def _publication_chain(db: Session) -> int:
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="repo-1",
        resource_type="pr",
        resource_number="1",
        repo_full_name="owner/repo",
    )
    db.add(identity)
    db.flush()
    activity_session = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
    )
    db.add(activity_session)
    db.flush()
    invocation = ActivityInvocation(session_id=activity_session.id, status="queued")
    db.add(invocation)
    db.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="openai",
        requested_model="model",
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="openai_compatible",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    db.add(snapshot)
    db.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=activity_session.id,
        role_binding_snapshot_id=snapshot.id,
        purpose="reviewer",
        requirement="required",
        is_primary=True,
        status="queued",
    )
    db.add(work_unit)
    db.flush()
    result = ActivityWorkUnitResult(
        work_unit_id=work_unit.id,
        result_kind="review",
        status="generated",
        requires_publication=True,
    )
    db.add(result)
    db.commit()
    return result.id


@pytest.fixture
def publication_service():
    engine, db = _publication_db()
    result_id = _publication_chain(db)
    service = PublicationService(
        db=_AsyncDbAdapter(db),
        recipient_user_ids=("user-a",),
        limits=PublicationLimits(max_reconcile_attempts=2, max_pending_retries=3),
    )
    service._test_result_id = result_id
    yield service
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_create_pending_is_idempotent_and_parent_chain_is_real_db(publication_service):
    first = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:1"
    )
    second = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:1"
    )
    assert first.id == second.id
    assert first.status == "pending"


@pytest.mark.asyncio
async def test_create_pending_uses_recipient_resolver_when_explicit_ids_are_unset():
    engine, db = _publication_db()
    try:
        result_id = _publication_chain(db)

        async def resolve_recipients(**_kwargs):
            return ("resolved-user",)

        service = PublicationService(
            db=_AsyncDbAdapter(db),
            recipient_resolver=resolve_recipients,
        )

        publication = await service.create_pending(
            result_id,
            "issue_comment",
            "issue:owner/repo:resolver",
        )

        assert publication.status == "pending"
        outbox = (await service._db.execute(select(ActivityOutbox))).scalars().all()
        assert [row.target_user_id for row in outbox] == ["resolved-user"]
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_result_coordinator_persists_generated_result_and_returns_stable_id(
    publication_service,
):
    work_unit = (
        await publication_service._db.execute(select(ActivityInvocationWorkUnit))
    ).scalars().one()
    context = SimpleNamespace(
        invocation_id=work_unit.invocation_id,
        work_unit_id=work_unit.id,
        thread_id=None,
    )
    coordinator = WorkUnitResultCoordinator(publication_service)

    first = await coordinator.publish_issue_analysis(
        {"summary": "safe summary"},
        context=context,
    )
    second = await coordinator.publish_issue_analysis(
        {"summary": "safe summary"},
        context=context,
    )

    assert first["_activity_result_id"] == second["_activity_result_id"]
    stored = await publication_service._db.get(
        ActivityWorkUnitResult,
        first["_activity_result_id"],
    )
    assert stored.result_kind == "issue_analysis"
    assert stored.payload_json == '{"summary":"safe summary"}'
    assert stored.requires_publication is True


@pytest.mark.asyncio
async def test_create_pending_recovers_unique_race_inside_savepoint(
    publication_service, monkeypatch
):
    first = await publication_service.create_pending(
        publication_service._test_result_id,
        "issue_comment",
        "issue:owner/repo:race",
    )
    await publication_service._db.commit()
    original_execute = publication_service._db.execute
    initial_lookup = True

    async def hide_existing_for_initial_lookup(statement):
        nonlocal initial_lookup
        if initial_lookup:
            initial_lookup = False
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        return await original_execute(statement)

    monkeypatch.setattr(publication_service._db, "execute", hide_existing_for_initial_lookup)
    recovered = await publication_service.create_pending(
        publication_service._test_result_id,
        "issue_comment",
        "issue:owner/repo:race",
    )
    assert recovered.id == first.id
    rows = (
        await original_execute(select(ActivityPublication))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first.id
    events = (await original_execute(select(ActivityObservabilityEvent))).scalars().all()
    outbox = (await original_execute(select(ActivityOutbox))).scalars().all()
    assert len(events) == 1
    assert len(outbox) == 1
    assert outbox[0].event_uuid == events[0].event_uuid
    await publication_service._db.commit()


@pytest.mark.asyncio
async def test_create_pending_non_key_integrity_error_is_not_recovered(
    publication_service, monkeypatch
):
    existing = await publication_service.create_pending(
        publication_service._test_result_id,
        "issue_comment",
        "issue:owner/repo:non-key-error",
    )
    await publication_service._db.commit()
    original_execute = publication_service._db.execute
    original_error = IntegrityError(
        "statement",
        {},
        Exception(
            "UNIQUE constraint failed: "
            "activity_observability_publications.marker"
        ),
    )
    initial_lookup = True

    async def hide_initial_lookup(statement):
        nonlocal initial_lookup
        if initial_lookup:
            initial_lookup = False
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        return await original_execute(statement)

    async def fail_nested_flush():
        raise original_error

    monkeypatch.setattr(publication_service._db, "execute", hide_initial_lookup)
    monkeypatch.setattr(publication_service._db, "flush", fail_nested_flush)
    with pytest.raises(IntegrityError) as caught:
        await publication_service.create_pending(
            publication_service._test_result_id,
            "issue_comment",
            existing.external_idempotency_key,
        )
    assert caught.value is original_error
    await publication_service._db.rollback()
    assert (
        await publication_service._db.execute(select(ActivityPublication))
    ).scalars().one().id == existing.id
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Duplicate entry for key 'uq_activity_observability_publication_idempotency'",
            True,
        ),
        ("Duplicate entry for key 'external_idempotency_key'", True),
        (
            "Duplicate entry for key 'uq_activity_observability_publication_marker'",
            False,
        ),
        (
            "UNIQUE constraint failed: activity_observability_publications.external_idempotency_key",
            True,
        ),
        (
            "UNIQUE constraint failed: activity_observability_publications.marker",
            False,
        ),
    ],
)
def test_integrity_error_classifier_is_dialect_specific(message, expected):
    from backend.services.activity_observability.publication_service import (
        _is_external_key_integrity_error,
    )

    error = IntegrityError("statement", {}, Exception(message))
    assert _is_external_key_integrity_error(error) is expected


@pytest.mark.asyncio
async def test_state_machine_rejects_illegal_transition_and_conflicting_terminal_identity(
    publication_service,
):
    publication = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:2"
    )
    with pytest.raises(PublicationConflictError):
        await publication_service.mark_succeeded(publication.id, "1")
    sent = await publication_service.mark_sending(publication.id)
    succeeded = await publication_service.mark_succeeded(
        publication.id,
        "1",
        "https://github.com/owner/repo/issues/1",
        claim_token=sent.claim_token,
    )
    assert succeeded.status == "succeeded"
    with pytest.raises(PublicationConflictError):
        await publication_service.mark_succeeded(
            publication.id, "2", claim_token="wrong"
        )


@pytest.mark.asyncio
async def test_reconcile_found_notfound_timeout_and_max_attempts(publication_service):
    class Probe:
        def __init__(self, value):
            self.value = value

        async def find_by_marker(self, *_args):
            if isinstance(self.value, BaseException):
                raise self.value
            return self.value

    found = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:3"
    )
    sent = await publication_service.mark_sending(found.id)
    await publication_service.mark_transport_timeout(found.id, claim_token=sent.claim_token)
    restored = await publication_service.reconcile(
        found.id, Probe({"id": 99, "html_url": "https://github.com/owner/repo/issues/99"}), {}
    )
    assert restored.status == "succeeded" and restored.external_object_id == "99"

    absent = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:4"
    )
    sent = await publication_service.mark_sending(absent.id)
    await publication_service.mark_transport_timeout(absent.id, claim_token=sent.claim_token)
    pending = await publication_service.reconcile(absent.id, Probe(None), {})
    assert pending.status == "pending"

    unknown = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:5"
    )
    sent = await publication_service.mark_sending(unknown.id)
    await publication_service.mark_transport_timeout(unknown.id, claim_token=sent.claim_token)
    still_unknown = await publication_service.reconcile(
        unknown.id, Probe(asyncio.TimeoutError()), {}
    )
    assert still_unknown.status == "unknown"
    failed = await publication_service.reconcile(unknown.id, Probe(None), {})
    assert failed.status == "pending"
    sent = await publication_service.mark_sending(failed.id)
    await publication_service.mark_transport_timeout(failed.id, claim_token=sent.claim_token)
    terminal = await publication_service.reconcile(failed.id, Probe(None), {})
    assert terminal.status == "failed"
    marker = publication_marker("review:owner/repo:7:head")
    assert marker.startswith("<!-- sakura-activity:")
    assert marker.endswith(" -->")
    assert len(marker) == len("<!-- sakura-activity:") + 64 + len(" -->")
    assert request_fingerprint("body", "secret") == request_fingerprint("body", "secret")
    assert "secret" not in request_fingerprint("body", "secret")


@pytest.mark.asyncio
async def test_claim_contract_and_stale_recovery(publication_service):
    publication = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:9"
    )
    claimed = await publication_service.mark_sending(publication.id)
    assert claimed is not None and claimed.claim_token
    assert await publication_service.mark_sending(publication.id) is None
    claimed.started_at = claimed.started_at.replace(year=2020)
    recovered = await publication_service.recover_stale_sending(
        now=claimed.started_at.replace(year=2021)
    )
    assert recovered == 1
    assert claimed.status == "unknown"
    assert claimed.claim_token is None
    publication = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:6"
    )
    failed = await publication_service.send(
        publication.id,
        body=publication_marker("body:marker:collision"),
        sender=lambda *_args: {"id": "never-sent"},
        resource_identity={},
    )
    assert failed.status == "failed"
    assert failed.error_category == "invalid_request"
    assert failed.claim_token is None


@pytest.mark.asyncio
async def test_allowed_enterprise_github_host_is_injected_and_untrusted_host_rejected(
    publication_service,
):
    publication_service.allowed_github_hosts = frozenset({"github.example.com"})
    publication = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:7"
    )
    sent = await publication_service.mark_sending(publication.id)
    accepted = await publication_service.mark_succeeded(
        publication.id,
        "7",
        "https://github.example.com/owner/repo/issues/7",
        claim_token=sent.claim_token,
    )
    assert accepted.external_object_url == "https://github.example.com/owner/repo/issues/7"

    rejected = await publication_service.create_pending(
        publication_service._test_result_id, "issue_comment", "issue:owner/repo:8"
    )
    sent = await publication_service.mark_sending(rejected.id)
    with pytest.raises(ValueError, match="verified GitHub HTTPS"):
        await publication_service.mark_succeeded(
            rejected.id,
            "8",
            "https://github.com/owner/repo/issues/8",
            claim_token=sent.claim_token,
        )
    assert rejected.status == "sending"
    marker = publication_marker("issue:owner/repo:3")
    assert marker in build_publication_body("analysis", marker)
    with pytest.raises(ValueError):
        build_publication_body(f"user text {marker}", marker)
    with pytest.raises(ValueError):
        validate_external_key("https://github.com/owner/repo/issues/3")
    with pytest.raises(ValueError):
        validate_external_key("token:abc")


@pytest.mark.asyncio
async def test_coordinator_requires_context_and_routes_issue():
    class FakeCoordinator:
        async def publish_issue_analysis(self, result, *, context):
            assert context.invocation_id == 7
            return {**result, "published": True}

    result = {"summary": "safe"}
    assert await coordinate_publication(FakeCoordinator(), kind="issue_analysis", result=result, context={}) == result
    context = {"invocation_context": SimpleNamespace(invocation_id=7)}
    assert await coordinate_publication(FakeCoordinator(), kind="issue_analysis", result=result, context=context) == {"summary": "safe", "published": True}


def test_external_key_whitelist():
    assert validate_external_key("pr_review:owner/repo:12:sha") == "pr_review:owner/repo:12:sha"
    with pytest.raises(ValueError):
        validate_external_key("contains space")
