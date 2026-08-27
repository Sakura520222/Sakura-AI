"""Sakura Agent using controlled tool calls.

The historical module and class names remain import-compatible for callers
that have not migrated yet. User-visible identity and runtime role values
are intentionally expressed as ``agent``.
"""

from __future__ import annotations

import asyncio
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


def _get_missing_tool_calls(messages: list[dict[str, Any]]) -> list[Any]:
    """返回缺少结果消息的工具调用。"""
    return get_missing_tool_calls(messages)


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
        self.messages.append(message)
        if self.checkpoint and self.session_id:
            return await self.checkpoint.append_message(self.session_id, message)
        return None

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

    async def execute(
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
        client, config = await create_agent_team_client()
        candidate = await client.resolve_role_primary_candidate(config.agent_role)
        context_window_tokens = (
            candidate.model.context_window_tokens if candidate else None
        )
        ctx = self._build_context(skills_context)
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

        tool_calls_count = 0
        token_tracker = TokenTracker()
        round_num = 0

        while True:
            round_num += 1
            if cancel_check and cancel_check():
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

            model_messages = await compress_agent_team_messages(
                self.messages, candidate=candidate, token_tracker=token_tracker
            )
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
                assistant_msg["tool_calls"] = [
                    tool_call_to_dict(tc) for tc in message.tool_calls
                ]
            await self._append_message(assistant_msg)

            # 无工具调用 → AI 以纯文本完成
            if not message.tool_calls:
                tracked = sorted(ctx.modified_files)
                return FullStackResult(
                    success=True,
                    summary=message.content or "任务完成（无工具调用）",
                    modified_files=tracked,
                    tool_calls_count=tool_calls_count,
                    prompt_tokens=token_tracker.prompt_tokens,
                    completion_tokens=token_tracker.completion_tokens,
                )

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
        terminal_output: dict[str, Any] | None = None
        for tool_call in tool_calls:
            fn_name = tool_call.function.name
            logger.info("Agent tool: {} (round={})", fn_name, round_num)

            if self.checkpoint and self.session_id:
                await self.checkpoint.mark_tool_call_running(
                    self.session_id, tool_call.id
                )
            try:
                if terminal_output is None:
                    result = await self.tool_executor.execute_tool_call(tool_call, ctx)
                else:
                    result = ToolResult(
                        success=True,
                        output={
                            "skipped": True,
                            "reason": "terminal_tool_already_called",
                        },
                    )
            except Exception as exc:
                if self.checkpoint and self.session_id:
                    await self.checkpoint.mark_tool_call_failed(
                        self.session_id, tool_call.id, str(exc)
                    )
                raise
            result_message_id = await self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": serialize_tool_result(result),
                }
            )
            if self.checkpoint and self.session_id and result_message_id:
                await self.checkpoint.mark_tool_call_completed(
                    self.session_id, tool_call.id, result_message_id
                )

            if result.is_terminal:
                terminal_output = result.output
        return terminal_output

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
