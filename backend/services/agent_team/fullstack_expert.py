"""Sakura Agent using controlled tool calls.

The historical module and class names remain import-compatible for callers
that have not migrated yet. User-visible identity and runtime role values
are intentionally expressed as ``agent``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from backend.services.agent_team.ai_client import create_agent_team_client
from backend.services.agent_team.context_compressor import compress_agent_team_messages
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)
from backend.services.agent_team.execution import ExecutionRunner
from backend.services.agent_team.prompt_config import (
    IMPLEMENTATION_SYSTEM_PROMPT,
    build_implementation_user_message,
)
from backend.services.agent_team.repository_context import (
    RepositoryContext,
    RepositoryContextError,
    get_repository_limits,
    parse_allowed_tools,
)
from backend.services.agent_team.runtime_limits import get_runtime_limits
from backend.services.agent_team.tool_scheduler import run_tool_batch
from backend.services.agent_team.tools.base import ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.registry import (
    create_executor,
    get_tool_definitions_fresh,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.utils.message_utils import (
    get_missing_tool_calls,
    has_missing_tool_results,
    serialize_tool_result,
    tool_call_to_dict,
)

# Historical module imports expose the same production prompt under the old
# name while the source of truth lives in ``prompt_config``.
FULLSTACK_SYSTEM_PROMPT = IMPLEMENTATION_SYSTEM_PROMPT


@dataclass
class FullStackResult:
    """Agent execution result."""

    success: bool
    summary: str
    modified_files: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    test_result: str = ""
    tool_calls_count: int = 0
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def outcome(self) -> str:
        if self.success:
            return "success"
        if self.error == "cancelled":
            return "cancelled"
        if self.error in {
            "no_progress",
            "model_round_limit",
            "tool_call_limit",
            "reconciliation_required",
            "checkpoint_inconsistent",
            "guidance_admission_failed",
        }:
            return "blocked"
        return "unrecoverable_error"


def _get_missing_tool_calls(messages: list[dict[str, Any]]) -> list[Any]:
    """返回缺少结果消息的工具调用。"""
    return get_missing_tool_calls(messages)


class _NoProgressTracker:
    """Incrementally detect repeated observable work, including across resume.

    Call IDs do not constitute progress. New canonical tool/argument/result
    evidence (including distinct errors) or new admitted user context resets
    consecutive stalled rounds and the text reminder. This is deterministic
    evidence comparison, not a claim that every changed output is useful.
    """

    def __init__(self):
        self.cursor = 0
        self.seen: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.stalled_rounds = 0
        self.reminded = False

    @staticmethod
    def _json(value: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _admit_evidence(self, value: Any) -> bool:
        digest = self._fingerprint(value)
        if digest in self.seen:
            return False
        self.seen.add(digest)
        return True

    def _reset(self) -> None:
        self.stalled_rounds = 0
        self.reminded = False

    def update(self, messages: list[dict[str, Any]]) -> None:
        while self.cursor < len(messages):
            message = messages[self.cursor]
            self.cursor += 1
            if message.get("role") == "user":
                if message.get("metadata", {}).get("completion_reminder"):
                    self.reminded = True
                elif message.get("content") and self._admit_evidence(
                    {
                        "context": message["content"],
                        "guidance_ids": message.get("metadata", {}).get(
                            "guidance_ids", message.get("guidance_ids")
                        ),
                    }
                ):
                    self._reset()
            elif message.get("role") == "assistant" and message.get("tool_calls"):
                self.pending = {call["id"]: call for call in message["tool_calls"]}
                self.results = {}
            elif (
                message.get("role") == "tool"
                and message.get("tool_call_id") in self.pending
            ):
                self.results[message["tool_call_id"]] = message
                if len(self.results) != len(self.pending):
                    continue
                novel = False
                for ident, call in self.pending.items():
                    function = call.get("function") or {}
                    evidence = {
                        "tool": function.get("name"),
                        "arguments": self._json(function.get("arguments")),
                        "result": self._json(self.results[ident].get("content")),
                    }
                    novel = self._admit_evidence(evidence) or novel
                if novel:
                    self._reset()
                else:
                    self.stalled_rounds += 1
                self.pending = {}
                self.results = {}


def _guidance_items(guidance: Any) -> list[Any]:
    """Flatten one callback result without changing guidance body text."""
    if guidance is None:
        return []
    if isinstance(guidance, (list, tuple)):
        items: list[Any] = []
        for item in guidance:
            items.extend(_guidance_items(item))
        return items
    if isinstance(guidance, dict):
        return [guidance]
    queued_items = getattr(guidance, "items", ())
    if queued_items:
        metadata = getattr(guidance, "metadata", None)
        return [
            {
                "content": content,
                "prompt_ids": (prompt_id,),
                "metadata": metadata,
            }
            for prompt_id, content in queued_items
        ]
    return [guidance]


def _normalize_guidance_item(
    guidance: Any,
) -> tuple[str, tuple[int, ...], dict[str, Any]]:
    """Extract raw guidance content and keep audit data out of the body."""
    audit_fields = ("author", "source", "audit_id", "created_at")
    if isinstance(guidance, dict):
        content = guidance.get("content", "")
        raw_ids = guidance.get("prompt_ids") or guidance.get("guidance_ids") or ()
        raw_metadata = dict(guidance.get("metadata") or {})
        for field_name in audit_fields:
            if field_name in guidance:
                raw_metadata.setdefault(field_name, guidance[field_name])
    else:
        content = getattr(guidance, "content", guidance)
        raw_ids = getattr(guidance, "prompt_ids", ())
        raw_metadata = dict(getattr(guidance, "metadata", {}) or {})
        for field_name in audit_fields:
            field_value = getattr(guidance, field_name, None)
            if field_value is not None:
                raw_metadata.setdefault(field_name, field_value)

    try:
        guidance_ids = tuple(int(item) for item in raw_ids)
    except TypeError, ValueError:
        guidance_ids = ()
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    if guidance_ids:
        metadata.setdefault("guidance_ids", list(guidance_ids))
    return str(content), guidance_ids, metadata


class FullStackExpertAgent:
    """Compatibility class for the single Agent."""

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
        checkpoint: ConversationCheckpointService | None = None,
        session_id: int | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        execution_runner: ExecutionRunner | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.tool_executor = create_executor("agent")
        self.file_state = ReadFileState()
        self.checkpoint = checkpoint
        self.session_id = session_id
        self.restored_messages = initial_messages is not None
        self.execution_runner = execution_runner
        self._cancel_event: asyncio.Event | None = None
        self.messages: list[dict[str, Any]] = (
            [dict(message) for message in initial_messages]
            if initial_messages is not None
            else [{"role": "system", "content": FULLSTACK_SYSTEM_PROMPT}]
        )

    async def _append_message(self, message: dict[str, Any]) -> int | None:
        message_id = None
        if self.checkpoint and self.session_id:
            message_id = await self.checkpoint.append_message(self.session_id, message)
        self.messages.append(message)
        return message_id

    async def _ensure_system_checkpoint(self) -> None:
        if not self.checkpoint or not self.session_id or not self.messages:
            return
        if len(self.messages) == 1 and self.messages[0].get("role") == "system":
            await self.checkpoint.append_message(self.session_id, self.messages[0])

    @staticmethod
    def _is_guidance_message(message: dict[str, Any]) -> bool:
        """Identify runtime guidance without inspecting or rewriting its body."""
        if message.get("role") != "user":
            return False
        if message.get("guidance_ids") or message.get("prompt_ids"):
            return True
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and (
            metadata.get("guidance_ids") or metadata.get("prompt_ids")
        ):
            return True
        return str(message.get("content") or "").startswith("[human_guidance:")

    def _prepare_restored_messages(
        self,
        *,
        task_title: str,
        task_summary: str,
        source_type: str,
        source_issue_number: int | None,
        sakura_memory: str,
        skills_summary: str,
        feedback: str,
        handoff_context: str,
        role_memory_context: str,
        execution_expectations: str,
        reference_context: str = "",
    ) -> None:
        """Migrate legacy history while preserving runtime guidance verbatim."""
        if not self.restored_messages:
            return

        for message in self.messages:
            if message.get("role") == "system":
                message["content"] = FULLSTACK_SYSTEM_PROMPT
                break
        else:
            self.messages.insert(
                0,
                {"role": "system", "content": FULLSTACK_SYSTEM_PROMPT},
            )

        initial_user_index = next(
            (
                index
                for index, message in enumerate(self.messages)
                if message.get("role") == "user"
                and not self._is_guidance_message(message)
                and not message.get("metadata", {}).get("completion_reminder")
            ),
            None,
        )
        rebuilt = self._build_user_message(
            task_title=task_title,
            task_summary=task_summary,
            source_type=source_type,
            source_issue_number=source_issue_number,
            sakura_memory=sakura_memory,
            skills_summary=skills_summary,
            reference_context=reference_context,
            feedback=feedback,
            handoff_context=handoff_context,
            role_memory_context=role_memory_context,
            execution_expectations=execution_expectations,
        )
        if initial_user_index is None:
            self.messages.insert(1, {"role": "user", "content": rebuilt})
            return

        existing = str(self.messages[initial_user_index].get("content") or "")
        if existing == rebuilt:
            return
        replacement = dict(self.messages[initial_user_index])
        replacement["content"] = rebuilt
        replacement.pop("metadata", None)
        replacement.pop("guidance_ids", None)
        replacement.pop("prompt_ids", None)
        self.messages[initial_user_index] = replacement

    def _build_context(
        self, skills_context: dict[str, Any] | None = None
    ) -> ToolContext:
        extra: dict[str, Any] = {"file_state": self.file_state}
        if skills_context:
            extra.update(skills_context)
        return ToolContext(
            workspace=str(self.workspace),
            workspace_service=self.workspace_service,
            execution_runner=self.execution_runner,
            cancel_event=self._cancel_event,
            read_file_state={},
            extra=extra,
        )

    async def execute(self, *args: Any, **kwargs: Any) -> FullStackResult:
        try:
            return await self._execute(*args, **kwargs)
        except asyncio.CancelledError:
            ctx = getattr(self, "_active_context", None)
            return FullStackResult(
                success=False,
                summary="任务已取消",
                error="cancelled",
                modified_files=sorted(ctx.modified_files) if ctx else [],
                tool_calls_count=sum(m.get("role") == "tool" for m in self.messages),
            )
        finally:
            ctx = getattr(self, "_active_context", None)
            if ctx:
                ctx.active_skill_tools.clear()

    async def _execute(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        sakura_memory: str = "",
        skills_summary: str = "",
        skills_context: dict[str, Any] | None = None,
        reference_context: str = "",
        feedback: str = "",
        handoff_context: str = "",
        role_memory_context: str = "",
        execution_expectations: str = "",
        iteration: int = 1,
        cancel_check: Callable[[], bool] | None = None,
        guidance_callback: Callable[[], Any] | None = None,
        guidance_ack_callback: Callable[[tuple[int, ...]], Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> FullStackResult:
        """Run the Agent until completion or cancellation."""
        self._cancel_event = cancel_event
        ctx = self._build_context(skills_context)
        self._active_context = ctx
        ctx.repository_context = RepositoryContext(self.workspace, await get_repository_limits())
        repo_index = await asyncio.to_thread(ctx.repository_context.discover_skills)
        # Administrator-installed/enabled Skills retain slug precedence.
        ctx.extra["skills_index"] = {**repo_index, **ctx.extra.get("skills_index", {})}
        if self.restored_messages:
            restored = await self._recover(ctx)
            if restored is not None:
                return restored
            self._restore_skill_workflows(ctx)
        limits = await get_runtime_limits()
        ctx.max_parallel_reads = limits["max_parallel_reads"]
        client, config = await create_agent_team_client()
        candidate = await client.resolve_role_primary_candidate(config.agent_role)
        context_window_tokens = (
            candidate.model.context_window_tokens if candidate else None
        )
        tool_schemas = await get_tool_definitions_fresh("agent")
        self._prepare_restored_messages(
            task_title=task_title,
            task_summary=task_summary,
            source_type=source_type,
            source_issue_number=source_issue_number,
            sakura_memory=sakura_memory,
            skills_summary=skills_summary,
            reference_context=reference_context,
            feedback=feedback,
            handoff_context=handoff_context,
            role_memory_context=role_memory_context,
            execution_expectations=execution_expectations,
        )
        await self._ensure_system_checkpoint()
        if not self.restored_messages and not has_missing_tool_results(self.messages):
            await self._append_message(
                {
                    "role": "user",
                    "content": self._build_user_message(
                        task_title=task_title,
                        task_summary=task_summary,
                        source_type=source_type,
                        source_issue_number=source_issue_number,
                        sakura_memory=sakura_memory,
                        skills_summary=skills_summary,
                        reference_context=reference_context,
                        feedback=feedback,
                        handoff_context=handoff_context,
                        role_memory_context=role_memory_context,
                        execution_expectations=execution_expectations,
                    ),
                }
            )

        root_docs = await asyncio.to_thread(ctx.repository_context.instructions_for)
        ctx.repository_instructions.update({doc.path: doc for doc in root_docs})
        repository_message = self._repository_message(ctx)
        if repository_message is not None and not has_missing_tool_results(
            self.messages
        ):
            await self._append_message(repository_message)

        tool_calls_count = sum(m.get("role") == "tool" for m in self.messages)
        token_tracker = TokenTracker()
        round_num = 0
        model_rounds = sum(m.get("role") == "assistant" for m in self.messages)
        progress = _NoProgressTracker()
        progress.update(self.messages)

        def blocked(reason: str) -> FullStackResult:
            return FullStackResult(
                success=False,
                summary="Agent 执行受阻",
                error=reason,
                modified_files=sorted(ctx.modified_files),
                tool_calls_count=tool_calls_count,
                prompt_tokens=token_tracker.prompt_tokens,
                completion_tokens=token_tracker.completion_tokens,
            )

        while True:
            round_num += 1
            if (cancel_check and cancel_check()) or (
                cancel_event and cancel_event.is_set()
            ):
                return FullStackResult(
                    success=False,
                    summary="任务已取消",
                    modified_files=sorted(ctx.modified_files),
                    error="cancelled",
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )
            logger.debug("Agent tool call round {}", round_num)

            pending_tool_calls = _get_missing_tool_calls(self.messages)
            if pending_tool_calls:
                if (
                    limits["max_tool_calls"]
                    and tool_calls_count + len(pending_tool_calls)
                    > limits["max_tool_calls"]
                ):
                    return blocked("tool_call_limit")
                terminal_output = await self._execute_tool_calls(
                    pending_tool_calls,
                    ctx,
                    round_num,
                )
                tool_calls_count += len(pending_tool_calls)
                if terminal_output is not None:
                    ai_files = terminal_output.get("modified_files", [])
                    if isinstance(ai_files, list):
                        merged = set(ai_files) | ctx.modified_files
                    else:
                        merged = ctx.modified_files
                    return FullStackResult(
                        success=True,
                        summary=terminal_output.get("summary", ""),
                        modified_files=sorted(merged),
                        risk_level=terminal_output.get("risk_level", "medium"),
                        test_result=terminal_output.get("test_result", ""),
                        tool_calls_count=tool_calls_count,
                        prompt_tokens=token_tracker.prompt_tokens,
                        completion_tokens=token_tracker.completion_tokens,
                    )
                continue

            if (
                limits["max_model_rounds"]
                and model_rounds >= limits["max_model_rounds"]
            ):
                return blocked("model_round_limit")

            # 消费新的管理员指导
            if guidance_callback:
                try:
                    guidance = await guidance_callback()
                    for guidance_item in _guidance_items(guidance):
                        guidance_text, guidance_ids, guidance_metadata = (
                            _normalize_guidance_item(guidance_item)
                        )
                        if not guidance_text and not guidance_ids:
                            continue
                        # The body is the submitted user content verbatim.
                        # Stable IDs, authorship, source, and audit fields belong
                        # in metadata/event state only.
                        guidance_message: dict[str, Any] = {
                            "role": "user",
                            "content": guidance_text,
                        }
                        if guidance_metadata:
                            guidance_message["metadata"] = guidance_metadata
                        if (
                            guidance_ids
                            and self.checkpoint
                            and self.session_id
                            and hasattr(self.checkpoint, "append_guidance_message")
                        ):
                            await self.checkpoint.append_guidance_message(
                                self.session_id,
                                guidance_message,
                                guidance_ids,
                            )
                            self.messages.append(guidance_message)
                        else:
                            await self._append_message(guidance_message)
                            if guidance_ids and guidance_ack_callback:
                                await guidance_ack_callback(guidance_ids)
                except Exception as exc:
                    # Guidance is an explicit user control and must be
                    # checkpointed/acknowledged before the next model call.
                    # Continuing after an admission failure would let the
                    # Agent act on stale instructions while leaving a pending
                    # prompt ambiguous.  Return a terminal, retryable result
                    # instead; the worker keeps the queue row pending.
                    logger.error(
                        "Agent guidance admission failed; stopping before model call: {}",
                        exc,
                    )
                    return FullStackResult(
                        success=False,
                        summary="管理员指导未能安全注入，已停止模型调用",
                        modified_files=sorted(ctx.modified_files),
                        error="guidance_admission_failed",
                        prompt_tokens=token_tracker.prompt_tokens,
                        completion_tokens=token_tracker.completion_tokens,
                    )

            progress.update(self.messages)
            if progress.stalled_rounds >= limits["max_no_progress_rounds"]:
                return blocked("no_progress")

            model_messages = await compress_agent_team_messages(
                self.messages, candidate=candidate, token_tracker=token_tracker
            )
            # Compression and legacy histories may omit repository messages.
            # Reinforce current data as a user turn, never system authority.
            repository_message = self._repository_message(ctx)
            if repository_message is not None:
                model_messages = [*model_messages, repository_message]
            await _publish_ai_request(
                "agent",
                round_num,
                task_id=self.checkpoint.task_id if self.checkpoint else None,
                session_id=self.session_id,
            )
            response = await client.call_with_retry(
                messages=model_messages,
                model="",
                tools=tool_schemas,
                tool_choice="auto",
                role="agent_team",
                cancel_event=cancel_event,
            )
            model_rounds += 1
            token_tracker.accumulate(response)
            token_tracker.log_context_usage(
                response,
                context_window_tokens,
                round_num,
            )

            if not response.choices:
                return FullStackResult(
                    success=False,
                    summary="AI 返回空响应",
                    modified_files=sorted(ctx.modified_files),
                    error="empty_response",
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )

            choice = response.choices[0]
            message = choice.message

            # 构建助手消息
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if message.content:
                assistant_msg["content"] = message.content
            if message.tool_calls:
                existing_ids = {
                    call.get("id")
                    for previous in self.messages
                    for call in previous.get("tool_calls") or []
                }
                new_ids = [call.id for call in message.tool_calls]
                if any(not ident or ident in existing_ids for ident in new_ids) or len(
                    set(new_ids)
                ) != len(new_ids):
                    return blocked("checkpoint_inconsistent")
                assistant_msg["tool_calls"] = [
                    tool_call_to_dict(tc) for tc in message.tool_calls
                ]
            await self._append_message(assistant_msg)

            # Text never completes a run. One durable reminder is allowed.
            if not message.tool_calls:
                if progress.reminded:
                    return blocked("no_progress")
                await self._append_message(
                    {
                        "role": "user",
                        "content": "Continue using tools. A text response does not complete this task. When the work and verification are complete, call finish_task with the summary and test evidence.",
                        "metadata": {"completion_reminder": True},
                    }
                )
                progress.update(self.messages)
                continue

            if (
                limits["max_tool_calls"]
                and tool_calls_count + len(message.tool_calls) > limits["max_tool_calls"]
            ):
                return blocked("tool_call_limit")

            # 逐个执行工具调用
            terminal_output = await self._execute_tool_calls(
                message.tool_calls,
                ctx,
                round_num,
            )
            tool_calls_count += len(message.tool_calls)

            if terminal_output is not None:
                ai_files = terminal_output.get("modified_files", [])
                if isinstance(ai_files, list):
                    merged = set(ai_files) | ctx.modified_files
                else:
                    merged = ctx.modified_files
                return FullStackResult(
                    success=True,
                    summary=terminal_output.get("summary", ""),
                    modified_files=sorted(merged),
                    risk_level=terminal_output.get("risk_level", "medium"),
                    test_result=terminal_output.get("test_result", ""),
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )

    async def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        ctx: ToolContext,
        round_num: int,
    ) -> dict[str, Any] | None:
        async def before(tool_call: Any) -> None:
            logger.info("Agent tool: {} (round={})", tool_call.function.name, round_num)
            if self.checkpoint and self.session_id:
                await self.checkpoint.mark_tool_call_running(
                    self.session_id, tool_call.id
                )

        async def after(tool_call: Any, result: ToolResult, status: str) -> None:
            message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": serialize_tool_result(result),
            }
            if self.checkpoint and self.session_id:
                await self.checkpoint.record_tool_result(
                    self.session_id, tool_call.id, message, status, result.error
                )
            self.messages.append(message)

        async def cancelled(tool_call: Any) -> None:
            if self.checkpoint and self.session_id:
                await self.checkpoint.mark_tool_call_cancelled(
                    self.session_id, tool_call.id
                )

        if ctx.repository_context:
            try:
                docs = await self.tool_executor.repository_requirements(tool_calls, ctx)
            except RepositoryContextError as exc:
                for tool_call in tool_calls:
                    await after(tool_call, ToolResult(False, error=str(exc), error_code="REPOSITORY_CONTEXT_REJECTED"), "failed")
                return None
            if docs:
                for tool_call in tool_calls:
                    await after(tool_call, ToolResult(False, error="New repository scope instructions delivered; retry this batch after applying them", error_code="REPOSITORY_CONTEXT_REQUIRED"), "failed")
                ctx.repository_instructions.update({doc.path: doc for doc in docs})
                repository_message = self._repository_message(ctx)
                if repository_message is not None:
                    await self._append_message(repository_message)
                return None

        return await run_tool_batch(
            tool_calls,
            self.tool_executor,
            ctx,
            before=before,
            after=after,
            cancelled=cancelled,
        )

    @staticmethod
    def _repository_message(ctx: ToolContext) -> dict[str, Any] | None:
        repository = ctx.repository_context
        index = {slug: entry for slug, entry in ctx.extra.get("skills_index", {}).items() if entry.get("source_type") == "repository"}
        if repository is None or not (
            ctx.repository_instructions
            or index
            or repository.diagnostics
            or ctx.active_skill_tools
        ):
            return None
        content = repository.render(list(ctx.repository_instructions.values()))
        content += "\nRepository Skills metadata (untrusted; use_skill loads bodies):\n" + repository.skills_summary(index)
        content += "\nActive Skill workflow tool restrictions: " + json.dumps({slug: sorted(tools) for slug, tools in ctx.active_skill_tools.items()})
        content += "\nUse use_skill with slug and end_skill=true when that workflow ends; this restores only prior runtime access."
        return {"role": "user", "content": content, "metadata": {"repository_context": True}}

    def _restore_skill_workflows(self, ctx: ToolContext) -> None:
        """Derive runtime scope from verified calls and current registered metadata.

        Never trust an allowed_tools field supplied in a stored tool result.
        Recovery ledger consistency is verified before this method is called.
        """
        calls = {}
        for message in self.messages:
            for call in message.get("tool_calls") or []:
                calls[call["id"]] = call
            if message.get("role") != "tool":
                continue
            call = calls.get(message.get("tool_call_id"), {})
            fn = call.get("function", {})
            if fn.get("name") != "use_skill":
                continue
            try:
                output = json.loads(message.get("content") or "{}")
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError, TypeError:
                continue
            if not isinstance(output, dict) or output.get("error") or not isinstance(args, dict):
                continue
            slug = str(args.get("slug") or "")
            if args.get("end_skill") is True:
                ctx.active_skill_tools.pop(slug, None)
            elif not args.get("list_files"):
                entry = ctx.extra.get("skills_index", {}).get(slug)
                if entry is None:
                    # A removed active Skill cannot silently restore access.
                    ctx.active_skill_tools[slug] = frozenset()
                    continue
                try:
                    allowed = parse_allowed_tools(entry.get("allowed_tools"))
                except RepositoryContextError:
                    allowed = frozenset()
                if allowed is not None:
                    prior = ctx.active_skill_tools.get(slug)
                    ctx.active_skill_tools[slug] = allowed if prior is None else prior & allowed

    async def _recover(self, ctx: ToolContext) -> FullStackResult | None:
        """Validate a durable prefix before replay; never guess a mutation result.

        Pending calls were not admitted and may execute. Interrupted read-only
        calls may retry. Running/failed/cancelled mutations without a committed
        result require workspace evidence reconciliation before replay, not a
        permission grant. Completed calls with missing results are corruption,
        not a reason to repeat the action. Legacy ambiguous histories fail closed.
        """
        states = {}
        session_result = None
        if (
            self.checkpoint
            and self.session_id
            and hasattr(self.checkpoint, "load_tool_call_states")
        ):
            states = await self.checkpoint.load_tool_call_states(self.session_id)
        if (
            self.checkpoint
            and self.session_id
            and hasattr(self.checkpoint, "load_session_result")
        ):
            session_result = await self.checkpoint.load_session_result(self.session_id)
        calls = {}
        results = {}
        invalid = False
        for message in self.messages:
            for call in message.get("tool_calls") or []:
                ident = call.get("id")
                if not ident or ident in calls:
                    invalid = True
                calls[ident] = call
            if message.get("role") == "tool":
                ident = message.get("tool_call_id")
                if ident not in calls or ident in results:
                    invalid = True
                try:
                    payload = json.loads(message.get("content") or "{}")
                except ValueError, TypeError:
                    payload = {}
                results[ident] = payload if isinstance(payload, dict) else {}
        terminal = None
        unresolved_before_finish = False
        for ident, call in calls.items():
            fn = call.get("function") or {}
            name = fn.get("name", "")
            state = states.get(ident, {})
            status = state.get("status")
            if state.get("name", name) != name:
                invalid = True
            arguments = fn.get("arguments", "")
            if (
                state.get("arguments_hash")
                and state["arguments_hash"]
                != hashlib.sha256(arguments.encode("utf-8")).hexdigest()
            ):
                invalid = True
            payload = results.get(ident)
            if payload is not None:
                if terminal is not None and not (
                    status == "cancelled"
                    and payload.get("error_code") == "CANCELLED_AFTER_FINISH"
                ):
                    invalid = True
                if (
                    name == "finish_task"
                    and status == "completed"
                    and not payload.get("_terminal")
                ):
                    invalid = True
                if status == "completed" and "error" in payload:
                    invalid = True
                if status in {"running", "pending"}:
                    invalid = True
                if payload.get("_terminal"):
                    if (
                        name != "finish_task"
                        or status != "completed"
                        or "error" in payload
                    ):
                        invalid = True
                    else:
                        from backend.services.agent_team.tools.finish_task_tool import (
                            FinishTaskTool,
                        )

                        tool = self.tool_executor.get_tool(name)
                        try:
                            finish_args = json.loads(arguments)
                        except ValueError, TypeError:
                            finish_args = None
                        if (
                            type(tool) is not FinishTaskTool
                            or tool.validate_input(payload, ctx)
                            or not isinstance(finish_args, dict)
                            or tool.validate_input(finish_args, ctx)
                            or any(
                                payload.get(key, default)
                                != finish_args.get(key, default)
                                for key, default in (
                                    ("summary", ""),
                                    ("modified_files", []),
                                    ("risk_level", "medium"),
                                    ("test_result", ""),
                                )
                            )
                        ):
                            invalid = True
                        else:
                            terminal = payload
                            if unresolved_before_finish:
                                invalid = True
                modified = payload.get("_modified_file")
                if isinstance(modified, str):
                    ctx.track_modified_file(modified)
                continue
            if terminal is None:
                unresolved_before_finish = True
            elif status != "pending":
                # Only calls that were never admitted can trail a completed
                # finish. An interrupted action here violates the barrier.
                invalid = True
            if status == "completed":
                invalid = True
            elif (
                terminal is None
                and status != "pending"
                and not self.tool_executor.metadata(name).read_only
            ):
                return FullStackResult(
                    False,
                    "中断的写操作需要核对工作区后再恢复",
                    error="reconciliation_required",
                    modified_files=sorted(ctx.modified_files),
                )
        if session_result and session_result.get("success") and terminal is None:
            invalid = True
        if invalid:
            return FullStackResult(
                False,
                "检查点工具状态不一致，无法安全恢复",
                error="checkpoint_inconsistent",
            )
        if terminal is not None:
            # A crash after finish persistence can leave trailing unstarted
            # calls. Resolve them as cancelled without invoking any tool.
            for call in _get_missing_tool_calls(self.messages):
                result = ToolResult(
                    False,
                    error="Skipped after successful finish_task",
                    error_code="CANCELLED_AFTER_FINISH",
                )
                message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": serialize_tool_result(result),
                }
                if self.checkpoint and self.session_id:
                    await self.checkpoint.record_tool_result(
                        self.session_id, call.id, message, "cancelled", result.error
                    )
                self.messages.append(message)
            return FullStackResult(
                True,
                terminal["summary"],
                modified_files=sorted(
                    set(terminal.get("modified_files", [])) | ctx.modified_files
                ),
                risk_level=terminal.get("risk_level", "medium"),
                test_result=terminal.get("test_result", ""),
                tool_calls_count=len(results),
            )
        return None

    def _build_user_message(
        self,
        task_title: str,
        task_summary: str,
        source_type: str,
        source_issue_number: int | None,
        sakura_memory: str,
        skills_summary: str,
        feedback: str,
        handoff_context: str = "",
        role_memory_context: str = "",
        execution_expectations: str = "",
        reference_context: str = "",
    ) -> str:
        return build_implementation_user_message(
            task_title=task_title,
            task_summary=task_summary,
            source_type=source_type,
            source_issue_number=source_issue_number,
            sakura_memory=sakura_memory,
            skills_summary=skills_summary,
            reference_context=reference_context,
            feedback=feedback,
            handoff_context=handoff_context,
            role_memory_context=role_memory_context,
            execution_expectations=execution_expectations,
        )


# Historical import aliases remain available while callers migrate.
build_fullstack_user_message = build_implementation_user_message
ImplementationAgent = FullStackExpertAgent


async def _publish_ai_request(
    role: str,
    round_num: int,
    task_id: int | None = None,
    session_id: int | None = None,
) -> None:
    """发布 AI 请求 SSE 事件（延迟导入避免循环依赖）。"""
    try:
        from backend.webui.sse import publish_event

        payload: dict[str, Any] = {
            "role": role,
            "round_num": round_num,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        if session_id is not None:
            payload["session_id"] = session_id
        await publish_event("agent:ai_request", payload)
    except Exception:
        pass
