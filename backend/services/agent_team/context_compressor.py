"""Agent-only evidence retention and audit around the shared compressor."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from backend.core.ai_protocol.models import UnifiedMessage, UnifiedTool
from backend.core.ai_protocol.request_policy import (
    estimate_unified_messages,
    estimate_unified_tools,
    resolve_effective_request_policy,
)
from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.services.agent_team.compaction_evidence import (
    CompactionEvidence,
    history_sha256,
    is_runtime_notice,
    message_sha256,
)
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import (
    _tools_from_legacy,
    messages_from_legacy,
    messages_to_legacy,
)
from backend.utils.message_utils import has_missing_tool_results

AuditCallback = Callable[[dict[str, Any]], Awaitable[None]]


class CompactionAuditError(RuntimeError):
    """Safe error text for callers whose retry logs include str(exception)."""


class AgentContextCompressor(UnifiedContextCompressor):
    """Inject into one Agent's AIApiClient, including fallback/overflow recovery.

    Model selection, compression thresholds and provider request policy stay in
    the shared implementation. This override is only reached for a real summary
    attempt, so routine no-compaction requests produce no audit event.
    """

    def __init__(
        self,
        *,
        audit_callback: AuditCallback | None = None,
        audit_context: dict[str, Any] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.audit_callback: AuditCallback | None = audit_callback
        self.audit_context = {
            key: value
            for key, value in (audit_context or {}).items()
            if key in {"task_id", "session_id"} and type(value) is int
        }
        self._source_messages: list[dict[str, Any]] | None = None
        self._generated_summaries: set[str] = set()
        self._settings_backed = False

    @classmethod
    def from_settings(
        cls,
        *,
        audit_callback: AuditCallback | None = None,
        audit_context: dict[str, Any] | None = None,
        usage_callback: Callable[[Any], Awaitable[None]] | None = None,
    ) -> AgentContextCompressor:
        settings = get_settings()
        compressor = cls(
            threshold=float(settings.context_compression_threshold),
            enabled=settings.enable_context_compression,
            audit_callback=audit_callback,
            audit_context=audit_context,
            usage_callback=usage_callback,
        )
        compressor._settings_backed = True
        return compressor

    def bind_source_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace evidence with this request's fresh runtime projection.

        Call for every model round, after repository/Skill projection. Never
        bind restored raw checkpoints or share this instance between Agents.
        """
        self._source_messages = [dict(message) for message in messages]
        self._generated_summaries.clear()
        if self._settings_backed:
            settings = get_settings()
            self.enabled = bool(settings.enable_context_compression)
            self.threshold = float(settings.context_compression_threshold)

    def _current_evidence(self, messages: list[UnifiedMessage]) -> CompactionEvidence:
        """Recover runtime labels; take actual evidence from the current input.

        The inner client drops legacy metadata. Match original messages by their
        complete protocol fingerprint, and retain fresh user context appended
        after the outer bridge (notably current repository rules). An earlier
        generated summary is replaceable data, never new human guidance.
        """
        originals = {}
        for original, unified in zip(
            self._source_messages or [],
            messages_from_legacy(self._source_messages or []),
        ):
            fingerprint = message_sha256(unified)
            previous = originals.get(fingerprint)
            # Identical text is insufficient to downgrade a genuine user
            # instruction into a generated notice. Preserve ambiguous copies.
            if previous is None or (
                is_runtime_notice(previous) and not is_runtime_notice(original)
            ):
                originals[fingerprint] = original
        current = messages_to_legacy(messages)
        for message, unified in zip(current, messages):
            fingerprint = message_sha256(unified)
            if original := originals.get(fingerprint):
                for name in ("metadata", "guidance_ids", "prompt_ids"):
                    if name in original:
                        message[name] = original[name]
            elif fingerprint in self._generated_summaries:
                message["metadata"] = {"context_compaction": True}
            elif self._source_messages is not None and message["role"] == "user":
                message["metadata"] = {"compaction_current_context": True}
        return CompactionEvidence(current)

    async def _record_audit(self, audit: dict[str, Any]) -> None:
        if self.audit_callback is not None:
            try:
                await self.audit_callback(audit)
            except Exception as exc:
                logger.error(
                    "Agent compaction audit persistence failed ({})",
                    type(exc).__name__,
                )
                raise CompactionAuditError(
                    "Agent compaction audit persistence failed"
                ) from None
        # The default sink remains observable for callers without checkpoints.
        # Log success only after the durable sink has returned successfully.
        logger.info("Agent context compaction audit: {}", json.dumps(audit))

    async def _summarize(
        self,
        candidate,
        messages: list[UnifiedMessage],
        *,
        system: str | None = None,
        tracker=None,
        final_output_tokens: int | None = None,
        safety_reserve_tokens: int | None = None,
        tools: list[UnifiedTool] | None = None,
    ) -> list[UnifiedMessage] | None:
        evidence = self._current_evidence(messages)
        try:
            result = await super()._summarize(
                candidate,
                messages,
                system=system,
                tracker=tracker,
                final_output_tokens=final_output_tokens,
                safety_reserve_tokens=safety_reserve_tokens,
                tools=tools,
            )
        except asyncio.CancelledError:
            logger.info("Agent context compaction cancelled before replacement")
            raise
        except Exception as exc:
            logger.warning("Agent context compaction failed ({})", type(exc).__name__)
            raise

        summary = next((m for m in result or [] if m.role != "system"), None)
        reason = "summary_unavailable"
        output = messages
        if summary is not None:
            if summary.role != "user" or summary.tool_calls or summary.tool_call_id:
                reason = "invalid_summary_message"
            else:
                output = evidence.restore(summary)
                if system and not any(
                    m.role == "system" and m.content == system for m in output
                ):
                    output = [UnifiedMessage(role="system", content=system), *output]
                policy = resolve_effective_request_policy(
                    candidate,
                    output,
                    role="agent_team",
                    max_tokens=final_output_tokens,
                    safety_reserve_tokens=safety_reserve_tokens,
                    tools=tools,
                    clamp_to_context=False,
                )
                if not policy.fits_context:
                    reason = "retained_context_exceeds_window"
                elif estimate_unified_messages(output) >= estimate_unified_messages(
                    messages
                ):
                    reason = "no_token_reduction"
                else:
                    reason = ""

        applied = not reason
        if not applied:
            output = messages
        tool_tokens = estimate_unified_tools(tools)
        audit = {
            "version": 1,
            "event_type": "context_compaction",
            "created_at": now_utc().isoformat(),
            **self.audit_context,
            "outcome": "applied" if applied else "not_applied",
            "reason": reason,
            "model_id": candidate.model.model_id,
            "context_window_tokens": self._context_window_tokens(candidate),
            "estimated_tokens_before": estimate_unified_messages(messages)
            + tool_tokens,
            "estimated_tokens_after": estimate_unified_messages(output) + tool_tokens,
            "estimated_tool_schema_tokens": tool_tokens,
            "message_count_before": len(messages),
            "message_count_after": len(output),
            "source_sha256": history_sha256(evidence.messages),
            "input_sha256": history_sha256(messages),
            "output_sha256": history_sha256(output),
            "retained": evidence.audit_retention(output),
        }
        await self._record_audit(audit)
        if applied and summary is not None:
            self._generated_summaries.add(message_sha256(summary))
        return output if applied else None


async def compress_agent_team_messages(
    messages: list[dict[str, Any]],
    *,
    candidate: Any,
    token_tracker: Any | None = None,
    audit_callback: AuditCallback | None = None,
    audit_context: dict[str, Any] | None = None,
    compressor: AgentContextCompressor | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """使用 UnifiedContextCompressor 压缩 Agent Team 消息，返回 dict 列表。"""
    if compressor is not None:
        # Even a skipped outer attempt precedes possible fallback/overflow
        # compression. Its evidence must come from this round's fresh scope.
        compressor.bind_source_messages(messages)
    if not candidate or not messages or has_missing_tool_results(messages):
        return messages

    # 每次从 Settings 现取配置（支持运行时刷新），用完即释放惰性 HTTP 客户端。
    owned = compressor is None
    compressor = compressor or AgentContextCompressor.from_settings(
        audit_callback=audit_callback, audit_context=audit_context
    )
    if owned:
        compressor.bind_source_messages(messages)
    try:
        compressed, result = await compressor.maybe_compress(
            candidate,
            messages_from_legacy(messages),
            tracker=token_tracker,
            tools=_tools_from_legacy(tools),
        )
    finally:
        if owned:
            await compressor.aclose()
    if not compressed:
        return messages
    # Conversion must not erase runtime guidance metadata or other original
    # fields. Those originals are not rewritten or added to the audit payload.
    originals: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for original, unified in zip(messages, messages_from_legacy(messages)):
        originals[message_sha256(unified)].append(original)
    restored = []
    for message in result:
        matches = originals[message_sha256(message)]
        restored.append(
            dict(matches.popleft()) if matches else _from_unified_messages([message])[0]
        )
    return restored


def _from_unified_messages(messages: list[UnifiedMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        d: dict[str, Any] = {"role": msg.role}
        if msg.content is not None:
            d["content"] = msg.content
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        result.append(d)
    return result


__all__ = ["AgentContextCompressor", "compress_agent_team_messages"]
