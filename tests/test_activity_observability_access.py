"""Task 7 access, cursor, projection, and SSE boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.requests import Request

from backend.models.activity_observability_models import (
    ActivityContextOperation,
    ActivityContextSnapshot,
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityEvent,
    ActivityObservabilityMessage,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
    ActivityThread,
    ActivityToolExecution,
)
from backend.models.database import Base, IssueAnalysis, PRReview
from backend.services.activity_observability.access_service import (
    ActivityAccessService,
    ActivityNotFoundError,
    CursorConfig,
    CursorResetRequiredError,
    project_attempt,
    project_context_snapshot,
    project_event,
)
from backend.services.activity_observability.conversation_service import (
    CONVERSATION_PROJECTION_VERSION,
    ConversationProjectionService,
)
from backend.services.activity_observability.tool_service import AuthorizedArtifactView
from backend.webui.routes import activity_observability as activity_routes
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
        if name.startswith("activity_observability_")
        or name in {"pr_reviews", "issue_analyses", "telegram_users"}
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
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
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
        return (
            self.allowed
            and user.get("repo") == session.resource_identity.repo_full_name
        )

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
        now=now or (lambda: datetime(2026, 7, 20, tzinfo=UTC)),
    )


def _conversation_chain(db):
    observed_session = _session(db, number=77)
    thread = ActivityThread(
        session_id=observed_session.id,
        thread_purpose="reviewer",
        last_seq=2,
    )
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="provider",
        requested_model="requested-model",
        requested_thinking_mode="adaptive",
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="anthropic-native",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    db.session.add_all([thread, snapshot])
    db.session.flush()
    invocation = ActivityInvocation(
        session_id=observed_session.id,
        status="running",
        current_phase="model_request",
        task_type="review",
        created_at=datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
    )
    db.session.add(invocation)
    db.session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=observed_session.id,
        thread_id=thread.id,
        role_binding_snapshot_id=snapshot.id,
        purpose="reviewer",
        requirement="required",
        is_primary=True,
        status="running",
        current_phase="model_request",
    )
    db.session.add(work_unit)
    db.session.flush()
    attempt = ActivityModelAttempt(
        work_unit_id=work_unit.id,
        attempt_index=1,
        logical_call_id="logical-77",
        attempt_kind="fallback",
        purpose="review",
        status="running",
        requested_provider="provider",
        requested_model="requested-model",
        requested_thinking_mode="adaptive",
        requested_effort="high",
        effective_provider="fallback-provider",
        effective_model="fallback-model",
        effective_thinking_mode="unsupported",
        effective_effort="medium",
        protocol_family="openai-compatible",
        max_output_tokens=4096,
        endpoint_fingerprint="b" * 64,
        contextless_reason="transcript_not_applicable",
        reasoning_availability="omitted",
        started_at=datetime(2026, 7, 20, 1, 0, 1, tzinfo=UTC),
    )
    db.session.add(attempt)
    db.session.flush()
    artifact = ActivityNativeArtifact(
        attempt_id=attempt.id,
        artifact_kind="reasoning_content",
        availability="provider_exposed",
        provider_family="fallback-provider",
        protocol_family="openai-compatible",
        model_family="fallback-model",
        compatibility_key="provider|protocol|model|scope",
        capture_mode="artifact",
        visibility="admin_only",
        payload_ciphertext="encrypted-secret",
        payload_nonce="nonce",
        encryption_key_id="key",
    )
    db.session.add(artifact)
    db.session.flush()
    user_message = ActivityObservabilityMessage(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        seq=1,
        role="user",
        content="legacy-user-secret",
        message_json='{"role":"user","content":"legacy-user-secret"}',
        artifact_id=artifact.id,
        created_at=datetime(2026, 7, 20, 1, 0, 2, tzinfo=UTC),
    )
    assistant_message = ActivityObservabilityMessage(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        origin_attempt_id=attempt.id,
        seq=2,
        role="assistant",
        content="visible assistant answer",
        message_json='{"role":"assistant","content":"visible assistant answer"}',
        created_at=datetime(2026, 7, 20, 1, 0, 3, tzinfo=UTC),
    )
    tool = ActivityToolExecution(
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        origin_attempt_id=attempt.id,
        tool_call_id="tool-77",
        name="read_file",
        arguments_json='{"token":"legacy-tool-secret"}',
        arguments_sensitivity="internal",
        status="running",
        created_at=datetime(2026, 7, 20, 1, 0, 4, tzinfo=UTC),
    )
    operation = ActivityContextOperation(
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        operation_type="canonical_summary",
        trigger_reason="threshold",
        status="completed",
        created_at=datetime(2026, 7, 20, 1, 0, 5, tzinfo=UTC),
    )
    db.session.add_all([user_message, assistant_message, tool, operation])
    db.session.commit()
    return observed_session, thread, invocation, work_unit, attempt, artifact


@pytest.mark.asyncio
async def test_require_session_access_denied_and_missing_are_same_not_found(db):
    session = _session(db)
    denied = _service(db, ScopeAuthorizer(allowed=True))
    user = {"user_id": "user-a", "repo": "owner/other"}
    with pytest.raises(ActivityNotFoundError) as denied_error:
        await denied.require_session_access(session.id, user)
    with pytest.raises(ActivityNotFoundError) as missing_error:
        await denied.require_session_access(99999, user)
    assert (
        str(denied_error.value)
        == str(missing_error.value)
        == "activity session not found"
    )


@pytest.mark.asyncio
async def test_admin_still_requires_repository_scope_but_super_admin_needs_explicit_global_policy(
    db,
):
    session = _session(db)
    admin = _service(db, ScopeAuthorizer(allowed=True))
    with pytest.raises(ActivityNotFoundError):
        await admin.require_session_access(
            session.id, {"user_id": "admin", "role": "admin", "repo": "owner/other"}
        )

    super_admin = ActivityAccessService(
        db,
        authorizer=None,
        super_admin_global_policy=lambda user: user.get("global") is True,
    )
    with pytest.raises(ActivityNotFoundError):
        await super_admin.require_session_access(
            session.id, {"user_id": "root", "role": "super_admin"}
        )
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
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    projected = project_event(event, {"role": "admin"})
    assert projected["payload"] == {"status": "failed", "safe_summary": "only safe"}
    assert all(
        secret not in json.dumps(projected).lower()
        for secret in ("raw prompt", "credential", "provider.invalid", "cipher")
    )

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
    assert "tool_args" not in serialized
    assert "tool_result" not in serialized
    assert "cipher" not in serialized
    assert "endpoint" not in serialized


def test_context_projection_prefers_provider_usage_over_heuristic_snapshot():
    context = ActivityContextSnapshot(
        attempt_id=10,
        context_revision_id=20,
        snapshot_kind="before_request",
        context_tokens=9783,
        context_tokens_availability="estimated",
        context_tokens_source="heuristic",
        context_window_tokens=1_000_000,
        context_window_tokens_availability="reported",
        context_window_tokens_source="model_catalog",
    )
    attempt = ActivityModelAttempt(
        input_tokens=18432,
        input_tokens_availability="reported",
        input_tokens_source="provider",
        cached_input_tokens=18304,
        cached_input_tokens_availability="reported",
        cached_input_tokens_source="provider",
        reasoning_tokens=32,
        reasoning_tokens_availability="reported",
        reasoning_tokens_source="provider",
    )

    projected = project_context_snapshot(context, attempt)

    assert projected["context_tokens"] == 18432
    assert projected["context_tokens_availability"] == "reported"
    assert projected["context_tokens_source"] == "provider"
    assert projected["context_window_tokens"] == 1_000_000
    assert projected["cache_read_tokens"] == 18304
    assert projected["reasoning_context_tokens"] == 32


def test_cursor_config_fails_closed_for_missing_or_short_secret_and_roundtrips():
    with pytest.raises(ValueError):
        CursorConfig(secret="", ttl_seconds=60, page_size=2)
    service = ActivityAccessService(
        cursor_config=CursorConfig(secret="x" * 32, ttl_seconds=60, page_size=2),
        now=lambda: issued,
    )
    issued = datetime(2026, 7, 20, tzinfo=UTC)
    cursor = service.create_cursor(
        session_id=7,
        last_scanned_sequence=3,
        authorization_version="auth-v1",
        issued_at=issued,
    )
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
    issued = datetime(2026, 7, 20, tzinfo=UTC)
    service = _service(None, now=lambda: issued + timedelta(seconds=61))
    cursor = service.create_cursor(
        session_id=1,
        last_scanned_sequence=1,
        authorization_version="v1",
        issued_at=issued,
    )
    with pytest.raises(CursorResetRequiredError):
        service.decode_cursor(cursor, session_id=1, authorization_version="v1")

    other = ActivityAccessService(
        cursor_config=CursorConfig(
            secret="s" * 32, ttl_seconds=60, page_size=2, projection_version=2
        ),
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
    assert snapshot["usage_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cached_input_tokens": None,
    }


@pytest.mark.asyncio
async def test_legacy_scope_uses_repository_pr_number_not_global_github_id(db):
    from backend.services.activity_observability.legacy_scope_authorizer import (
        LegacyRepositoryScopeAuthorizer,
    )

    pr = PRReview(
        pr_id=987654,
        pr_number=123,
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
    pr_session = ActivityObservabilitySession(
        resource_identity_id=pr_identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    issue_session = ActivityObservabilitySession(
        resource_identity_id=issue_identity.id,
        session_kind="long_lived",
        status="open",
        session_event_sequence=0,
    )
    db.session.add_all([pr_session, issue_session])
    db.session.flush()
    authorizer = LegacyRepositoryScopeAuthorizer()
    user = {"user_id": "alice", "sub": "alice", "role": "user"}
    assert await authorizer.authorize_session(db, session=pr_session, user=user) is True
    pr.pr_number = 124
    db.session.flush()
    assert (
        await authorizer.authorize_session(db, session=pr_session, user=user) is False
    )
    assert (
        await authorizer.authorize_session(db, session=issue_session, user=user) is True
    )
    assert (
        await authorizer.authorize_session(
            db,
            session=issue_session,
            user={"sub": "bob", "user_id": "bob", "role": "user"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_legacy_dispatch_authorizer_resolves_recipient_github_identity(db):
    from backend.services.activity_observability.legacy_scope_authorizer import (
        LegacyRepositoryScopeAuthorizer,
    )

    authorizer = LegacyRepositoryScopeAuthorizer()
    assert (
        await authorizer.is_authorized(db, user_id="alice", session_id=_session(db).id)
        is False
    )


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

    template = Path("backend/webui/templates/activity_observability.html").read_text(
        encoding="utf-8"
    )
    # Deterministic omitted hint is rendered via the i18n key.
    assert "reasoning_omitted_hint" in template
    # No permanent spinner component, no fabricated reasoning text.
    assert "thinking-spinner" not in template
    assert "推理内容" not in template
    assert "reasoning_delta" not in template  # provider text never echoed server-side
    # Notifications must only drive a REST re-fetch, not mutate UI from SSE payload.
    assert "loadConversationUpdates" in template
    assert "activity:notification" in template
    assert 'x-data="{snapshot' not in template
    assert ".__x" not in template


def test_activity_page_uses_independent_scroll_regions_and_sse_fallback_polling():
    from pathlib import Path

    template = Path("backend/webui/templates/activity_observability.html").read_text(
        encoding="utf-8"
    )
    notification_handler = template.split(
        "addEventListener('activity:notification'", 1
    )[1].split("this.eventSource.onerror", 1)[0]

    assert "min-h-0 min-w-0 grid-cols-1 overflow-hidden" in template
    assert 'x-ref="timeline"' in template
    assert "min-h-0 flex-1 overflow-y-auto overscroll-contain" in template
    assert "lg:h-full" in template
    assert "&& !this.streamConnected" in template
    assert "this.stopFallbackPolling();" in template
    assert "this.scheduleRefresh(sessionId);" in notification_handler
    assert "this.loadSessions();" not in notification_handler
    assert "}, 15000);" in template
    assert "{% block global_sse_client %}{% endblock %}" in template
    assert 'x-data="{expanded: false, expandable: false}"' in template
    assert "$refs.content.scrollHeight > $refs.content.clientHeight" in template
    assert '@click.stop="expanded = !expanded"' in template
    assert "expandedEntries" not in template
    assert "fa-wave-square" not in template
    assert "fa-comments" not in template
    # Diagnostics must follow the selected Attempt or the display Invocation's
    # primary Work Unit, rather than a newer auxiliary summary Attempt.
    assert "get contextAttempt()" in template
    assert "invocation.primary_work_unit_id" in template
    assert "this.contextAttempt?.context" in template
    assert "current_request_context" in template
    assert "session_usage_total" in template


@pytest.mark.asyncio
async def test_conversation_projection_is_ordered_versioned_and_server_redacted(db):
    observed_session, _, _, _, _, artifact = _conversation_chain(db)
    access = _service(db, ScopeAuthorizer())
    projection = ConversationProjectionService(db, access_service=access)
    ordinary = {
        "user_id": "user-a",
        "repo": "owner/repo-77",
        "role": "user",
    }

    page = await projection.get_conversation(
        observed_session.id,
        ordinary,
        limit=50,
    )

    assert page["projection_version"] == CONVERSATION_PROJECTION_VERSION
    assert page["session_id"] == observed_session.id
    assert [entry["order_key"] for entry in page["entries"]] == sorted(
        (entry["order_key"] for entry in page["entries"]),
        key=lambda value: tuple(map(int, value.split(":"))),
    )
    serialized = json.dumps(page, ensure_ascii=False)
    assert "legacy-user-secret" not in serialized
    assert "legacy-tool-secret" not in serialized
    assert "encrypted-secret" not in serialized
    assistant = next(
        entry
        for entry in page["entries"]
        if entry["type"] == "message" and entry["role"] == "assistant"
    )
    assert assistant["content"] == "visible assistant answer"
    restricted = next(
        entry
        for entry in page["entries"]
        if entry["type"] == "message" and entry["role"] == "user"
    )
    assert restricted["content"] is None
    assert restricted["content_visibility"] == "restricted"
    assert restricted["can_request_sensitive"] is False
    assert restricted["artifact_id"] == artifact.id

    admin_page = await projection.get_conversation(
        observed_session.id,
        {**ordinary, "role": "admin"},
        limit=50,
    )
    assert "legacy-user-secret" not in json.dumps(admin_page)
    admin_message = next(
        entry
        for entry in admin_page["entries"]
        if entry["type"] == "message" and entry["role"] == "user"
    )
    assert admin_message["can_request_sensitive"] is False

    super_page = await projection.get_conversation(
        observed_session.id,
        {**ordinary, "role": "super_admin", "trace": True},
        limit=50,
    )
    super_message = next(
        entry
        for entry in super_page["entries"]
        if entry["type"] == "message" and entry["role"] == "user"
    )
    assert super_message["content"] is None
    assert super_message["can_request_sensitive"] is True
    assert super_message["artifacts"][0]["artifact_id"] == artifact.id
    assert "encrypted-secret" not in json.dumps(super_page)


@pytest.mark.asyncio
async def test_conversation_projects_tool_round_once_without_blank_protocol_messages(
    db,
):
    observed_session, thread, _, work_unit, attempt, _ = _conversation_chain(db)
    db.session.add_all(
        [
            ActivityObservabilityMessage(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                origin_attempt_id=attempt.id,
                seq=3,
                role="assistant",
                content="",
                message_json=(
                    '{"role":"assistant","content":"",'
                    '"tool_calls":[{"id":"tool-77","type":"function",'
                    '"function":{"name":"read_file"}}]}'
                ),
                created_at=datetime(2026, 7, 20, 1, 0, 3, 500000, tzinfo=UTC),
            ),
            ActivityObservabilityMessage(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                origin_attempt_id=attempt.id,
                seq=4,
                role="tool",
                content=None,
                message_json='{"role":"tool","tool_call_id":"tool-77"}',
                tool_call_id="tool-77",
                created_at=datetime(2026, 7, 20, 1, 0, 5, tzinfo=UTC),
            ),
        ]
    )
    db.session.commit()
    projection = ConversationProjectionService(
        db,
        access_service=_service(db, ScopeAuthorizer()),
    )

    page = await projection.get_conversation(
        observed_session.id,
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        limit=50,
    )

    assert not any(
        entry["type"] == "message" and entry.get("seq") in {3, 4}
        for entry in page["entries"]
    )
    assert [
        entry["tool_call_id"]
        for entry in page["entries"]
        if entry["type"] == "tool_call"
    ] == ["tool-77"]


@pytest.mark.asyncio
async def test_conversation_projects_allowed_message_kind(db):
    """投影在公开时间线暴露 allowlist 内的 message_kind。

    使前端可渲染"标签推荐请求/响应"等可区分标题，而不依赖受限正文或对
    summary 等技术角色的推断。The projection surfaces allowlisted business
    kinds so the live monitor can render distinguishable titles without
    restricted content or role guesses.
    """
    observed_session, thread, _, work_unit, _, _ = _conversation_chain(db)
    db.session.add_all(
        [
            ActivityObservabilityMessage(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                seq=11,
                role="assistant",
                content='{"labels":[]}',
                message_json=(
                    '{"role":"assistant","content":"{\\"labels\\":[]}",'
                    '"message_kind":"label_recommendation_response"}'
                ),
                created_at=datetime(2026, 7, 20, 2, 0, 0, tzinfo=UTC),
            ),
            ActivityObservabilityMessage(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                seq=12,
                role="user",
                content=None,
                message_json=(
                    '{"role":"user","message_kind":"label_recommendation_request"}'
                ),
                created_at=datetime(2026, 7, 20, 2, 0, 1, tzinfo=UTC),
            ),
        ]
    )
    db.session.commit()
    projection = ConversationProjectionService(
        db, access_service=_service(db, ScopeAuthorizer())
    )
    page = await projection.get_conversation(
        observed_session.id,
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        limit=50,
    )
    by_seq = {
        entry["seq"]: entry for entry in page["entries"] if entry["type"] == "message"
    }
    assert by_seq[11]["message_kind"] == "label_recommendation_response"
    assert by_seq[12]["message_kind"] == "label_recommendation_request"


@pytest.mark.asyncio
async def test_conversation_strips_unapproved_message_kind(db):
    """投影丢弃 allowlist 外的 message_kind。

    防止任意元数据借道公开时间线泄漏（例如绕过写入侧直接写入 DB 的旧数据
    或伪造行）。Unapproved kinds never reach the public timeline, defending
    against rows that bypassed the writer-side allowlist.
    """
    observed_session, thread, _, work_unit, _, _ = _conversation_chain(db)
    db.session.add_all(
        [
            ActivityObservabilityMessage(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                seq=13,
                role="assistant",
                content="hi",
                message_json=(
                    '{"role":"assistant","content":"hi","message_kind":"forged_kind"}'
                ),
                created_at=datetime(2026, 7, 20, 2, 0, 2, tzinfo=UTC),
            ),
        ]
    )
    db.session.commit()
    projection = ConversationProjectionService(
        db, access_service=_service(db, ScopeAuthorizer())
    )
    page = await projection.get_conversation(
        observed_session.id,
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        limit=50,
    )
    by_seq = {
        entry["seq"]: entry for entry in page["entries"] if entry["type"] == "message"
    }
    assert by_seq[13].get("message_kind") is None


@pytest.mark.asyncio
async def test_conversation_cursor_paginates_and_event_updates_are_idempotent(db):
    observed_session, _, invocation, work_unit, _, _ = _conversation_chain(db)
    access = _service(db, ScopeAuthorizer())
    projection = ConversationProjectionService(db, access_service=access)
    user = {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"}

    first = await projection.get_conversation(observed_session.id, user, limit=3)
    assert len(first["entries"]) == 3
    assert first["has_more"] is True
    assert first["before_cursor"]
    earlier = await projection.get_conversation(
        observed_session.id,
        user,
        cursor=first["before_cursor"],
        limit=3,
    )
    assert {entry["id"] for entry in first["entries"]}.isdisjoint(
        entry["id"] for entry in earlier["entries"]
    )
    with pytest.raises(CursorResetRequiredError):
        await projection.get_conversation(
            observed_session.id,
            user,
            cursor=f"{first['before_cursor']}tampered",
        )

    _event(
        db,
        observed_session.id,
        1,
        payload={"status": "running"},
        event_id="conversation-event-1",
    )
    event = (
        db.session.query(ActivityObservabilityEvent)
        .filter_by(event_uuid="conversation-event-1")
        .one()
    )
    event.invocation_id = invocation.id
    event.work_unit_id = work_unit.id
    observed_session.session_event_sequence = 1
    db.session.commit()

    updates = await projection.get_updates(
        observed_session.id,
        user,
        cursor=first["events_cursor"],
    )
    assert updates["entries"]
    assert len({entry["id"] for entry in updates["entries"]}) == len(updates["entries"])
    empty = await projection.get_updates(
        observed_session.id,
        user,
        cursor=updates["cursor"],
    )
    assert empty["entries"] == []


@pytest.mark.asyncio
async def test_session_list_projects_current_phase_model_thinking_and_fallback(db):
    observed_session, _, invocation, work_unit, attempt, _ = _conversation_chain(db)
    access = _service(db, ScopeAuthorizer())

    result = await access.list_sessions(
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        db=db,
    )

    item = next(
        session
        for session in result["sessions"]
        if session["session_id"] == observed_session.id
    )
    assert item["current_phase"] == "model_request"
    assert item["active_provider"] == "fallback-provider"
    assert item["active_model"] == "fallback-model"
    assert item["thinking_mode"] == "unsupported"
    assert item["attempt_kind"] == "fallback"
    assert item["attempt_status"] == "running"
    assert item["status"] == "running"
    assert item["session_status"] == "open"

    invocation.status = "completed"
    invocation.current_phase = None
    work_unit.status = "completed"
    work_unit.current_phase = None
    attempt.status = "completed"
    db.session.commit()

    completed = await access.list_sessions(
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        db=db,
    )
    completed_item = next(
        session
        for session in completed["sessions"]
        if session["session_id"] == observed_session.id
    )
    assert completed_item["status"] == "completed"
    assert completed_item["session_status"] == "open"
    snapshot = await access.create_snapshot(
        observed_session.id,
        {"user_id": "user-a", "repo": "owner/repo-77", "role": "user"},
        db=db,
    )
    assert snapshot["session"]["status"] == "completed"
    assert snapshot["session"]["session_status"] == "open"


def test_configures_cursor_and_outbox_from_settings():
    from backend.core.config import Settings

    settings = Settings(
        activity_cursor_signing_secret="x" * 32,
        activity_outbox_claim_timeout_seconds=12.5,
    )
    assert settings.activity_outbox_claim_timeout_seconds == 12.5


@pytest.mark.asyncio
async def test_artifact_route_returns_no_store_decrypted_view(monkeypatch):
    calls = {}

    class Access:
        async def require_session_access(self, session_id, user, db):
            calls["access"] = (session_id, user["user_id"], db)
            return object()

    class FakeToolService:
        def __init__(self, **kwargs):
            calls["service"] = kwargs

        async def read_artifact_with_audit(self, artifact_id, **kwargs):
            calls["read"] = (artifact_id, kwargs)
            return AuthorizedArtifactView(
                artifact_id=artifact_id,
                artifact_kind="request_projection",
                availability="available",
                provider_family="openai",
                protocol_family="responses",
                compatibility_key="compatibility",
                capture_mode="encrypted",
                visibility="admin_only",
                payload_safe_summary=None,
                payload='{"messages":[{"role":"user","content":"secret prompt"}]}',
                payload_unavailable_reason=None,
                replay_allowed=False,
                retention_expires_at=datetime(2026, 8, 1, tzinfo=UTC),
            )

    app = Starlette()
    app.state.activity_access_service = Access()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/activity/observability/api/sessions/7/artifacts/11",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "client": ("test", 123),
            "server": ("test", 80),
            "app": app,
        }
    )
    db = object()
    monkeypatch.setattr(activity_routes, "ToolService", FakeToolService)

    response = await activity_routes.activity_artifact(
        7,
        11,
        request,
        user={"user_id": "super-a", "role": "super_admin"},
        db=db,
    )
    body = json.loads(response.body)

    assert response.headers["cache-control"] == (
        "no-store, no-cache, must-revalidate, private"
    )
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert body["payload"].endswith('"secret prompt"}]}')
    assert calls["access"] == (7, "super-a", db)
    assert calls["read"] == (
        11,
        {"reader": "super-a", "require_trace": True},
    )
