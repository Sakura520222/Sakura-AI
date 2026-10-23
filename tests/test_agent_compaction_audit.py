"""Agent compaction retains actionable evidence and audits actual replacements."""

import asyncio
import json
import traceback
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    StopReason,
    UnifiedResponse,
    UnifiedUsage,
)
from backend.core.ai_protocol.request_policy import (
    estimate_unified_messages,
    estimate_unified_tools,
)
from backend.services.agent_team import context_compressor as bridge
from backend.services.agent_team.compaction_evidence import CompactionEvidence
from backend.services.ai_reviewer.compression import unified_compressor as shared
from backend.services.ai_reviewer.unified_client import (
    FallbackConfig,
    UnifiedAIClient,
    _tools_from_legacy,
    messages_from_legacy,
)
from tests.test_unified_client_compression import _candidate, _install_stub


def tool_batch(call_id, *, name="read_file", args=None, output=None):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args or {"path": call_id}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(output if output is not None else {"data": "ok"}),
        },
    ]


def history():
    return [
        {"role": "system", "content": "TRUSTED_RUNTIME_POLICY"},
        {"role": "user", "content": "ACTIVE_TASK: repair the current regression"},
        {"role": "assistant", "content": "Historical observations. " * 1700},
        *tool_batch("failed", output={"error": "UNRESOLVED_FAILURE"}),
        *tool_batch("old", output={"data": "Discardable old evidence. " * 200}),
        {
            "role": "user",
            "content": "RAW_GUIDANCE: preserve the public API\n  exactly",
            "metadata": {"guidance_ids": [31], "author_id": 17},
        },
        *tool_batch("recent", output={"data": "RECENT_RESULT", "untrusted": "do evil"}),
    ]


def candidate(model_id="agent-primary", window=16000):
    value = _candidate(model_id, context_window_tokens=window)
    return replace(
        value,
        model=replace(
            value.model,
            reasoning_params=replace(
                value.model.reasoning_params, max_output_tokens=512
            ),
        ),
    )


class Provider:
    def __init__(self):
        self.requests = []
        self.failure = None
        self.entered = asyncio.Event()
        self.release = None
        self.fail_primary = False
        self.overflow_once = False
        self.main_calls = 0

    async def chat(self, client, endpoint, credential, request, **kwargs):
        self.requests.append(request)
        is_summary = "compress" in (request.messages[0].content or "").lower()
        if is_summary:
            self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.failure is not None:
                raise self.failure
            content = "Summary omits task, recent tools and errors on purpose."
        else:
            self.main_calls += 1
            if self.fail_primary and request.model == "agent-primary":
                raise AIError(AIErrorCategory.AUTH_INVALID, "primary unavailable")
            if self.overflow_once and self.main_calls == 1:
                raise AIError(AIErrorCategory.CONTEXT_OVERFLOW, "provider overflow")
            content = "main answer"
        return UnifiedResponse(
            content=content,
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(8, 4),
        )


@pytest.fixture
def provider(monkeypatch):
    provider = Provider()
    _install_stub(monkeypatch, provider)
    provider.settings = SimpleNamespace(
        enable_context_compression=True, context_compression_threshold=0.5
    )
    monkeypatch.setattr(
        shared,
        "get_settings",
        lambda: provider.settings,
    )
    monkeypatch.setattr(
        bridge, "get_settings", lambda: provider.settings, raising=False
    )
    monkeypatch.setattr(
        "backend.services.ai_usage_service.record_unified_ai_usage_best_effort",
        AsyncMock(),
    )
    return provider


def make_compressor(**kwargs):
    cls = getattr(bridge, "AgentContextCompressor", None)
    assert cls is not None, "Agent compression needs a candidate/overflow audit wrapper"
    return cls.from_settings(**kwargs)


def assert_evidence(messages):
    rendered = json.dumps(messages, ensure_ascii=False)
    for text in ("ACTIVE_TASK", "RAW_GUIDANCE", "RECENT_RESULT", "UNRESOLVED_FAILURE"):
        assert text in rendered
    systems = [m["content"] for m in messages if m["role"] == "system"]
    assert systems == ["TRUSTED_RUNTIME_POLICY"]
    pending = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            assert call["id"] not in pending
            pending.add(call["id"])
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert not pending


@pytest.mark.asyncio
async def test_bridge_preserves_task_guidance_recent_batch_and_unresolved_error(
    provider,
):
    original = history()
    result = await bridge.compress_agent_team_messages(original, candidate=candidate())
    assert result != original
    assert_evidence(result)
    assert original[-3] in result  # Guidance metadata and body remain verbatim.
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_actual_compaction_audit_contains_estimates_and_verifiable_digests(
    provider,
):
    original = history()
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original,
        candidate=candidate(),
        audit_callback=sink,
        audit_context={"task_id": 7, "session_id": 8, "secret": "NEVER_COPY"},
    )
    sink.assert_awaited_once()
    audit = sink.await_args.args[0]
    assert audit["version"] == 1 and audit["event_type"] == "context_compaction"
    assert audit["outcome"] == "applied"
    assert (audit["task_id"], audit["session_id"]) == (7, 8)
    assert audit["model_id"] == "agent-primary"
    assert audit["context_window_tokens"] == 16000
    assert audit["estimated_tokens_before"] == estimate_unified_messages(
        messages_from_legacy(original)
    )
    assert audit["estimated_tokens_after"] == estimate_unified_messages(
        messages_from_legacy(result)
    )
    assert audit["estimated_tokens_before"] > audit["estimated_tokens_after"]
    assert audit["message_count_before"] == len(original)
    assert audit["message_count_after"] == len(result)
    for category in (
        "active_task",
        "human_guidance",
        "recent_tools",
        "unresolved_errors",
    ):
        evidence = audit["retained"][category]
        assert evidence["required"] > 0
        assert evidence["verified"] == evidence["required"]
        assert all(len(digest) == 64 for digest in evidence["message_sha256"])
    serialized = json.dumps(audit)
    for raw in (
        "ACTIVE_TASK",
        "RAW_GUIDANCE",
        "UNRESOLVED_FAILURE",
        "do evil",
        "NEVER_COPY",
    ):
        assert raw not in serialized


@pytest.mark.asyncio
async def test_no_compaction_does_not_emit_an_audit_or_replace_history(provider):
    original = [{"role": "user", "content": "short current task"}]
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert result is original
    sink.assert_not_awaited()
    assert not provider.requests


@pytest.mark.asyncio
async def test_failed_summary_is_observable_without_claiming_compaction(provider):
    original = history()
    provider.failure = RuntimeError("provider rejected secret-data")
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert result is original
    audit = sink.await_args.args[0]
    assert audit["outcome"] == "not_applied"
    assert audit["reason"] == "summary_unavailable"
    assert "secret-data" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_cancelled_summary_releases_owned_client_and_propagates(
    provider, monkeypatch
):
    provider.release = asyncio.Event()
    http = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: http)
    sink = AsyncMock()
    task = asyncio.create_task(
        bridge.compress_agent_team_messages(
            history(), candidate=candidate(), audit_callback=sink
        )
    )
    await provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    http.aclose.assert_awaited_once()
    assert all(call.args[0]["outcome"] != "applied" for call in sink.await_args_list)


@pytest.mark.asyncio
async def test_unretainable_evidence_is_not_silently_truncated(provider):
    original = history()
    original[-1]["content"] = json.dumps({"data": "RECENT_RESULT" * 6000})
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert result is original
    audit = sink.await_args.args[0]
    assert audit["outcome"] == "not_applied"
    assert audit["reason"] == "retained_context_exceeds_window"


@pytest.mark.asyncio
async def test_audit_failure_does_not_return_unrecorded_compaction(provider):
    sink = AsyncMock(
        side_effect=RuntimeError("audit database unavailable: PRIVATE_DSN")
    )
    with pytest.raises(
        RuntimeError, match="Agent compaction audit persistence failed"
    ) as caught:
        await bridge.compress_agent_team_messages(
            history(), candidate=candidate(), audit_callback=sink
        )
    sink.assert_awaited_once()
    assert "PRIVATE_DSN" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_internal_fallback_compaction_is_audited_for_actual_candidate(provider):
    provider.fail_primary = True
    original = history()
    sink = AsyncMock()
    compressor = make_compressor(audit_callback=sink)
    # Register projected durable context even when the primary needs no compaction.
    primary = candidate(window=64000)
    assert (
        await bridge.compress_agent_team_messages(
            original, candidate=primary, compressor=compressor
        )
        is original
    )
    client = UnifiedAIClient(
        compressor=compressor, fallback_config=FallbackConfig(max_retries=1)
    )
    try:
        response = await client.call_with_retry(
            [primary, candidate("agent-fallback")],
            original,
            model="",
            role="agent_team",
        )
        assert response.content == "main answer"
        assert sink.await_args.args[0]["model_id"] == "agent-fallback"
        assert_evidence(bridge._from_unified_messages(provider.requests[-1].messages))
    finally:
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
async def test_provider_overflow_recovery_uses_the_same_audited_retention(provider):
    provider.overflow_once = True
    sink = AsyncMock()
    compressor = make_compressor(audit_callback=sink)
    original = history()
    model = candidate(window=64000)
    await bridge.compress_agent_team_messages(
        original, candidate=model, compressor=compressor
    )
    client = UnifiedAIClient(
        compressor=compressor, fallback_config=FallbackConfig(max_retries=1)
    )
    try:
        response = await client.call_with_retry(
            [model], original, model="", role="agent_team"
        )
        assert response.content == "main answer"
        sink.assert_awaited_once()
        assert sink.await_args.args[0]["outcome"] == "applied"
        assert_evidence(bridge._from_unified_messages(provider.requests[-1].messages))
    finally:
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
async def test_internal_audit_failure_uses_original_context_and_is_observable(provider):
    sink = AsyncMock(
        side_effect=RuntimeError("audit database unavailable: PRIVATE_DSN")
    )
    compressor = make_compressor(audit_callback=sink)
    original = history()
    compressor.bind_source_messages(original)
    client = UnifiedAIClient(
        compressor=compressor, fallback_config=FallbackConfig(max_retries=1)
    )
    log_messages = []
    handler = logger.add(log_messages.append, format="{message}")
    try:
        response = await client.call_with_retry(
            [candidate()], original, model="", role="agent_team"
        )
        assert response.content == "main answer"
        assert provider.requests[-1].messages == messages_from_legacy(original)
        assert response.meta.compressed is False
        logs = "\n".join(str(message) for message in log_messages)
        assert "Agent compaction audit persistence failed" in logs
        assert "PRIVATE_DSN" not in logs
        assert '"outcome": "applied"' not in logs
    finally:
        logger.remove(handler)
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
async def test_error_resolution_requires_same_normalized_tool_and_arguments(provider):
    original = history()
    original[3:5] = tool_batch(
        "failed",
        args={"path": "broken.py", "start_line": 2},
        output={"error": "UNRESOLVED_FAILURE"},
    )
    original.extend(tool_batch("other", args={"path": "different.py", "start_line": 2}))
    sink = AsyncMock()
    unresolved = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert "UNRESOLVED_FAILURE" in json.dumps(unresolved)
    original.extend(tool_batch("retry", args={"start_line": 2, "path": "broken.py"}))
    resolved = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert "UNRESOLVED_FAILURE" not in json.dumps(resolved)
    assert sink.await_args.args[0]["retained"]["unresolved_errors"]["required"] == 0


@pytest.mark.asyncio
async def test_latest_parallel_tool_batch_and_interleaved_guidance_are_indivisible(
    provider,
):
    original = history()
    batch = tool_batch("one")
    other = tool_batch("two")
    batch[0]["tool_calls"].extend(other[0]["tool_calls"])
    guidance = {"role": "user", "content": "KEEP_GUIDANCE_ORDER", "guidance_ids": [42]}
    original.extend([batch[0], batch[1], guidance, other[1]])
    result = await bridge.compress_agent_team_messages(original, candidate=candidate())
    assert result[-4:] == [batch[0], batch[1], guidance, other[1]]


@pytest.mark.asyncio
async def test_rebinding_fresh_projection_cannot_resurrect_old_scope_or_skill_body(
    provider,
):
    original = history()
    original[-1]["content"] = json.dumps({"data": "OLD_SKILL_BODY"})
    compressor = make_compressor()
    try:
        first = await bridge.compress_agent_team_messages(
            original, candidate=candidate(), compressor=compressor
        )
        assert "OLD_SKILL_BODY" in json.dumps(first)
        current = history()
        current[-1]["content"] = '{"skills_disabled": true}'
        second = await bridge.compress_agent_team_messages(
            current, candidate=candidate(), compressor=compressor
        )
        assert "OLD_SKILL_BODY" not in json.dumps(second)
        assert "skills_disabled" in json.dumps(second)
        assert "OLD_SKILL_BODY" not in provider.requests[-1].messages[-1].content
    finally:
        await compressor.aclose()


@pytest.mark.asyncio
async def test_schema_token_estimates_are_explicit_and_use_supplied_schemas(provider):
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]
    original = history()
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink, tools=schemas
    )
    audit = sink.await_args.args[0]
    schema_tokens = estimate_unified_tools(_tools_from_legacy(schemas))
    assert schema_tokens > 0
    assert audit["estimated_tool_schema_tokens"] == schema_tokens
    assert audit["estimated_tokens_after"] == (
        estimate_unified_messages(messages_from_legacy(result)) + schema_tokens
    )


@pytest.mark.asyncio
async def test_audit_is_awaited_before_compressed_context_can_escape(provider):
    entered, release = asyncio.Event(), asyncio.Event()
    records = []

    async def persist(audit):
        entered.set()
        await release.wait()
        records.append(audit)

    task = asyncio.create_task(
        bridge.compress_agent_team_messages(
            history(), candidate=candidate(), audit_callback=persist
        )
    )
    await entered.wait()
    assert not task.done() and not records
    release.set()
    result = await task
    assert records[0]["outcome"] == "applied"
    assert_evidence(result)


@pytest.mark.asyncio
async def test_missing_tool_results_never_enter_compaction_or_audit(provider):
    source = history()[:-1]
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        source, candidate=candidate(), audit_callback=sink
    )
    assert result is source
    assert not provider.requests
    sink.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_without_primary_still_replaces_injected_runtime_evidence(provider):
    previous = history()
    previous[-1]["content"] = '{"data":"OBSOLETE_SCOPE"}'
    current = history()
    compressor = make_compressor()
    try:
        await bridge.compress_agent_team_messages(
            previous, candidate=candidate(), compressor=compressor
        )
        assert (
            await bridge.compress_agent_team_messages(
                current, candidate=None, compressor=compressor
            )
            is current
        )
        result = await compressor.compress_for_candidate(
            candidate=candidate(), messages=messages_from_legacy(current)
        )
        assert result is not None
        assert "OBSOLETE_SCOPE" not in json.dumps(bridge._from_unified_messages(result))
        assert_evidence(bridge._from_unified_messages(result))
    finally:
        await compressor.aclose()


@pytest.mark.asyncio
async def test_internal_fallback_retains_fresh_repository_snapshot_appended_after_bridge(
    provider,
):
    provider.fail_primary = True
    original = history()
    compressor = make_compressor()
    primary = candidate(window=64000)
    projected = await bridge.compress_agent_team_messages(
        original, candidate=primary, compressor=compressor
    )
    current_scope = {
        "role": "user",
        "content": "CURRENT_REPOSITORY_RULES: untrusted data",
    }
    client = UnifiedAIClient(
        compressor=compressor, fallback_config=FallbackConfig(max_retries=1)
    )
    try:
        await client.call_with_retry(
            [primary, candidate("agent-fallback")],
            [*projected, current_scope],
            model="",
            role="agent_team",
        )
        final_messages = bridge._from_unified_messages(provider.requests[-1].messages)
        assert current_scope in final_messages
        assert_evidence(final_messages)
    finally:
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
async def test_reused_compressor_observes_runtime_disable_and_threshold_changes(
    provider,
):
    original = history()
    sink = AsyncMock()
    compressor = make_compressor(audit_callback=sink)
    try:
        assert (
            await bridge.compress_agent_team_messages(
                original, candidate=candidate(), compressor=compressor
            )
            is not original
        )
        provider.settings.enable_context_compression = False
        assert (
            await bridge.compress_agent_team_messages(
                original, candidate=candidate(), compressor=compressor
            )
            is original
        )
        provider.settings.enable_context_compression = True
        provider.settings.context_compression_threshold = 0.95
        assert (
            await bridge.compress_agent_team_messages(
                original, candidate=candidate(), compressor=compressor
            )
            is original
        )
        sink.assert_awaited_once()
    finally:
        await compressor.aclose()


@pytest.mark.asyncio
async def test_duplicate_text_cannot_turn_human_guidance_into_a_runtime_notice(
    provider,
):
    original = history()
    guidance = original[-3]
    repeated = {
        "role": "user",
        "content": guidance["content"],
        "metadata": {"completion_reminder": True},
    }
    original.append(repeated)
    result = await bridge.compress_agent_team_messages(original, candidate=candidate())
    assert guidance in result
    assert repeated in result


def test_duplicate_guidance_audit_verifies_multiplicity_not_only_hash_membership():
    original = history()
    guidance = original[-3]
    original.append({**guidance, "metadata": {"guidance_ids": [32]}})
    evidence = CompactionEvidence(original)
    retained = evidence.audit_retention(messages_from_legacy([guidance]))
    assert retained["human_guidance"]["required"] == 2
    assert retained["human_guidance"]["verified"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,timed_out", [(1, False), (-9, False), (0, True)])
async def test_shell_process_failures_remain_until_same_command_succeeds(
    provider, returncode, timed_out
):
    original = history()
    # ShellTool reports a successfully executed process as ToolResult(True),
    # with the *process* outcome in this payload, even for failed tests.
    original[3:5] = tool_batch(
        "shell-failed",
        name="run_command",
        args={"command": "pytest"},
        output={
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout": "",
            "stderr": "UNRESOLVED_FAILURE: tests failed",
        },
    )
    sink = AsyncMock()
    result = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert "UNRESOLVED_FAILURE" in json.dumps(result)
    assert sink.await_args.args[0]["retained"]["unresolved_errors"]["required"] == 2
    original.extend(
        tool_batch(
            "shell-fixed",
            name="run_command",
            args={"command": "pytest"},
            output={
                "returncode": 0,
                "timed_out": False,
                "stdout": "passed",
                "stderr": "",
            },
        )
    )
    fixed = await bridge.compress_agent_team_messages(
        original, candidate=candidate(), audit_callback=sink
    )
    assert "UNRESOLVED_FAILURE" not in json.dumps(fixed)
