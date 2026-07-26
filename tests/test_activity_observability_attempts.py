"""Task 4 tests for authoritative attempts, tools, and native artifacts."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.models.activity_observability_models import (
    ActivityArtifactAccessLog,
    ActivityCanonicalContextRevision,
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
    ActivityThread,
    ActivityToolExecution,
)
from backend.core.ai_protocol.models import (
    AuthScheme,
    ModelCapabilitySet,
    ModelMetadata,
    MetadataSource,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
    StopReason,
    UnifiedMessage,
    UnifiedStreamEvent,
    UnifiedUsage,
    UnifiedRequest,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.models.database import Base
from backend.services.activity_observability.reasoning import (
    CAPTURE_ARTIFACT,
    CAPTURE_METADATA_ONLY,
    CAPTURE_SAFE_SUMMARY,
    REASONING_ENCRYPTED_OPAQUE,
    REASONING_PROVIDER_EXPOSED,
    REASONING_SUMMARIZED,
    ReasoningCapturePolicy,
    safe_summary_or_none,
)
from backend.services.activity_observability.contracts import (
    InvocationContext,
    RoleConfigSnapshot,
)
from backend.services.activity_observability.attempt_service import AttemptService
from backend.services.activity_observability.context_service import ContextService
from backend.services.activity_observability.observer import (
    ObservedEmbeddingSender,
    ObservedModelSender,
)
from backend.services.ai_reviewer.unified_client import UnifiedAIClient
from backend.services.activity_observability.tool_service import (
    ArtifactAuthorization,
    ConflictError,
    SENSITIVITY_SECRET,
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_RUNNING,
    ToolService,
)


class _Begin:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, _value, _traceback):
        if exc_type:
            self.session.rollback()
        else:
            self.session.commit()
        return False


class _AsyncAdapter:
    def __init__(self, session):
        self.session = session

    def begin(self):
        return _Begin(self.session)

    async def get(self, model, object_id, **kwargs):
        return self.session.get(model, object_id, **kwargs)

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _configure(connection, _record):
        connection.create_collation("ascii_bin", lambda left, right: (left > right) - (left < right))
        connection.create_function("regexp", 2, lambda *args: bool(args))
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [table for table in Base.metadata.tables.values() if table.name.startswith("activity_observability_")]
    Base.metadata.create_all(engine, tables=tables)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def chain(db_session):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="repo-1",
        resource_type="pr",
        resource_number="1",
        repo_full_name="owner/repo",
    )
    db_session.add(identity)
    db_session.flush()
    session = ActivityObservabilitySession(resource_identity_id=identity.id, session_kind="long_lived", status="open")
    db_session.add(session)
    db_session.flush()
    thread = ActivityThread(session_id=session.id, thread_purpose="reviewer", last_seq=0)
    db_session.add(thread)
    db_session.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer", requested_provider="provider", requested_model="model",
        candidate_chain_json="[]", account_id="account", protocol_family="protocol",
        endpoint_fingerprint="a" * 64, config_snapshot_version=1,
    )
    db_session.add(snapshot)
    db_session.flush()
    invocation = ActivityInvocation(session_id=session.id, status="queued")
    db_session.add(invocation)
    db_session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id, session_id=session.id, thread_id=thread.id,
        role_binding_snapshot_id=snapshot.id, purpose="reviewer", requirement="required",
        is_primary=True, status="queued",
    )
    db_session.add(work_unit)
    db_session.flush()
    attempt = ActivityModelAttempt(
        work_unit_id=work_unit.id, attempt_index=0, logical_call_id="call-1",
        attempt_kind="primary", purpose="review", endpoint_fingerprint="b" * 64,
        contextless_reason="transcript_not_applicable",
    )
    db_session.add(attempt)
    db_session.commit()
    return session, thread, work_unit, attempt


@pytest.mark.asyncio
async def test_canonical_message_discards_reasoning_and_keeps_artifact_reference(db_session, chain):
    session, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    artifact = ActivityNativeArtifact(
        attempt_id=attempt.id, artifact_kind="reasoning", availability=REASONING_SUMMARIZED,
        provider_family="provider", protocol_family="protocol", model_family="model",
        compatibility_key="provider|protocol|model|endpoint", capture_mode=CAPTURE_METADATA_ONLY,
        visibility="admin_only",
    )
    db_session.add(artifact)
    db_session.commit()
    message = await service.append_assistant_message(
        thread_id=thread.id, work_unit_id=work_unit.id, origin_attempt_id=attempt.id,
        content="final", reasoning_content="secret reasoning", artifact_id=artifact.id,
    )
    assert message.seq == 1
    assert "reasoning" not in message.message_json
    assert "secret" not in message.message_json
    assert message.artifact_id == artifact.id
    assert db_session.get(ActivityThread, thread.id).last_seq == 1


@pytest.mark.asyncio
async def test_tool_fields_parent_chain_sensitivity_and_recovery(db_session, chain):
    _, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session), artifact_hash_secret=b"test-key")
    execution = await service.create_tool_execution(
        work_unit_id=work_unit.id, thread_id=thread.id, origin_attempt_id=attempt.id,
        tool_call_id="tool-1", name="read", arguments={"path": "x"},
    )
    assert execution.arguments_json == '{"path":"x"}'
    assert not hasattr(execution, "function")
    await service.start_tool_execution(execution.id)
    assert db_session.get(ActivityToolExecution, execution.id).status == TOOL_STATUS_RUNNING
    await service.finish_tool_execution(execution.id, status=TOOL_STATUS_COMPLETED, result={"ok": True})
    assert db_session.get(ActivityToolExecution, execution.id).status == TOOL_STATUS_COMPLETED
    with pytest.raises(ConflictError):
        await service.finish_tool_execution(execution.id, status=TOOL_STATUS_FAILED, result={"ok": False})

    sensitive = await service.create_tool_execution(
        work_unit_id=work_unit.id, thread_id=thread.id, origin_attempt_id=attempt.id,
        tool_call_id="tool-secret", name="secret", arguments="password", sensitivity=SENSITIVITY_SECRET,
    )
    assert sensitive.arguments_json is None
    assert sensitive.arguments_hash is not None


@pytest.mark.asyncio
async def test_tool_parent_chain_and_dedupe_reject_mismatch(db_session, chain):
    _, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    await service.create_tool_execution(
        work_unit_id=work_unit.id, thread_id=thread.id, origin_attempt_id=attempt.id,
        tool_call_id="duplicate", name="read", arguments="{}",
    )
    with pytest.raises(ConflictError):
        await service.create_tool_execution(
            work_unit_id=work_unit.id, thread_id=thread.id, origin_attempt_id=attempt.id,
            tool_call_id="duplicate", name="write", arguments="{}",
        )
    with pytest.raises(ValueError):
        await service.create_tool_execution(
            work_unit_id=work_unit.id, thread_id=None, origin_attempt_id=attempt.id,
            tool_call_id="bad-parent", name="read", arguments="{}",
        )


@dataclass(frozen=True)
class _Encrypted:
    ciphertext: str = "ciphertext"
    nonce: str = "nonce"
    key_id: str = "key-1"


class _FakeEncryption:
    def encrypt(self, payload):
        assert isinstance(payload, str)
        return _Encrypted()


@pytest.mark.asyncio
async def test_artifact_policy_metadata_summary_encryption_and_retention(db_session, chain):
    _, _, _, attempt = chain
    fixed = datetime(2026, 7, 23, tzinfo=timezone.utc)
    service = ToolService(_AsyncAdapter(db_session), encryption_provider=_FakeEncryption(), clock=lambda: fixed)
    metadata = await service.capture_reasoning_artifact(
        attempt_id=attempt.id, availability=REASONING_PROVIDER_EXPOSED, payload="must not persist",
        provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
    )
    assert metadata.payload_ciphertext is None and metadata.payload_safe_summary is None
    summary = await service.capture_reasoning_artifact(
        attempt_id=attempt.id, availability=REASONING_SUMMARIZED, payload="safe summary",
        provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_SAFE_SUMMARY, provider_allowlist=frozenset({"provider"})),
    )
    assert summary.payload_safe_summary == "safe summary"
    encrypted = await service.capture_reasoning_artifact(
        attempt_id=attempt.id, availability=REASONING_ENCRYPTED_OPAQUE, payload="opaque",
        provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT, retention_days=2),
    )
    assert encrypted.payload_ciphertext == "ciphertext"
    assert encrypted.payload_safe_summary is None
    assert encrypted.retention_expires_at == fixed + timedelta(days=2)


@pytest.mark.asyncio
async def test_artifact_mode_without_encryption_is_rejected(db_session, chain):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    with pytest.raises(RuntimeError, match="encryption"):
        await service.capture_reasoning_artifact(
            attempt_id=attempt.id, availability=REASONING_PROVIDER_EXPOSED, payload="secret",
            provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
            policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT),
        )


@pytest.mark.asyncio
async def test_artifact_read_fails_closed_and_audits_missing_and_denied(db_session, chain):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id, availability=REASONING_PROVIDER_EXPOSED, payload="summary",
        provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_SAFE_SUMMARY),
    )
    assert await service.read_artifact_with_audit(artifact.id, reader="user") is None
    assert await service.read_artifact_with_audit(99999, reader="user") is None
    logs = db_session.scalars(select(ActivityArtifactAccessLog)).all()
    assert [log.outcome for log in logs] == ["denied", "denied_not_found"]


class _Allow:
    async def authorize(self, **kwargs):
        assert kwargs
        return ArtifactAuthorization(allowed=True, authorization_scope="repo:owner/repo", can_display=True)


@pytest.mark.asyncio
async def test_admin_scoped_trace_returns_safe_view_and_opaque_never_displays(db_session, chain):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session), artifact_authorizer=_Allow())
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id, availability=REASONING_ENCRYPTED_OPAQUE, payload=None,
        provider_family="provider", protocol_family="protocol", model_family="model", endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
    )
    view = await service.read_artifact_with_audit(artifact.id, reader="admin")
    assert view is not None and view.payload_safe_summary is None


def _threadless_context(work_unit):
    return InvocationContext(
        invocation_id=work_unit.invocation_id,
        work_unit_id=work_unit.id,
        thread_id=None,
        role_snapshot=RoleConfigSnapshot(
            role="embedding",
            requested_provider="provider",
            requested_model="model",
            requested_thinking_mode=None,
            candidate_chain=(("provider", "model"),),
            account_id="account",
            protocol_family="protocol",
            endpoint_fingerprint="a" * 64,
            config_snapshot_version=1,
            captured_at=datetime.now(timezone.utc),
        ),
    )


@pytest.mark.asyncio
async def test_observed_embedding_success_persists_usage_and_embedding_purpose(db_session):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com", repository_external_id="embedding",
        resource_type="ephemeral", resource_number="1", repo_full_name="owner/repo",
    )
    db_session.add(identity)
    db_session.flush()
    session = ActivityObservabilitySession(resource_identity_id=identity.id, session_kind="ephemeral", status="open")
    db_session.add(session)
    db_session.flush()
    invocation = ActivityInvocation(session_id=session.id, status="queued")
    db_session.add(invocation)
    db_session.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="embedding", requested_provider="provider", requested_model="model",
        candidate_chain_json="[]", account_id="account", protocol_family="protocol",
        endpoint_fingerprint="a" * 64, config_snapshot_version=1,
    )
    db_session.add(snapshot)
    db_session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id, session_id=session.id, thread_id=None,
        role_binding_snapshot_id=snapshot.id, purpose="embedding", requirement="detached",
        is_primary=False, status="queued",
    )
    db_session.add(work_unit)
    db_session.flush()
    sender = ObservedEmbeddingSender(AttemptService(_AsyncAdapter(db_session)), context=_threadless_context(work_unit))

    class _Usage:
        input_tokens = 4
        output_tokens = 0

    class _Response:
        usage = _Usage()

    response, attempt_id = await sender.send_embedding(
        lambda: _completed(_Response()), logical_call_id="embedding-call",
        requested={"provider_id": "provider", "model_id": "model", "protocol_family": "protocol"},
        effective={"provider_id": "provider", "model_id": "model", "protocol_family": "protocol"},
    )
    assert response is not None
    attempt = db_session.get(ActivityModelAttempt, attempt_id)
    assert attempt.status == "completed"
    assert attempt.purpose == "embedding"
    assert attempt.context_revision_id is None
    assert '"input_tokens":4' in attempt.provider_usage_json


async def _completed(value):
    return value


@pytest.mark.asyncio
async def test_attempt_unknown_error_persists_safe_message_only(db_session, chain):
    _, _, _, attempt = chain
    service = AttemptService(_AsyncAdapter(db_session))
    dangerous = "https://evil.test?api_key=secret Authorization: Bearer token body=secret"
    stored = await service.fail(attempt.id, dangerous)
    assert stored.status == "failed"
    assert stored.error_category == "unknown"
    assert stored.error_message == "internal_provider_error"
    serialized = " ".join(
        str(getattr(stored, field))
        for field in ("error_category", "error_message", "provider_usage_json", "normalized_usage_json")
        if getattr(stored, field) is not None
    )
    assert dangerous not in serialized


def test_safe_summary_requires_provider_and_protocol_allowlist_match():
    policy = ReasoningCapturePolicy(
        capture_mode=CAPTURE_SAFE_SUMMARY,
        provider_allowlist=frozenset({"provider"}),
        protocol_allowlist=frozenset({"protocol"}),
    )
    assert safe_summary_or_none(REASONING_SUMMARIZED, "summary", policy) is None
    assert safe_summary_or_none(
        REASONING_SUMMARIZED, "summary", policy, provider_family="provider", protocol_family="protocol"
    ) == "summary"
    assert safe_summary_or_none(REASONING_ENCRYPTED_OPAQUE, "opaque", policy, provider_family="provider", protocol_family="protocol") is None


class _RecordingAttemptService:
    def __init__(self):
        self.events = []

    async def begin_attempt(self, context, logical_call_id, attempt_kind, purpose, requested, effective, context_revision_id, **kwargs):
        self.events.append(("begin", logical_call_id, attempt_kind, purpose, context_revision_id, kwargs))
        return SimpleNamespace(id=1)

    async def first_token(self, attempt_id):
        self.events.append(("first_token", attempt_id))

    async def finish(self, attempt_id, response=None, **kwargs):
        self.events.append(("finish", attempt_id, kwargs))

    async def fail(self, attempt_id, error, **kwargs):
        self.events.append(("fail", attempt_id, kwargs))


def _stream_candidate():
    provider = ProviderDeclaration(
        id="stream-provider", label="stream-provider", family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://stream.example/v1/", auth_scheme=AuthScheme.BEARER,
    )
    metadata = ModelMetadata(
        model_id="stream-model", provider_id=provider.id, display_name="stream-model",
        context_window_tokens=128000, max_output_tokens=4096,
        capabilities=ModelCapabilitySet(), reasoning_params=ReasoningParams(), source=MetadataSource.FALLBACK,
    )
    return ResolvedModel(provider=provider, model=metadata, credential="credential", endpoint=resolve_endpoint(provider, None))


@pytest.mark.asyncio
async def test_unified_stream_real_entry_observes_first_delta_and_done_usage(monkeypatch):
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())

    class _StreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(type="text_delta", text="hello")
            yield UnifiedStreamEvent(type="done", usage=UnifiedUsage(input_tokens=2, output_tokens=1))

    adapter = _StreamAdapter()
    monkeypatch.setattr("backend.services.ai_reviewer.unified_client._get_adapter", lambda _family: adapter)
    client = UnifiedAIClient(observer=sender, context=object(), logical_call_factory=lambda: "stream-call")
    events = [
        event
        async for event in client.stream_with_retry(
            [_stream_candidate()], [UnifiedMessage(role="user", content="hi")], role="reviewer",
            logical_call_factory=lambda: "stream-call",
        )
    ]
    assert [event.type for event in events] == ["text_delta", "done"]
    assert [event[0] for event in attempt_service.events] == ["begin", "first_token", "finish"]
    assert attempt_service.events[0][1:4] == ("stream-call", "primary", "reviewer")
    assert attempt_service.events[2][2]["raw_usage"].output_tokens == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "is_admin", "has_channel", "should_preview"),
    [
        (ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY), True, True, False),
        (
            ReasoningCapturePolicy(
                capture_mode=CAPTURE_SAFE_SUMMARY,
                provider_allowlist=frozenset({"provider"}),
                protocol_allowlist=frozenset({"openai_compatible"}),
            ),
            True,
            True,
            True,
        ),
        (
            ReasoningCapturePolicy(
                capture_mode=CAPTURE_SAFE_SUMMARY,
                provider_allowlist=frozenset({"provider"}),
                protocol_allowlist=frozenset({"openai_compatible"}),
            ),
            False,
            True,
            False,
        ),
        (
            ReasoningCapturePolicy(
                capture_mode=CAPTURE_SAFE_SUMMARY,
                provider_allowlist=frozenset({"provider"}),
                protocol_allowlist=frozenset({"openai_compatible"}),
            ),
            True,
            False,
            False,
        ),
        (ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT), True, True, False),
    ],
)
async def test_consume_stream_event_enforces_reasoning_preview_policy_matrix(
    policy, is_admin, has_channel, should_preview
):
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())
    previews = []
    event = UnifiedStreamEvent(
        type="reasoning_delta",
        text="safe summary",
        reasoning_availability=(
            REASONING_ENCRYPTED_OPAQUE
            if policy.capture_mode == CAPTURE_ARTIFACT
            else REASONING_SUMMARIZED
        ),
        provider_event_metadata={"event": "summary", "url": "https://evil.test", "raw": "secret"},
    )

    await sender.consume_stream_event(
        event,
        attempt=SimpleNamespace(id=1),
        reasoning_policy=policy,
        preview_callback=previews.append,
        is_admin=is_admin,
        has_admin_channel=has_channel,
    )

    assert bool(previews) is should_preview
    assert attempt_service.events[0][0] == "reasoning" if attempt_service.events else True


@pytest.mark.asyncio
async def test_consume_stream_event_records_omitted_phases_without_preview_text():
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())
    previews = []

    async def record_reasoning_event(attempt_id, **kwargs):
        attempt_service.events.append(("reasoning", attempt_id, kwargs))

    attempt_service.record_reasoning_event = record_reasoning_event
    await sender.consume_stream_event(
        UnifiedStreamEvent(type="reasoning_start", text=None, reasoning_availability="omitted"),
        attempt=SimpleNamespace(id=1),
        preview_callback=previews.append,
        is_admin=True,
        has_admin_channel=True,
    )
    await sender.consume_stream_event(
        UnifiedStreamEvent(type="reasoning_end", text=None, reasoning_availability="omitted"),
        attempt=SimpleNamespace(id=1),
        preview_callback=previews.append,
        is_admin=True,
        has_admin_channel=True,
    )

    assert previews == []
    assert [item[2]["event_type"] for item in attempt_service.events] == [
        "reasoning_start", "reasoning_end"
    ]
    assert all(item[2]["availability"] == "omitted" for item in attempt_service.events)


@pytest.mark.asyncio
async def test_canonical_transcript_regression_excludes_all_reasoning_text(db_session, chain):
    _, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    message = await service.append_assistant_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        origin_attempt_id=attempt.id,
        content="final answer",
        reasoning_content="hidden provider reasoning",
    )

    assert message.role == "assistant"
    assert "hidden provider reasoning" not in message.message_json
    assert "reasoning_content" not in message.message_json


@pytest.mark.asyncio
async def test_conversation_restore_follows_head_revision_after_compaction(
    db_session, chain
):
    _, thread, work_unit, _ = chain
    adapter = _AsyncAdapter(db_session)
    context_service = ContextService(adapter)
    tool_service = ToolService(adapter)
    lease = await context_service.acquire_lease(thread.id, work_unit.id)

    await tool_service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={"role": "user", "content": "obsolete request"},
        lease=lease,
    )
    await tool_service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={"role": "assistant", "content": "obsolete answer"},
        lease=lease,
    )
    await tool_service.replace_context_messages(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        messages=[
            {"role": "system", "content": "compressed summary"},
            {"role": "user", "content": "retained request"},
        ],
        lease=lease,
        trigger_reason="threshold",
    )
    await tool_service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={"role": "assistant", "content": "answer after compression"},
        lease=lease,
    )

    assert await tool_service.load_conversation_messages(thread.id) == [
        {"role": "system", "content": "compressed summary"},
        {"role": "user", "content": "retained request"},
        {"role": "assistant", "content": "answer after compression"},
    ]


@pytest.mark.asyncio
async def test_done_usage_finishes_attempt_and_preserves_reported_usage(db_session, chain):
    _, thread, work_unit, _ = chain
    revision = ActivityCanonicalContextRevision(
        thread_id=thread.id,
        revision_number=1,
        message_manifest_json="[]",
        content_hash="hash",
        reason="test",
    )
    db_session.add(revision)
    db_session.flush()
    thread.current_revision_id = revision.id
    db_session.commit()
    context = InvocationContext(
        invocation_id=work_unit.invocation_id,
        work_unit_id=work_unit.id,
        thread_id=work_unit.thread_id,
        role_snapshot=RoleConfigSnapshot(
            role="reviewer", requested_provider="provider", requested_model="model",
            requested_thinking_mode=None, candidate_chain=(("provider", "model"),),
            account_id="account", protocol_family="openai_compatible",
            endpoint_fingerprint="a" * 64, config_snapshot_version=1,
            captured_at=datetime.now(timezone.utc),
        ),
    )
    attempt_service = AttemptService(_AsyncAdapter(db_session))
    sender = ObservedModelSender(attempt_service, context=context)
    candidate = _stream_candidate()

    class _StreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(type="reasoning_delta", text="summary", reasoning_availability=REASONING_SUMMARIZED)
            yield UnifiedStreamEvent(
                type="usage",
                usage=UnifiedUsage(
                    input_tokens=5,
                    reported_fields=frozenset({"input_tokens"}),
                ),
            )
            yield UnifiedStreamEvent(
                type="done",
                usage=UnifiedUsage(
                    output_tokens=3,
                    reasoning_tokens=2,
                    reported_fields=frozenset(
                        {"output_tokens", "reasoning_tokens"}
                    ),
                ),
                stop_reason=StopReason.END_TURN,
            )

    events = [event async for event in sender.send_stream(
        _StreamAdapter(), object(), candidate, UnifiedRequest(model="model", messages=[], max_tokens=10),
        logical_call_id="done-call", reasoning_policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
        context_revision_id=revision.id,
    )]
    assert events[-1].type == "done"
    attempt = db_session.scalars(select(ActivityModelAttempt).where(ActivityModelAttempt.logical_call_id == "done-call")).one()
    assert attempt.status == "completed"
    assert attempt.stop_reason == "end_turn"
    assert attempt.input_tokens == 5 and attempt.output_tokens == 3
    assert attempt.reasoning_tokens == 2
    assert attempt.reasoning_tokens_availability == "reported"


@pytest.mark.asyncio
async def test_unified_stream_real_entry_marks_failure_on_provider_error(monkeypatch):
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())

    class _FailingStreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(type="text_delta", text="partial")
            raise RuntimeError("https://evil.test?api_key=secret body=secret")

    monkeypatch.setattr("backend.services.ai_reviewer.unified_client._get_adapter", lambda _family: _FailingStreamAdapter())
    client = UnifiedAIClient(observer=sender, context=object(), logical_call_factory=lambda: "stream-call")
    with pytest.raises(RuntimeError):
        async for _event in client.stream_with_retry(
            [_stream_candidate()], [UnifiedMessage(role="user", content="hi")], role="reviewer",
        ):
            pass
    assert [event[0] for event in attempt_service.events] == ["begin", "first_token", "fail"]


@pytest.mark.asyncio
async def test_embedding_service_without_context_uses_fake_sdk_production_path(monkeypatch):
    from backend.services import embedding_service as module

    settings = SimpleNamespace(
        embedding_provider="openai", embedding_base_url="https://embedding.example/v1",
        embedding_api_key="key", embedding_model="embedding-model", embedding_batch_size=10,
    )

    class _EmbeddingAPI:
        async def create(self, **_kwargs):
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])], usage=SimpleNamespace(input_tokens=1))

    class _FakeSDK:
        def __init__(self, **kwargs):
            self.max_retries = kwargs["max_retries"]
            self.embeddings = _EmbeddingAPI()

    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "AsyncOpenAI", _FakeSDK)
    service = module.EmbeddingService()
    assert await service.embed_texts(["hello"]) == [[0.1, 0.2]]
    assert service.client.max_retries == 0
