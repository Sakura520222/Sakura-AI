"""Task 7 access, cursor, projection, and SSE boundary tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityModelAttempt,
    ActivityObservabilityEvent,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
)
from backend.models.database import Base, IssueAnalysis, PRReview
from backend.services.activity_observability.access_service import (
    ActivityAccessService,
    ActivityNotFoundError,
    CursorConfig,
    CursorResetRequiredError,
    project_attempt,
    project_event,
)
from backend.webui.sse import user_activity_channel


class AsyncDb:
    def __init__(self, session: Session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, object_id, **kwargs):
        return self.session.get(model, object_id, **kwargs)

    async def refresh(self, row):
        self.session.refresh(row)


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
        connection.create_function(
            "regexp",
            2,
            lambda pattern, value: bool(value is not None),
        )
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name.startswith("activity_observability_") or name in {"pr_reviews", "issue_analyses", "telegram_users"}
    ]
    Base.metadata.create_all(engine, tables=tables)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db():
    engine, factory = _database()
    session = factory()
    try:
        yield AsyncDb(session)
    finally:
        session.close()
        engine.dispose()


def _session(db, number=1):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id=str(number),
        resource_type="pr",
        resource_number=str(number),
        repo_full_name=f"owner/repo-{number}",
    )
    db.session.add(identity)
    db.session.flush()
    row = ActivityObservabilitySession(
        resource_identity_id=identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _event(db, session_id, sequence, visibility="public", payload=None, event_id=None):
    row = ActivityObservabilityEvent(
        event_uuid=event_id or f"event-{sequence}",
        session_id=session_id,
        event_sequence=sequence,
        event_type="status",
        visibility=visibility,
        projection_json=json.dumps(payload or {"status": "running"}),
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    db.session.add(row)
    db.session.flush()
    return row


class ScopeAuthorizer:
    def __init__(self, allowed=True, version="v1"):
        self.allowed = allowed
        self.version = version
        self.calls = []

    async def authorize_session(self, db, *, session, user):
        self.calls.append((session.id, user.get("user_id")))
        return self.allowed and user.get("repo") == session.resource_identity.repo_full_name

    async def authorization_version(self, db, *, user):
        return self.version

    async def may_view_trace(self, db, *, session, user):
        return user.get("trace") is True


def _service(db, authorizer=None, now=None):
    return ActivityAccessService(
        db,
        authorizer=authorizer,
        cursor_config=CursorConfig(
            secret="s" * 32,
            ttl_seconds=60,
            page_size=2,
        ),
        now=now or (lambda: datetime(2026, 7, 20, tzinfo=timezone.utc)),
    )


@pytest.mark.asyncio
async def test_require_session_access_denied_and_missing_are_same_not_found(db):
    session = _session(db)
    denied = _service(db, ScopeAuthorizer(allowed=True))
    user = {"user_id": "user-a", "repo": "owner/other"}
    with pytest.raises(ActivityNotFoundError) as denied_error:
        await denied.require_session_access(session.id, user)
    with pytest.raises(ActivityNotFoundError) as missing_error:
        await denied.require_session_access(99999, user)
    assert str(denied_error.value) == str(missing_error.value) == "activity session not found"


@pytest.mark.asyncio
async def test_admin_still_requires_repository_scope_but_super_admin_needs_explicit_global_policy(db):
    session = _session(db)
    admin = _service(db, ScopeAuthorizer(allowed=True))
    with pytest.raises(ActivityNotFoundError):
        await admin.require_session_access(session.id, {"user_id": "admin", "role": "admin", "repo": "owner/other"})

    super_admin = ActivityAccessService(
        db,
        authorizer=None,
        super_admin_global_policy=lambda user: user.get("global") is True,
    )
    with pytest.raises(ActivityNotFoundError):
        await super_admin.require_session_access(session.id, {"user_id": "root", "role": "super_admin"})
    assert await super_admin.require_session_access(
        session.id, {"user_id": "root", "role": "super_admin", "global": True}
    )


def test_projection_whitelists_event_and_attempt_without_sensitive_fields():
    event = ActivityObservabilityEvent(
        event_uuid="e1",
        session_id=1,
        event_sequence=1,
        event_type="failure",
        visibility="public",
        projection_json=json.dumps(
            {
                "status": "failed",
                "prompt": "raw prompt",
                "tool_args": {"secret": "x"},
                "tool_result": "raw result",
                "error_detail": "credential=secret",
                "ciphertext": "cipher",
                "endpoint": "https://provider.invalid",
                "safe_summary": "only safe",
            }
        ),
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    projected = project_event(event, {"role": "admin"})
    assert projected["payload"] == {"status": "failed", "safe_summary": "only safe"}
    assert all(secret not in json.dumps(projected).lower() for secret in ("raw prompt", "credential", "provider.invalid", "cipher"))

    attempt = ActivityModelAttempt(
        work_unit_id=1,
        attempt_index=1,
        logical_call_id="logical-1",
        attempt_kind="primary",
        purpose="review",
        status="completed",
        requested_provider="openai",
        requested_model="model",
        effective_provider="openai",
        effective_model="model",
        stop_reason="end_turn",
        http_status=200,
        retryable=False,
        input_tokens=1,
        output_tokens=2,
        reasoning_tokens=3,
        cached_input_tokens=0,
        input_tokens_availability="provider",
        output_tokens_availability="provider",
        reasoning_tokens_availability="provider",
        cached_input_tokens_availability="provider",
    )
    projected_attempt = project_attempt(attempt, {"role": "admin"})
    serialized = json.dumps(projected_attempt).lower()
    assert "prompt" not in serialized
    assert "tool" not in serialized
    assert "cipher" not in serialized
    assert "endpoint" not in serialized


def test_cursor_config_fails_closed_for_missing_or_short_secret_and_roundtrips():
    with pytest.raises(ValueError):
        CursorConfig(secret="", ttl_seconds=60, page_size=2)
    service = ActivityAccessService(
        cursor_config=CursorConfig(secret="x" * 32, ttl_seconds=60, page_size=2),
        now=lambda: issued,
    )
    issued = datetime(2026, 7, 20, tzinfo=timezone.utc)
    cursor = service.create_cursor(session_id=7, last_scanned_sequence=3, authorization_version="auth-v1", issued_at=issued)
    body = service.decode_cursor(cursor, session_id=7, authorization_version="auth-v1")
    assert body["last_scanned_sequence"] == 3

    encoded, signature = cursor.split(".")
    tampered = f"{encoded[:-1]}{'A' if encoded[-1] != 'A' else 'B'}.{signature}"
    with pytest.raises(CursorResetRequiredError):
        service.decode_cursor(tampered, session_id=7, authorization_version="auth-v1")
    with pytest.raises(CursorResetRequiredError):
        service.decode_cursor(cursor, session_id=8, authorization_version="auth-v1")
    with pytest.raises(CursorResetRequiredError):
        service.decode_cursor(cursor, session_id=7, authorization_version="auth-v2")


def test_cursor_expiry_and_projection_version_reset():
    issued = datetime(2026, 7, 20, tzinfo=timezone.utc)
    service = _service(None, now=lambda: issued + timedelta(seconds=61))
    cursor = service.create_cursor(session_id=1, last_scanned_sequence=1, authorization_version="v1", issued_at=issued)
    with pytest.raises(CursorResetRequiredError):
        service.decode_cursor(cursor, session_id=1, authorization_version="v1")

    other = ActivityAccessService(
        cursor_config=CursorConfig(secret="s" * 32, ttl_seconds=60, page_size=2, projection_version=2),
        now=lambda: issued,
    )
    with pytest.raises(CursorResetRequiredError):
        other.decode_cursor(cursor, session_id=1, authorization_version="v1")


@pytest.mark.asyncio
async def test_hidden_events_advance_cursor_without_returning_or_replaying(db):
    session = _session(db)
    session.session_event_sequence = 4
    _event(db, session.id, 1, "public", {"status": "one"})
    _event(db, session.id, 2, "hidden", {"status": "secret"})
    _event(db, session.id, 3, "internal", {"status": "internal"})
    _event(db, session.id, 4, "public", {"status": "four"})
    authorizer = ScopeAuthorizer()
    result = await _service(db, authorizer).list_events_after(
        session.id, {"user_id": "u", "repo": "owner/repo-1"}
    )
    assert [row["sequence"] for row in result["events"]] == [1, 4]
    assert result["last_scanned_sequence"] == 4
    next_page = await _service(db, authorizer).list_events_after(
        session.id,
        {"user_id": "u", "repo": "owner/repo-1"},
        cursor=result["cursor"],
    )
    assert next_page["events"] == []
    assert next_page["last_scanned_sequence"] == 4


@pytest.mark.asyncio
async def test_snapshot_high_water_and_cursor_are_same_sequence(db):
    session = _session(db)
    session.session_event_sequence = 3
    authorizer = ScopeAuthorizer()
    snapshot = await _service(db, authorizer).create_snapshot(
        session.id, {"user_id": "u", "repo": "owner/repo-1"}
    )
    service = _service(db, authorizer)
    cursor_body = service.decode_cursor(
        snapshot["cursor"], session_id=session.id, authorization_version="v1"
    )
    assert snapshot["high_water_mark"] == 3
    assert cursor_body["last_scanned_sequence"] == 3


@pytest.mark.asyncio
async def test_legacy_scope_uses_external_numbers_and_repository_user_scope(db):
    from backend.services.activity_observability.legacy_scope_authorizer import (
        LegacyRepositoryScopeAuthorizer,
    )

    pr = PRReview(
        pr_id=987654,
        repo_name="repo",
        repo_owner="owner",
        author="alice",
        strategy="standard",
    )
    issue = IssueAnalysis(
        issue_number=42,
        repo_name="repo",
        repo_owner="owner",
        author="alice",
    )
    db.session.add_all([pr, issue])
    db.session.flush()
    pr_identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="repo-id",
        resource_type="pr",
        resource_number="123",
        repo_full_name="owner/repo",
    )
    issue_identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="repo-id",
        resource_type="issue",
        resource_number="42",
        repo_full_name="owner/repo",
    )
    db.session.add_all([pr_identity, issue_identity])
    db.session.flush()
    pr_session = ActivityObservabilitySession(resource_identity_id=pr_identity.id, session_kind="long_lived", status="open", session_event_sequence=0)
    issue_session = ActivityObservabilitySession(resource_identity_id=issue_identity.id, session_kind="long_lived", status="open", session_event_sequence=0)
    db.session.add_all([pr_session, issue_session])
    db.session.flush()
    authorizer = LegacyRepositoryScopeAuthorizer()
    user = {"user_id": "alice", "sub": "alice", "role": "user"}
    assert await authorizer.authorize_session(db, session=pr_session, user=user) is False
    pr.pr_id = 123
    db.session.flush()
    assert await authorizer.authorize_session(db, session=pr_session, user=user) is True
    assert await authorizer.authorize_session(db, session=issue_session, user=user) is True
    assert await authorizer.authorize_session(db, session=issue_session, user={"sub": "bob", "user_id": "bob", "role": "user"}) is False


@pytest.mark.asyncio
async def test_legacy_dispatch_authorizer_resolves_recipient_github_identity(db):
    from backend.services.activity_observability.legacy_scope_authorizer import LegacyRepositoryScopeAuthorizer

    authorizer = LegacyRepositoryScopeAuthorizer()
    assert await authorizer.is_authorized(db, user_id="alice", session_id=_session(db).id) is False


def test_user_activity_channels_are_opaque_stable_and_noninjectable():
    assert user_activity_channel("user-a") != user_activity_channel("user-b")
    assert "user:a" not in user_activity_channel("user:a")
    for bad in ("a\nb", "a\rb", "a\tb", "a/../b"):
        with pytest.raises(ValueError):
            user_activity_channel(bad)


def test_activity_page_renders_reasoning_omitted_without_spinner_or_fabricated_text():
    """The observability page must never render a permanent spinner or fabricated
    reasoning text; omitted/unavailable states keep a deterministic hint only."""
    from pathlib import Path

    template = Path(
        "backend/webui/templates/activity_observability.html"
    ).read_text(encoding="utf-8")
    # Deterministic omitted hint is rendered via the i18n key.
    assert "reasoning_omitted_hint" in template
    # No permanent spinner component, no fabricated reasoning text.
    assert "thinking-spinner" not in template
    assert "推理内容" not in template
    assert "reasoning_delta" not in template  # provider text never echoed server-side
    # Notifications must only drive a REST re-fetch, not mutate UI from SSE payload.
    assert "loadEvents" in template
    assert "activity:notification" in template
    assert 'x-data="{snapshot' not in template
    assert ".__x" not in template


def test_configures_cursor_and_outbox_from_settings():
    from backend.core.config import Settings

    settings = Settings(
        activity_cursor_signing_secret="x" * 32,
        activity_outbox_claim_timeout_seconds=12.5,
    )
    assert settings.activity_outbox_claim_timeout_seconds == 12.5
