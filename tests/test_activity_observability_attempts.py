"""Task 4 tests for authoritative attempts, tools, and native artifacts."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.ai_protocol.models import (
    AuthScheme,
    MetadataSource,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
    StopReason,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedStreamEvent,
    UnifiedToolCall,
    UnifiedUsage,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.models.activity_observability_models import (
    ActivityArtifactAccessLog,
    ActivityCanonicalContextRevision,
    ActivityContextSnapshot,
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
from backend.models.database import Base
from backend.services.activity_observability.attempt_service import (
    AttemptService,
    _candidate_parts,
)
from backend.services.activity_observability.context_service import ContextService
from backend.services.activity_observability.contracts import (
    InvocationContext,
    RoleConfigSnapshot,
)
from backend.services.activity_observability.observer import (
    ObservedEmbeddingSender,
    ObservedModelSender,
)
from backend.services.activity_observability.reasoning import (
    CAPTURE_ARTIFACT,
    CAPTURE_METADATA_ONLY,
    CAPTURE_SAFE_SUMMARY,
    REASONING_ENCRYPTED_OPAQUE,
    REASONING_PROVIDER_EXPOSED,
    REASONING_SUMMARIZED,
    REASONING_UNAVAILABLE,
    ReasoningCapturePolicy,
    safe_summary_or_none,
)
from backend.services.activity_observability.tool_service import (
    SENSITIVITY_SECRET,
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_RUNNING,
    ArtifactAuthorization,
    ConflictError,
    ToolService,
)
from backend.services.ai_reviewer.unified_client import (
    FallbackConfig,
    UnifiedAIClient,
    _effective_reasoning_snapshot,
    _filter_params_by_capability,
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
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _configure(connection, _record):
        connection.create_collation(
            "ascii_bin", lambda left, right: (left > right) - (left < right)
        )
        connection.create_function("regexp", 2, lambda *args: bool(args))
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name.startswith("activity_observability_")
    ]
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
    session = ActivityObservabilitySession(
        resource_identity_id=identity.id, session_kind="long_lived", status="open"
    )
    db_session.add(session)
    db_session.flush()
    thread = ActivityThread(
        session_id=session.id, thread_purpose="reviewer", last_seq=0
    )
    db_session.add(thread)
    db_session.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="reviewer",
        requested_provider="provider",
        requested_model="model",
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="protocol",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    db_session.add(snapshot)
    db_session.flush()
    invocation = ActivityInvocation(session_id=session.id, status="queued")
    db_session.add(invocation)
    db_session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=session.id,
        thread_id=thread.id,
        role_binding_snapshot_id=snapshot.id,
        purpose="reviewer",
        requirement="required",
        is_primary=True,
        status="queued",
    )
    db_session.add(work_unit)
    db_session.flush()
    attempt = ActivityModelAttempt(
        work_unit_id=work_unit.id,
        attempt_index=0,
        logical_call_id="call-1",
        attempt_kind="primary",
        purpose="review",
        endpoint_fingerprint="b" * 64,
        contextless_reason="transcript_not_applicable",
    )
    db_session.add(attempt)
    db_session.commit()
    return session, thread, work_unit, attempt


@pytest.mark.asyncio
async def test_canonical_message_discards_reasoning_and_keeps_artifact_reference(
    db_session, chain
):
    _session, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    artifact = ActivityNativeArtifact(
        attempt_id=attempt.id,
        artifact_kind="reasoning",
        availability=REASONING_SUMMARIZED,
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        compatibility_key="provider|protocol|model|endpoint",
        capture_mode=CAPTURE_METADATA_ONLY,
        visibility="admin_only",
    )
    db_session.add(artifact)
    db_session.commit()
    message = await service.append_assistant_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        origin_attempt_id=attempt.id,
        content="final",
        reasoning_content="secret reasoning",
        artifact_id=artifact.id,
    )
    assert message.seq == 1
    assert "reasoning" not in message.message_json
    assert "secret" not in message.message_json
    assert message.artifact_id == artifact.id
    assert db_session.get(ActivityThread, thread.id).last_seq == 1


@pytest.mark.asyncio
async def test_tool_fields_parent_chain_sensitivity_and_recovery(db_session, chain):
    _, thread, work_unit, attempt = chain
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FakeEncryption(),
        artifact_hash_secret=b"test-key",
    )
    execution = await service.create_tool_execution(
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        origin_attempt_id=attempt.id,
        tool_call_id="tool-1",
        name="read",
        arguments={"path": "x"},
    )
    assert execution.arguments_json is None
    assert execution.arguments_storage_ref.startswith("artifact:")
    assert not hasattr(execution, "function")
    await service.start_tool_execution(execution.id)
    assert (
        db_session.get(ActivityToolExecution, execution.id).status
        == TOOL_STATUS_RUNNING
    )
    await service.finish_tool_execution(
        execution.id, status=TOOL_STATUS_COMPLETED, result={"ok": True}
    )
    assert (
        db_session.get(ActivityToolExecution, execution.id).status
        == TOOL_STATUS_COMPLETED
    )
    with pytest.raises(ConflictError):
        await service.finish_tool_execution(
            execution.id, status=TOOL_STATUS_FAILED, result={"ok": False}
        )

    sensitive = await service.create_tool_execution(
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        origin_attempt_id=attempt.id,
        tool_call_id="tool-secret",
        name="secret",
        arguments="password",
        sensitivity=SENSITIVITY_SECRET,
    )
    assert sensitive.arguments_json is None
    assert sensitive.arguments_hash is not None


@pytest.mark.asyncio
async def test_tool_parent_chain_and_dedupe_reject_mismatch(db_session, chain):
    _, thread, work_unit, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    await service.create_tool_execution(
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        origin_attempt_id=attempt.id,
        tool_call_id="duplicate",
        name="read",
        arguments="{}",
    )
    with pytest.raises(ConflictError):
        await service.create_tool_execution(
            work_unit_id=work_unit.id,
            thread_id=thread.id,
            origin_attempt_id=attempt.id,
            tool_call_id="duplicate",
            name="write",
            arguments="{}",
        )
    with pytest.raises(ValueError):
        await service.create_tool_execution(
            work_unit_id=work_unit.id,
            thread_id=None,
            origin_attempt_id=attempt.id,
            tool_call_id="bad-parent",
            name="read",
            arguments="{}",
        )


@dataclass(frozen=True)
class _Encrypted:
    ciphertext: str
    nonce: str = "nonce"
    key_id: str = "key-1"


class _FakeEncryption:
    def __init__(self):
        self.payloads = {}

    def encrypt(self, payload):
        assert isinstance(payload, str)
        ciphertext = f"ciphertext-{len(self.payloads) + 1}"
        self.payloads[ciphertext] = payload
        return _Encrypted(ciphertext=ciphertext)

    def decrypt(self, ciphertext, *, nonce, key_id):
        assert nonce == "nonce"
        assert key_id == "key-1"
        return self.payloads[ciphertext]


class _FailingEncryption:
    def encrypt(self, _payload):
        raise RuntimeError("encryption backend unavailable")


@pytest.mark.asyncio
async def test_artifact_policy_metadata_summary_encryption_and_retention(
    db_session, chain
):
    _, _, _, attempt = chain
    fixed = datetime(2026, 7, 23, tzinfo=UTC)
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FakeEncryption(),
        clock=lambda: fixed,
    )
    metadata = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_PROVIDER_EXPOSED,
        payload="must not persist",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
    )
    assert metadata.payload_ciphertext is None and metadata.payload_safe_summary is None
    summary = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_SUMMARIZED,
        payload="safe summary",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(
            capture_mode=CAPTURE_SAFE_SUMMARY,
            provider_allowlist=frozenset({"provider"}),
        ),
    )
    assert summary.payload_safe_summary is None
    assert summary.payload_ciphertext is not None
    encrypted = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_ENCRYPTED_OPAQUE,
        payload="opaque",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT, retention_days=2),
    )
    assert encrypted.payload_ciphertext.startswith("ciphertext-")
    assert encrypted.payload_safe_summary is None
    assert encrypted.retention_expires_at == fixed + timedelta(days=2)


@pytest.mark.asyncio
async def test_artifact_mode_without_encryption_degrades_to_metadata(db_session, chain):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_PROVIDER_EXPOSED,
        payload="secret",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT),
    )
    assert artifact.payload_ciphertext is None
    assert artifact.payload_safe_summary is None
    assert artifact.capture_error == "encryption_unavailable"


@pytest.mark.asyncio
async def test_encryption_failure_persists_metadata_without_plaintext(
    db_session, chain
):
    _, _, _, attempt = chain
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FailingEncryption(),
    )

    reasoning = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_PROVIDER_EXPOSED,
        payload="reasoning secret",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT),
    )
    projection = await service.capture_sensitive_artifact(
        artifact_kind="request_projection",
        payload='{"prompt":"request secret"}',
        attempt_id=attempt.id,
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
    )

    for artifact in (reasoning, projection):
        assert artifact.capture_mode == "metadata_only"
        assert artifact.capture_error == "encryption_failed"
        assert artifact.payload_ciphertext is None
        assert artifact.payload_safe_summary is None


@pytest.mark.asyncio
async def test_artifact_read_fails_closed_and_audits_missing_and_denied(
    db_session, chain
):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session))
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_PROVIDER_EXPOSED,
        payload="summary",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_SAFE_SUMMARY),
    )
    assert await service.read_artifact_with_audit(artifact.id, reader="user") is None
    assert await service.read_artifact_with_audit(99999, reader="user") is None
    logs = db_session.scalars(select(ActivityArtifactAccessLog)).all()
    assert [log.outcome for log in logs] == ["denied", "denied_not_found"]


class _Allow:
    async def authorize(self, **kwargs):
        assert kwargs
        return ArtifactAuthorization(
            allowed=True, authorization_scope="repo:owner/repo", can_display=True
        )


@pytest.mark.asyncio
async def test_admin_scoped_trace_returns_safe_view_and_opaque_never_displays(
    db_session, chain
):
    _, _, _, attempt = chain
    service = ToolService(_AsyncAdapter(db_session), artifact_authorizer=_Allow())
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_ENCRYPTED_OPAQUE,
        payload=None,
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
    )
    view = await service.read_artifact_with_audit(artifact.id, reader="admin")
    assert view is not None and view.payload_safe_summary is None


@pytest.mark.asyncio
async def test_expired_artifact_read_never_decrypts_or_returns_ciphertext(
    db_session, chain
):
    _, _, _, attempt = chain
    fixed = datetime(2026, 7, 23, tzinfo=UTC)

    class _NoDecrypt(_FakeEncryption):
        def decrypt(self, *_args, **_kwargs):
            raise AssertionError("expired artifact must not be decrypted")

    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_NoDecrypt(),
        artifact_authorizer=_Allow(),
        clock=lambda: fixed,
    )
    artifact = await service.capture_reasoning_artifact(
        attempt_id=attempt.id,
        availability=REASONING_PROVIDER_EXPOSED,
        payload="expired secret",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="api",
        policy=ReasoningCapturePolicy(capture_mode=CAPTURE_ARTIFACT, retention_days=1),
    )
    artifact.retention_expires_at = fixed - timedelta(seconds=1)
    db_session.commit()

    view = await service.read_artifact_with_audit(artifact.id, reader="admin")

    assert view is not None
    assert view.payload is None
    assert view.availability == REASONING_UNAVAILABLE
    assert view.payload_unavailable_reason == "retention_expired"


@pytest.mark.asyncio
async def test_purge_expired_artifacts_is_idempotent_and_preserves_future_or_unbounded(
    db_session,
):
    fixed = datetime(2026, 7, 23, tzinfo=UTC)
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FakeEncryption(),
        clock=lambda: fixed,
    )
    expired = await service.capture_sensitive_artifact(
        artifact_kind="request_projection",
        payload="expired",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="expired",
        retention_days=1,
    )
    expired.retention_expires_at = fixed - timedelta(seconds=1)
    future = await service.capture_sensitive_artifact(
        artifact_kind="request_projection",
        payload="future",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="future",
        retention_days=2,
    )
    unbounded = await service.capture_sensitive_artifact(
        artifact_kind="request_projection",
        payload="unbounded",
        provider_family="provider",
        protocol_family="protocol",
        model_family="model",
        endpoint_scope="unbounded",
    )
    db_session.commit()

    assert await service.purge_expired_artifacts() == 1
    assert expired.payload_ciphertext is None
    assert expired.payload_nonce is None
    assert expired.encryption_key_id is None
    assert expired.availability == REASONING_UNAVAILABLE
    assert expired.capture_error == "retention_expired"
    assert expired.replay_allowed is False
    assert future.payload_ciphertext is not None
    assert unbounded.payload_ciphertext is not None
    assert await service.purge_expired_artifacts() == 0


@pytest.mark.asyncio
async def test_authorized_message_artifact_decrypts_without_attempt_parent(
    db_session, chain
):
    _, thread, work_unit, _ = chain
    encryption = _FakeEncryption()
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=encryption,
        artifact_authorizer=_Allow(),
    )
    message = await service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={"role": "user", "content": "private prompt"},
    )

    assert message.content is None
    assert "private prompt" not in message.message_json
    artifact = db_session.get(ActivityNativeArtifact, message.artifact_id)
    assert artifact is not None
    assert artifact.attempt_id is None
    assert "private prompt" not in (artifact.payload_ciphertext or "")

    view = await service.read_artifact_with_audit(
        artifact.id,
        reader="super-admin",
    )
    assert view is not None
    assert json.loads(view.payload) == {"content": "private prompt", "role": "user"}
    assert db_session.scalars(select(ActivityArtifactAccessLog)).all()[-1].outcome == (
        "allowed"
    )


@pytest.mark.asyncio
async def test_canonical_message_persists_allowed_message_kind(db_session, chain):
    """allowlist 内的 message_kind 写入公开 message_json。

    使投影层能暴露稳定的业务标识（如"标签推荐请求/响应"），不依赖敏感
    正文或对 summary 角色的角色推断。A stable business kind is persisted in
    the public message payload so the timeline can distinguish cards without
    touching restricted content.
    """
    _, thread, work_unit, _ = chain
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FakeEncryption(),
    )

    assistant_msg = await service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={
            "role": "assistant",
            "content": '{"labels":[]}',
            "message_kind": "label_recommendation_response",
        },
    )
    user_msg = await service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={
            "role": "user",
            "content": "recommend labels",
            "message_kind": "label_recommendation_request",
        },
    )

    assert json.loads(assistant_msg.message_json)["message_kind"] == (
        "label_recommendation_response"
    )
    assert json.loads(user_msg.message_json)["message_kind"] == (
        "label_recommendation_request"
    )


@pytest.mark.asyncio
async def test_canonical_message_drops_unapproved_message_kind(db_session, chain):
    """allowlist 外的 message_kind 不落入公开 message_json。

    避免任意元数据借道公开投影泄漏。Unapproved kinds are stripped so
    arbitrary metadata cannot escape through the public projection.
    """
    _, thread, work_unit, _ = chain
    service = ToolService(_AsyncAdapter(db_session))
    message = await service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        message={
            "role": "assistant",
            "content": "hi",
            "message_kind": "not_a_real_kind",
        },
    )
    assert "message_kind" not in json.loads(message.message_json)


@pytest.mark.asyncio
async def test_tool_request_artifact_keeps_structured_unified_tool_calls(
    db_session, chain
):
    _, thread, work_unit, attempt = chain
    service = ToolService(
        _AsyncAdapter(db_session),
        encryption_provider=_FakeEncryption(),
        artifact_authorizer=_Allow(),
    )
    message = await service.append_conversation_message(
        thread_id=thread.id,
        work_unit_id=work_unit.id,
        origin_attempt_id=attempt.id,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                UnifiedToolCall(
                    id="call-structured",
                    name="read_file",
                    arguments='{"path":"README.md"}',
                )
            ],
        },
    )

    view = await service.read_artifact_with_audit(
        message.artifact_id,
        reader="super-admin",
    )
    payload = json.loads(view.payload)
    assert payload["tool_calls"] == [
        {
            "id": "call-structured",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
    ]
    assert "UnifiedToolCall" not in view.payload
    execution = db_session.scalars(
        select(ActivityToolExecution).where(
            ActivityToolExecution.tool_call_id == "call-structured"
        )
    ).one()
    assert execution.name == "read_file"


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
            captured_at=datetime.now(UTC),
        ),
    )


@pytest.mark.asyncio
async def test_observed_embedding_success_persists_usage_and_embedding_purpose(
    db_session,
):
    identity = ActivityResourceIdentity(
        source_system_instance="github.com",
        repository_external_id="embedding",
        resource_type="ephemeral",
        resource_number="1",
        repo_full_name="owner/repo",
    )
    db_session.add(identity)
    db_session.flush()
    session = ActivityObservabilitySession(
        resource_identity_id=identity.id, session_kind="ephemeral", status="open"
    )
    db_session.add(session)
    db_session.flush()
    invocation = ActivityInvocation(session_id=session.id, status="queued")
    db_session.add(invocation)
    db_session.flush()
    snapshot = ActivityObservabilityRoleBindingSnapshot(
        role="embedding",
        requested_provider="provider",
        requested_model="model",
        candidate_chain_json="[]",
        account_id="account",
        protocol_family="protocol",
        endpoint_fingerprint="a" * 64,
        config_snapshot_version=1,
    )
    db_session.add(snapshot)
    db_session.flush()
    work_unit = ActivityInvocationWorkUnit(
        invocation_id=invocation.id,
        session_id=session.id,
        thread_id=None,
        role_binding_snapshot_id=snapshot.id,
        purpose="embedding",
        requirement="detached",
        is_primary=False,
        status="queued",
    )
    db_session.add(work_unit)
    db_session.flush()
    sender = ObservedEmbeddingSender(
        AttemptService(_AsyncAdapter(db_session)),
        context=_threadless_context(work_unit),
    )

    class _Usage:
        input_tokens = 4
        output_tokens = 0

    class _Response:
        usage = _Usage()

    response, attempt_id = await sender.send_embedding(
        lambda: _completed(_Response()),
        logical_call_id="embedding-call",
        requested={
            "provider_id": "provider",
            "model_id": "model",
            "protocol_family": "protocol",
        },
        effective={
            "provider_id": "provider",
            "model_id": "model",
            "protocol_family": "protocol",
        },
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
    dangerous = (
        "https://evil.test?api_key=secret Authorization: Bearer token body=secret"
    )
    stored = await service.fail(attempt.id, dangerous)
    assert stored.status == "failed"
    assert stored.error_category == "unknown"
    assert stored.error_message == "internal_provider_error"
    serialized = " ".join(
        str(getattr(stored, field))
        for field in (
            "error_category",
            "error_message",
            "provider_usage_json",
            "normalized_usage_json",
        )
        if getattr(stored, field) is not None
    )
    assert dangerous not in serialized


@pytest.mark.asyncio
async def test_nonstream_attempt_applies_normalized_nested_usage(db_session, chain):
    _, _, _, attempt = chain
    service = AttemptService(_AsyncAdapter(db_session))
    raw_usage = {
        "prompt_tokens": 4258,
        "completion_tokens": 181,
        "prompt_cache_hit_tokens": 3000,
        "prompt_cache_miss_tokens": 1258,
        "completion_tokens_details": {"reasoning_tokens": 120},
        "total_tokens": 4439,
    }
    response = UnifiedResponse(
        content="done",
        tool_calls=[],
        stop_reason=StopReason.END_TURN,
        usage=UnifiedUsage(
            input_tokens=4258,
            output_tokens=181,
            cache_read_tokens=3000,
            reasoning_tokens=120,
            reported_fields=frozenset(
                {
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "reasoning_tokens",
                }
            ),
        ),
        raw=SimpleNamespace(json=lambda: {"usage": raw_usage}),
    )

    stored = await service.finish(attempt.id, response)

    assert stored.input_tokens == 4258
    assert stored.output_tokens == 181
    assert stored.cached_input_tokens == 3000
    assert stored.reasoning_tokens == 120
    assert stored.cached_input_tokens_availability == "reported"
    assert stored.reasoning_tokens_availability == "reported"
    assert json.loads(stored.normalized_usage_json) == {
        "cache_read_tokens": 3000,
        "input_tokens": 4258,
        "output_tokens": 181,
        "reasoning_tokens": 120,
    }


@pytest.mark.asyncio
async def test_nonstream_sender_supersedes_context_estimate_with_provider_usage(
    db_session,
    chain,
):
    _, thread, work_unit, _ = chain
    revision = ActivityCanonicalContextRevision(
        thread_id=thread.id,
        revision_number=1,
        message_manifest_json="[]",
        content_hash="nonstream-context",
        reason="test",
    )
    db_session.add(revision)
    db_session.flush()
    thread.current_revision_id = revision.id
    db_session.commit()
    context = InvocationContext(
        invocation_id=work_unit.invocation_id,
        work_unit_id=work_unit.id,
        thread_id=thread.id,
        role_snapshot=RoleConfigSnapshot(
            role="reviewer",
            requested_provider="stream-provider",
            requested_model="stream-model",
            requested_thinking_mode="adaptive",
            candidate_chain=(("stream-provider", "stream-model"),),
            account_id="account",
            protocol_family="openai_compatible",
            endpoint_fingerprint="a" * 64,
            config_snapshot_version=1,
            captured_at=datetime.now(UTC),
        ),
    )
    adapter_session = _AsyncAdapter(db_session)
    sender = ObservedModelSender(
        AttemptService(adapter_session),
        context=context,
        context_service=ContextService(adapter_session),
    )

    class _Adapter:
        @staticmethod
        def serialize_request(request):
            return {"model": request.model, "messages": ["estimated payload"]}

        async def chat(self, *_args, **_kwargs):
            return UnifiedResponse(
                content="done",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=UnifiedUsage(
                    input_tokens=18432,
                    output_tokens=574,
                    cache_read_tokens=18304,
                    reasoning_tokens=32,
                    reported_fields=frozenset(
                        {
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "reasoning_tokens",
                        }
                    ),
                ),
            )

    _, attempt_id = await sender.send_chat(
        _Adapter(),
        object(),
        _stream_candidate(),
        UnifiedRequest(
            model="stream-model",
            messages=[UnifiedMessage(role="user", content="inspect issue")],
            max_tokens=4096,
        ),
        logical_call_id="nonstream-context",
        context_revision_id=revision.id,
    )

    snapshots = db_session.scalars(
        select(ActivityContextSnapshot)
        .where(ActivityContextSnapshot.attempt_id == attempt_id)
        .order_by(ActivityContextSnapshot.id)
    ).all()
    assert [item.snapshot_kind for item in snapshots] == [
        "before_request",
        "after_request",
    ]
    assert snapshots[0].context_tokens_availability == "estimated"
    assert snapshots[-1].context_tokens == 18432
    assert snapshots[-1].context_tokens_availability == "reported"
    assert snapshots[-1].context_tokens_source == "provider"
    assert snapshots[-1].cache_read_tokens == 18304
    assert snapshots[-1].reasoning_context_tokens == 32


def test_safe_summary_requires_provider_and_protocol_allowlist_match():
    policy = ReasoningCapturePolicy(
        capture_mode=CAPTURE_SAFE_SUMMARY,
        provider_allowlist=frozenset({"provider"}),
        protocol_allowlist=frozenset({"protocol"}),
    )
    assert safe_summary_or_none(REASONING_SUMMARIZED, "summary", policy) is None
    assert (
        safe_summary_or_none(
            REASONING_SUMMARIZED,
            "summary",
            policy,
            provider_family="provider",
            protocol_family="protocol",
        )
        == "summary"
    )
    assert (
        safe_summary_or_none(
            REASONING_ENCRYPTED_OPAQUE,
            "opaque",
            policy,
            provider_family="provider",
            protocol_family="protocol",
        )
        is None
    )


class _RecordingAttemptService:
    def __init__(self):
        self.events = []

    async def begin_attempt(
        self,
        context,
        logical_call_id,
        attempt_kind,
        purpose,
        requested,
        effective,
        context_revision_id,
        **kwargs,
    ):
        self.events.append(
            (
                "begin",
                logical_call_id,
                attempt_kind,
                purpose,
                context_revision_id,
                kwargs,
                requested,
                effective,
            )
        )
        return SimpleNamespace(id=1)

    async def first_token(self, attempt_id):
        self.events.append(("first_token", attempt_id))

    async def finish(self, attempt_id, response=None, **kwargs):
        self.events.append(("finish", attempt_id, kwargs))

    async def fail(self, attempt_id, error, **kwargs):
        self.events.append(("fail", attempt_id, kwargs))


def _stream_candidate():
    provider = ProviderDeclaration(
        id="stream-provider",
        label="stream-provider",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://stream.example/v1/",
        auth_scheme=AuthScheme.BEARER,
    )
    metadata = ModelMetadata(
        model_id="stream-model",
        provider_id=provider.id,
        display_name="stream-model",
        context_window_tokens=128000,
        max_output_tokens=4096,
        capabilities=ModelCapabilitySet(),
        reasoning_params=ReasoningParams(),
        source=MetadataSource.FALLBACK,
    )
    return ResolvedModel(
        provider=provider,
        model=metadata,
        credential="credential",
        endpoint=resolve_endpoint(provider, None),
    )


def test_candidate_parts_uses_effective_protocol_without_snapshot():
    candidate = replace(
        _stream_candidate(),
        protocol=ProtocolFamily.ANTHROPIC_NATIVE,
    )

    assert _candidate_parts(candidate)["protocol"] == ProtocolFamily.ANTHROPIC_NATIVE.value


def test_artifact_identity_uses_effective_protocol_without_snapshot():
    candidate = replace(
        _stream_candidate(),
        protocol=ProtocolFamily.ANTHROPIC_NATIVE,
    )

    identity = ObservedModelSender._artifact_identity(candidate, None)

    assert identity["protocol_family"] == ProtocolFamily.ANTHROPIC_NATIVE.value


@pytest.mark.asyncio
async def test_sticky_winner_does_not_change_requested_primary_candidate(monkeypatch):
    primary = _stream_candidate()
    fallback_provider = replace(
        primary.provider,
        id="fallback-provider",
        label="fallback-provider",
        base_url="https://fallback.example/v1/",
    )
    fallback = replace(
        primary,
        provider=fallback_provider,
        model=replace(
            primary.model,
            model_id="fallback-model",
            provider_id=fallback_provider.id,
        ),
        endpoint=resolve_endpoint(fallback_provider, None),
    )

    class _Adapter:
        def serialize_request(self, request):
            return {"model": request.model}

        async def chat(self, *_args, **_kwargs):
            return UnifiedResponse(
                content="ok",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=UnifiedUsage(input_tokens=1, output_tokens=1),
            )

    monkeypatch.setattr(
        "backend.services.ai_reviewer.unified_client._get_adapter",
        lambda _family: _Adapter(),
    )
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())
    client = UnifiedAIClient(
        observer=sender,
        context=object(),
        fallback_config=FallbackConfig(
            enabled=True,
            max_candidates=2,
            max_retries=1,
            sticky_candidate=True,
        ),
        logical_call_factory=lambda: "sticky-call",
    )
    client._last_successful["reviewer"] = (
        fallback.provider.id,
        fallback.model.model_id,
    )

    await client.call_with_retry(
        [primary, fallback],
        [UnifiedMessage(role="user", content="hi")],
        model="",
        role="reviewer",
    )

    begin = attempt_service.events[0]
    requested = begin[6]
    effective = begin[7]
    assert requested.provider.id == primary.provider.id
    assert requested.model.model_id == primary.model.model_id
    assert effective.provider.id == fallback.provider.id
    assert effective.model.model_id == fallback.model.model_id


def test_effective_reasoning_snapshot_uses_final_capability_filtered_request():
    unsupported = _stream_candidate()
    filtered = _filter_params_by_capability(
        unsupported.model,
        temperature=0.4,
        top_p=0.8,
        top_k=12,
        thinking={"type": "adaptive"},
        effort="high",
    )
    unsupported_request = UnifiedRequest(
        model=unsupported.model.model_id,
        messages=[UnifiedMessage(role="user", content="hi")],
        max_tokens=1234,
        thinking=filtered["thinking"],
        effort=filtered["effort"],
        temperature=filtered["temperature"],
        top_p=filtered["top_p"],
        top_k=filtered["top_k"],
        tool_choice="auto",
    )
    unsupported_snapshot = _effective_reasoning_snapshot(
        unsupported,
        unsupported_request,
        requested_thinking={"type": "adaptive"},
        requested_effort="high",
    )
    assert unsupported_snapshot.requested_thinking_mode == "adaptive"
    assert unsupported_snapshot.effective_thinking_mode == "unsupported"
    assert unsupported_snapshot.requested_effort == "high"
    assert unsupported_snapshot.effective_effort == "unsupported"
    assert unsupported_snapshot.top_k is None
    assert unsupported_snapshot.max_output_tokens == 1234
    assert unsupported_snapshot.tool_choice == "auto"

    supported = replace(
        unsupported,
        model=replace(
            unsupported.model,
            capabilities=ModelCapabilitySet(
                thinking=True,
                effort=True,
                temperature=True,
                top_p=True,
                top_k=True,
            ),
            reasoning_params=ReasoningParams(
                max_output_tokens=8192,
                thinking={"type": "adaptive"},
                effort="medium",
            ),
        ),
    )
    filtered = _filter_params_by_capability(
        supported.model,
        temperature=None,
        top_p=None,
        top_k=9,
        thinking={"type": "disabled"},
        effort="max",
    )
    supported_request = UnifiedRequest(
        model=supported.model.model_id,
        messages=[UnifiedMessage(role="user", content="hi")],
        max_tokens=8192,
        thinking=filtered["thinking"],
        effort=filtered["effort"],
        top_k=filtered["top_k"],
    )
    supported_snapshot = _effective_reasoning_snapshot(
        supported,
        supported_request,
        requested_thinking={"type": "disabled"},
        requested_effort="max",
    )
    assert supported_snapshot.requested_thinking_mode == "disabled"
    assert supported_snapshot.effective_thinking_mode == "disabled"
    assert supported_snapshot.requested_effort == "max"
    assert supported_snapshot.effective_effort == "max"
    assert supported_snapshot.top_k == 9


@pytest.mark.asyncio
async def test_observability_failures_do_not_replace_provider_result_or_error():
    class _BrokenObservation:
        async def begin_attempt(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    class _Adapter:
        def __init__(self, error=None):
            self.calls = 0
            self.error = error

        def serialize_request(self, request):
            return {"model": request.model}

        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return SimpleNamespace(
                reasoning_content=None,
                usage=UnifiedUsage(),
                stop_reason=StopReason.END_TURN,
            )

    sender = ObservedModelSender(_BrokenObservation(), context=object())
    candidate = _stream_candidate()
    request = UnifiedRequest(
        model=candidate.model.model_id,
        messages=[UnifiedMessage(role="user", content="hi")],
        max_tokens=128,
    )
    success_adapter = _Adapter()
    response, attempt_id = await sender.send_chat(
        success_adapter,
        object(),
        candidate,
        request,
        logical_call_id="best-effort-success",
    )
    assert response.usage is not None
    assert attempt_id is None
    assert success_adapter.calls == 1

    provider_error = RuntimeError("provider failed")
    failing_adapter = _Adapter(provider_error)
    with pytest.raises(RuntimeError) as raised:
        await sender.send_chat(
            failing_adapter,
            object(),
            candidate,
            request,
            logical_call_id="best-effort-provider-error",
        )
    assert raised.value is provider_error
    assert failing_adapter.calls == 1


@pytest.mark.asyncio
async def test_unified_stream_real_entry_observes_first_delta_and_done_usage(
    monkeypatch,
):
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())

    class _StreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(type="text_delta", text="hello")
            yield UnifiedStreamEvent(
                type="done", usage=UnifiedUsage(input_tokens=2, output_tokens=1)
            )

    adapter = _StreamAdapter()
    monkeypatch.setattr(
        "backend.services.ai_reviewer.unified_client._get_adapter",
        lambda _family: adapter,
    )
    client = UnifiedAIClient(
        observer=sender, context=object(), logical_call_factory=lambda: "stream-call"
    )
    events = [
        event
        async for event in client.stream_with_retry(
            [_stream_candidate()],
            [UnifiedMessage(role="user", content="hi")],
            role="reviewer",
            logical_call_factory=lambda: "stream-call",
        )
    ]
    assert [event.type for event in events] == ["text_delta", "done"]
    assert [event[0] for event in attempt_service.events] == [
        "begin",
        "first_token",
        "finish",
    ]
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
        provider_event_metadata={
            "event": "summary",
            "url": "https://evil.test",
            "raw": "secret",
        },
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
    assert (
        attempt_service.events[0][0] == "reasoning" if attempt_service.events else True
    )


@pytest.mark.asyncio
async def test_consume_stream_event_records_omitted_phases_without_preview_text():
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())
    previews = []

    async def record_reasoning_event(attempt_id, **kwargs):
        attempt_service.events.append(("reasoning", attempt_id, kwargs))

    attempt_service.record_reasoning_event = record_reasoning_event
    await sender.consume_stream_event(
        UnifiedStreamEvent(
            type="reasoning_start", text=None, reasoning_availability="omitted"
        ),
        attempt=SimpleNamespace(id=1),
        preview_callback=previews.append,
        is_admin=True,
        has_admin_channel=True,
    )
    await sender.consume_stream_event(
        UnifiedStreamEvent(
            type="reasoning_end", text=None, reasoning_availability="omitted"
        ),
        attempt=SimpleNamespace(id=1),
        preview_callback=previews.append,
        is_admin=True,
        has_admin_channel=True,
    )

    assert previews == []
    assert [item[2]["event_type"] for item in attempt_service.events] == [
        "reasoning_start",
        "reasoning_end",
    ]
    assert all(item[2]["availability"] == "omitted" for item in attempt_service.events)


@pytest.mark.asyncio
async def test_canonical_transcript_regression_excludes_all_reasoning_text(
    db_session, chain
):
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
    tool_service = ToolService(adapter, encryption_provider=_FakeEncryption())
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
async def test_done_usage_finishes_attempt_and_preserves_reported_usage(
    db_session, chain
):
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
            role="reviewer",
            requested_provider="provider",
            requested_model="model",
            requested_thinking_mode=None,
            candidate_chain=(("provider", "model"),),
            account_id="account",
            protocol_family="openai_compatible",
            endpoint_fingerprint="a" * 64,
            config_snapshot_version=1,
            captured_at=datetime.now(UTC),
        ),
    )
    attempt_service = AttemptService(_AsyncAdapter(db_session))
    sender = ObservedModelSender(
        attempt_service,
        context=context,
        context_service=ContextService(_AsyncAdapter(db_session)),
    )
    candidate = _stream_candidate()

    class _StreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(
                type="reasoning_delta",
                text="summary",
                reasoning_availability=REASONING_SUMMARIZED,
            )
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
                    reported_fields=frozenset({"output_tokens", "reasoning_tokens"}),
                ),
                stop_reason=StopReason.END_TURN,
            )

    events = [
        event
        async for event in sender.send_stream(
            _StreamAdapter(),
            object(),
            candidate,
            UnifiedRequest(model="model", messages=[], max_tokens=10),
            logical_call_id="done-call",
            reasoning_policy=ReasoningCapturePolicy(capture_mode=CAPTURE_METADATA_ONLY),
            context_revision_id=revision.id,
        )
    ]
    assert events[-1].type == "done"
    attempt = db_session.scalars(
        select(ActivityModelAttempt).where(
            ActivityModelAttempt.logical_call_id == "done-call"
        )
    ).one()
    assert attempt.status == "completed"
    assert attempt.stop_reason == "end_turn"
    assert attempt.input_tokens == 5 and attempt.output_tokens == 3
    assert attempt.reasoning_tokens == 2
    assert attempt.reasoning_tokens_availability == "reported"
    context_snapshots = db_session.scalars(
        select(ActivityContextSnapshot)
        .where(ActivityContextSnapshot.attempt_id == attempt.id)
        .order_by(ActivityContextSnapshot.id)
    ).all()
    assert [item.snapshot_kind for item in context_snapshots] == [
        "before_request",
        "after_request",
    ]
    reported = context_snapshots[-1]
    assert reported.context_tokens == 5
    assert reported.context_tokens_availability == "reported"
    assert reported.context_tokens_source == "provider"
    assert reported.context_window_tokens == 128000
    assert reported.cache_read_tokens is None
    assert reported.reasoning_context_tokens == 2
    assert reported.reasoning_context_tokens_availability == "reported"


@pytest.mark.asyncio
async def test_unified_stream_real_entry_marks_failure_on_provider_error(monkeypatch):
    attempt_service = _RecordingAttemptService()
    sender = ObservedModelSender(attempt_service, context=object())

    class _FailingStreamAdapter:
        async def stream(self, *_args, **_kwargs):
            yield UnifiedStreamEvent(type="text_delta", text="partial")
            raise RuntimeError("https://evil.test?api_key=secret body=secret")

    monkeypatch.setattr(
        "backend.services.ai_reviewer.unified_client._get_adapter",
        lambda _family: _FailingStreamAdapter(),
    )
    client = UnifiedAIClient(
        observer=sender, context=object(), logical_call_factory=lambda: "stream-call"
    )
    with pytest.raises(RuntimeError):
        async for _event in client.stream_with_retry(
            [_stream_candidate()],
            [UnifiedMessage(role="user", content="hi")],
            role="reviewer",
        ):
            pass
    assert [event[0] for event in attempt_service.events] == [
        "begin",
        "first_token",
        "fail",
    ]


@pytest.mark.asyncio
async def test_embedding_service_without_context_uses_fake_sdk_production_path(
    monkeypatch,
):
    from backend.services import embedding_service as module

    settings = SimpleNamespace(
        embedding_provider="openai",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="key",
        embedding_model="embedding-model",
        embedding_batch_size=10,
    )

    class _EmbeddingAPI:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2])],
                usage=SimpleNamespace(input_tokens=1),
            )

    class _FakeSDK:
        def __init__(self, **kwargs):
            self.max_retries = kwargs["max_retries"]
            self.embeddings = _EmbeddingAPI()

    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "AsyncOpenAI", _FakeSDK)
    service = module.EmbeddingService()
    assert await service.embed_texts(["hello"]) == [[0.1, 0.2]]
    assert service.client.max_retries == 0
