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
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from loguru import logger

from backend.services.agent_team.ai_client import create_agent_team_client
from backend.services.agent_team.context_compressor import (
    AgentContextCompressor,
    compress_agent_team_messages,
)
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)
from backend.services.agent_team.execution import ExecutionRunner
from backend.services.agent_team.harness_runtime import create_harness_runtime
from backend.services.agent_team.lifecycle_hooks import HookFailure
from backend.services.agent_team.prompt_config import (
    IMPLEMENTATION_SYSTEM_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
    build_implementation_user_message,
)
from backend.services.agent_team.repository_context import (
    RepositoryContext,
    RepositoryContextError,
    skills_enabled,
)
from backend.services.agent_team.skill_scope import SkillRestriction
from backend.services.agent_team.skill_service import normalize_skill_slug
from backend.services.agent_team.strategy_self_check import StrategySelfCheckState
from backend.services.agent_team.subagents import (
    SubagentManager,
    SubagentStore,
    drain_cleanup,
)
from backend.services.agent_team.tool_scheduler import run_tool_batch
from backend.services.agent_team.tools.base import ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.registry import (
    create_executor,
    get_tool_definitions_fresh,
)
from backend.services.agent_team.tools.use_skill_tool import UseSkillTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.services.ai_reviewer.compression.errors import UsagePersistenceError
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
            "reconciliation_required",
            "checkpoint_inconsistent",
            "guidance_admission_failed",
            "repository_context_rejected",
        }:
            return "blocked"
        return "unrecoverable_error"


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
        self._subagents: SubagentManager | None = None
        self._read_only = False
        self._harness = None
        self._compressor: AgentContextCompressor | None = None
        self.messages: list[dict[str, Any]] = (
            [dict(message) for message in initial_messages]
            if initial_messages is not None
            else [{"role": "system", "content": FULLSTACK_SYSTEM_PROMPT}]
        )

    @property
    def system_prompt(self) -> str:
        return SUBAGENT_SYSTEM_PROMPT if self._read_only else FULLSTACK_SYSTEM_PROMPT

    async def _append_message(self, message: dict[str, Any]) -> int | None:
        message_id = None
        if self.checkpoint and self.session_id:
            message_id = await self.checkpoint.append_message(self.session_id, message)
        self.messages.append(message)
        return message_id

    async def _ensure_system_checkpoint(self) -> None:
        if (
            self.restored_messages
            or not self.checkpoint
            or not self.session_id
            or not self.messages
        ):
            return
        if len(self.messages) == 1 and self.messages[0].get("role") == "system":
            await self.checkpoint.append_message(self.session_id, self.messages[0])

    @staticmethod
    def _is_guidance_message(message: dict[str, Any]) -> bool:
        """Identify runtime guidance without inspecting or rewriting its body."""
        if message.get("role") != "user":
            return False
        if {"context_compaction", "harness_event"} & (
            message.get("metadata") or {}
        ).keys():
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
                message["content"] = self.system_prompt
                break
        else:
            self.messages.insert(
                0,
                {"role": "system", "content": self.system_prompt},
            )

        initial_user_index = next(
            (
                index
                for index, message in enumerate(self.messages)
                if message.get("role") == "user"
                and not self._is_guidance_message(message)
                and not message.get("metadata", {}).get("completion_reminder")
                and not message.get("metadata", {}).get("repository_context")
                and not message.get("metadata", {}).get("strategy_self_check")
                and "context_compaction" not in message.get("metadata", {})
                and "harness_event" not in message.get("metadata", {})
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
            executor=self.tool_executor,
        )

    async def _initialize_subagents(self, ctx: ToolContext) -> None:
        if (
            not isinstance(self.checkpoint, ConversationCheckpointService)
            or not self.session_id
        ):
            return
        store = SubagentStore(self.checkpoint.task_id)
        child = await store.for_session(self.session_id)
        if child is not None:
            self._read_only = True
            scope = SkillRestriction()
            for inherited in child.skill_scopes.values():
                scope = scope.intersect(inherited)
            self.tool_executor = create_executor(read_only=True, delegated_scope=scope)
            ctx.executor = self.tool_executor
            ctx.active_skill_tools.update(child.skill_scopes)
            # A fresh child has only a system seed. Recovered history is
            # refreshed by _prepare_restored_messages after ledger validation.
            if not self.restored_messages:
                self.messages = [{"role": "system", "content": self.system_prompt}]
            return
        from backend.core.config import get_dynamic_config_fresh

        concurrency = await get_dynamic_config_fresh("agent_team_subagent_concurrency")
        self._subagents = SubagentManager(
            self.checkpoint, self.session_id, ctx, concurrency=concurrency
        )
        ctx.subagents = self._subagents

    async def execute(self, *args: Any, **kwargs: Any) -> FullStackResult:
        result = None
        task_cancellation = None
        try:
            result = await self._execute(*args, **kwargs)
        except HookFailure as exc:
            result = FullStackResult(False, str(exc), error="hook_failed")
        except RepositoryContextError:
            ctx = getattr(self, "_active_context", None)
            result = FullStackResult(
                False,
                "仓库上下文路径或格式校验阻止了执行",
                error="repository_context_rejected",
                modified_files=sorted(ctx.modified_files) if ctx else [],
                tool_calls_count=sum(m.get("role") == "tool" for m in self.messages),
            )
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                task_cancellation = exc
            ctx = getattr(self, "_active_context", None)
            result = FullStackResult(
                success=False,
                summary="任务已取消",
                error="cancelled",
                modified_files=sorted(ctx.modified_files) if ctx else [],
                tool_calls_count=sum(m.get("role") == "tool" for m in self.messages),
            )
        finally:
            try:
                try:
                    if self._harness:
                        ctx = getattr(self, "_active_context", None)
                        if ctx and (result is None or not result.success):
                            try:
                                await drain_cleanup(
                                    self._harness.hooks.emit(
                                        "task_cancelled"
                                        if result and result.error == "cancelled"
                                        else "task_failed",
                                        ctx,
                                        audit_only=bool(
                                            result
                                            and result.error
                                            in {
                                                "reconciliation_required",
                                                "checkpoint_inconsistent",
                                            }
                                        ),
                                        status="cancelled"
                                        if result and result.error == "cancelled"
                                        else "failed",
                                    )
                                )
                            except Exception:
                                logger.error("Agent terminal lifecycle hook failed")
                finally:
                    await self._close_harness_resources()
            except (asyncio.CancelledError, Exception) as cleanup_error:
                if task_cancellation is None:
                    raise
                if not isinstance(cleanup_error, asyncio.CancelledError):
                    logger.error(
                        "Agent cleanup failed during task cancellation: {}",
                        type(cleanup_error).__name__,
                    )
        if task_cancellation is not None:
            # Retain structured lifecycle data, but let the owning worker/task
            # observe shutdown cancellation after all resources have drained.
            raise task_cancellation
        if result is not None and self._read_only:
            # Also sanitize completed finish ledgers restored from older runs.
            result.modified_files = []
        if (
            result is not None
            and isinstance(self.checkpoint, ConversationCheckpointService)
            and self.session_id
        ):
            (
                result.prompt_tokens,
                result.completion_tokens,
            ) = await self.checkpoint.load_usage(self.session_id)
        return result

    async def _close_harness_resources(self):
        try:
            if self._subagents:
                await drain_cleanup(self._subagents.close())
        finally:
            try:
                if self._compressor:
                    await drain_cleanup(self._compressor.aclose())
            finally:
                ctx = getattr(self, "_active_context", None)
                try:
                    if self._harness:
                        await drain_cleanup(self._harness.close())
                finally:
                    if ctx:
                        ctx.active_skill_tools.clear()

    async def _persist_harness_audit(self, event):
        await self._append_message(
            {"role": "audit", "content": "", "metadata": {"harness_event": event}}
        )

    async def _persist_provider_usage(self, usage: Any) -> None:
        if (
            isinstance(self.checkpoint, ConversationCheckpointService)
            and self.session_id
        ):
            failed = False
            try:
                # Drain known usage through cancellation before any model effect.
                await drain_cleanup(
                    self.checkpoint.record_usage(self.session_id, str(uuid4()), usage)
                )
            except Exception:
                failed = True
            if failed:
                # Never expose a driver exception or its sensitive context.
                raise UsagePersistenceError() from None

    async def _persist_compaction_audit(self, audit: dict[str, Any]) -> None:
        if self.checkpoint and self.session_id:
            # Audit rows are durable metadata, never an extra model/user turn.
            await self.checkpoint.append_message(
                self.session_id,
                {
                    "role": "audit",
                    "content": "",
                    "metadata": {"context_compaction": audit},
                },
            )

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
        try:
            await self._initialize_subagents(ctx)
        except ValueError:
            return FullStackResult(
                False, "子代理会话或运行时配置无效", error="checkpoint_inconsistent"
            )
        self._harness = create_harness_runtime(
            audit=self._persist_harness_audit,
            task_id=getattr(self.checkpoint, "task_id", None),
            session_id=self.session_id,
            read_only=self._read_only,
        )
        self.tool_executor.bind_runtime(self._harness.capabilities, self._harness.hooks)
        ctx.mcp_runtime = self._harness.mcp
        await self._ensure_system_checkpoint()
        if not (await self._harness.capabilities.check("request", ())).allowed:
            raise HookFailure("runtime_policy_unavailable_or_denied")
        ctx.repository_context = RepositoryContext(self.workspace)
        ctx.extra["admin_skills_index"] = dict(ctx.extra.get("skills_index", {}))
        await self._refresh_skills(ctx)
        if self.restored_messages:
            restored = await self._recover(ctx)
            if restored is not None:
                return restored
            self._restore_skill_workflows(ctx)
        await self._harness.hooks.emit("session_start", ctx, status="started")
        if self._subagents:
            await self._subagents.start()
        self._compressor = AgentContextCompressor.from_settings(
            audit_callback=self._persist_compaction_audit
            if self.checkpoint and self.session_id
            else None,
            audit_context={
                "task_id": getattr(self.checkpoint, "task_id", None),
                "session_id": self.session_id,
            },
            usage_callback=self._persist_provider_usage,
        )
        client, config = await create_agent_team_client(compressor=self._compressor)
        candidate = await client.resolve_role_primary_candidate(config.agent_role)
        context_window_tokens = (
            candidate.model.context_window_tokens if candidate else None
        )
        self._task_context = {
            "task_title": task_title,
            "task_summary": task_summary,
            "source_type": source_type,
            "source_issue_number": source_issue_number,
            "sakura_memory": sakura_memory,
            "skills_summary": skills_summary,
            "reference_context": reference_context,
            "feedback": feedback,
            "handoff_context": handoff_context,
            "role_memory_context": role_memory_context,
            "execution_expectations": execution_expectations,
        }
        self._prepare_restored_messages(
            task_title=task_title,
            task_summary=task_summary,
            source_type=source_type,
            source_issue_number=source_issue_number,
            sakura_memory=sakura_memory,
            skills_summary=skills_summary if ctx.skills_enabled else "",
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
                        skills_summary=skills_summary if ctx.skills_enabled else "",
                        reference_context=reference_context,
                        feedback=feedback,
                        handoff_context=handoff_context,
                        role_memory_context=role_memory_context,
                        execution_expectations=execution_expectations,
                    ),
                }
            )

        initial_docs = await asyncio.to_thread(
            ctx.repository_context.snapshot,
            ctx.repository_targets,
            whole=ctx.repository_whole_scope,
        )
        ctx.repository_instructions = {doc.path: doc for doc in initial_docs}
        repository_message = self._repository_message(ctx)
        if repository_message is not None and not has_missing_tool_results(
            self.messages
        ):
            await self._append_message(repository_message)

        tool_calls_count = sum(m.get("role") == "tool" for m in self.messages)
        token_tracker = TokenTracker()
        round_num = 0
        strategy = StrategySelfCheckState()

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

            for self_check in strategy.update(self.messages):
                await self._append_message(self_check)

            await self._harness.hooks.emit("before_model", ctx)
            await self._refresh_skills(ctx)
            # Re-read the current scope; deletions and replacements are observed
            # before the next request, while durable history stays untouched.
            current_docs = await asyncio.to_thread(
                ctx.repository_context.snapshot,
                ctx.repository_targets,
                whole=ctx.repository_whole_scope,
            )
            ctx.repository_instructions = {doc.path: doc for doc in current_docs}
            projected = self._project_model_messages(ctx)
            self._compressor.bind_source_messages(projected)
            tool_schemas = await get_tool_definitions_fresh("agent", ctx=ctx)
            model_messages = await compress_agent_team_messages(
                projected,
                candidate=candidate,
                token_tracker=token_tracker,
                compressor=self._compressor,
                tools=tool_schemas,
            )
            # Compression and legacy histories may omit repository messages.
            # Reinforce current data as a user turn, never system authority.
            repository_message = self._repository_message(ctx)
            if repository_message is not None:
                model_messages = [*model_messages, repository_message]
            for guidance in projected:
                if self._is_guidance_message(guidance) and not any(
                    message.get("role") == "user"
                    and message.get("content") == guidance.get("content")
                    for message in model_messages
                ):
                    model_messages.append(guidance)
            await _publish_ai_request(
                "agent",
                round_num,
                task_id=self.checkpoint.task_id if self.checkpoint else None,
                session_id=self.session_id,
            )
            try:
                response = await client.call_with_retry(
                    messages=model_messages,
                    model="",
                    tools=tool_schemas,
                    tool_choice="auto",
                    role="agent_team",
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                # Preserve provider/cancellation failures if a post hook fails.
                try:
                    await drain_cleanup(
                        self._harness.hooks.emit(
                            "after_model",
                            ctx,
                            status="cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "failed",
                            audit_only=isinstance(exc, asyncio.CancelledError),
                        )
                    )
                except Exception:
                    logger.error("Agent after_model failure hook failed")
                raise
            await self._persist_provider_usage(getattr(response, "usage", None))
            await self._harness.hooks.emit("after_model", ctx, status="completed")
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
                await self._append_message(
                    {
                        "role": "user",
                        "content": "Continue using tools. A text response does not complete this task. When the work and verification are complete, call finish_task with the summary and test evidence.",
                        "metadata": {"completion_reminder": True},
                    }
                )
                continue

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
            if (
                result.success
                and type(self.tool_executor.get_tool(tool_call.function.name))
                is UseSkillTool
            ):
                args = json.loads(tool_call.function.arguments)
                if not args.get("list_files"):
                    slug = normalize_skill_slug(str(args.get("slug") or ""))
                    ended = args.get("end_skill") is True
                    scope = ctx.active_skill_tools.get(slug, SkillRestriction.deny())
                    # Runtime-owned metadata commits atomically with the result
                    # and ledger status, never derived from untrusted output.
                    message["metadata"] = {
                        "skill_runtime_state": {
                            "version": 1,
                            "slug": slug,
                            "operation": "end" if ended else "activate",
                            "scope": None if ended else scope.to_data(),
                        }
                    }
            if ctx.active_skill_tools or message.get("metadata", {}).get(
                "skill_runtime_state"
            ):
                message.setdefault("metadata", {})["skill_workflow_ceiling"] = {
                    "version": 1,
                    "active": {
                        slug: scope.to_data()
                        for slug, scope in ctx.active_skill_tools.items()
                    },
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
                await self.tool_executor.repository_requirements(tool_calls, ctx)
            except RepositoryContextError as exc:
                for tool_call in tool_calls:
                    await after(
                        tool_call,
                        ToolResult(
                            False,
                            error=str(exc),
                            error_code="REPOSITORY_CONTEXT_REJECTED",
                        ),
                        "failed",
                    )
                return None
            if ctx.pending_repository_snapshot is not None:
                for tool_call in tool_calls:
                    await after(
                        tool_call,
                        ToolResult(
                            False,
                            error="New repository scope instructions delivered; retry this batch after applying them",
                            error_code="REPOSITORY_CONTEXT_REQUIRED",
                        ),
                        "failed",
                    )
                ctx.repository_instructions = ctx.pending_repository_snapshot
                ctx.pending_repository_snapshot = None
                ctx.repository_targets = ctx.pending_repository_targets
                ctx.repository_whole_scope = ctx.pending_repository_whole_scope
                repository_message = self._repository_message(ctx)
                if repository_message is not None:
                    await self._append_message(repository_message)
                return None
            ctx.repository_targets = ctx.pending_repository_targets
            ctx.repository_whole_scope = ctx.pending_repository_whole_scope

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
        index = {
            slug: entry
            for slug, entry in ctx.extra.get("skills_index", {}).items()
            if entry.get("source_type") == "repository"
        }
        if repository is None or not (
            ctx.repository_instructions
            or index
            or repository.diagnostics
            or ctx.active_skill_tools
        ):
            return None
        content = repository.render(
            list(ctx.repository_instructions.values()),
            index=index if ctx.skills_enabled else {},
            workflow={
                slug: scope.to_data() for slug, scope in ctx.active_skill_tools.items()
            },
        )
        return {
            "role": "user",
            "content": content,
            "metadata": {"repository_context": True},
        }

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
                self._restore_ceiling_snapshot(message, ctx)
                continue
            try:
                output = json.loads(message.get("content") or "{}")
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError, TypeError:
                self._restore_ceiling_snapshot(message, ctx)
                continue
            if (
                not isinstance(output, dict)
                or output.get("error")
                or not isinstance(args, dict)
            ):
                self._restore_ceiling_snapshot(message, ctx)
                continue
            slug = str(args.get("slug") or "")
            slug = normalize_skill_slug(slug)
            if args.get("list_files"):
                self._restore_ceiling_snapshot(message, ctx)
                continue
            metadata = message.get("metadata")
            state = (
                metadata.get("skill_runtime_state")
                if isinstance(metadata, dict)
                else None
            )
            ended = args.get("end_skill") is True
            valid = (
                isinstance(state, dict)
                and type(state.get("version")) is int
                and state.get("version") == 1
                and state.get("slug") == slug
                and state.get("operation") == ("end" if ended else "activate")
            )
            if ended and valid and state.get("scope") is None:
                ctx.active_skill_tools.pop(slug, None)
                self._restore_ceiling_snapshot(message, ctx)
                continue
            try:
                if not valid or ended:
                    raise ValueError("Missing historical Skill ceiling")
                historical = SkillRestriction.from_data(state.get("scope"))
                entry = ctx.extra.get("skills_index", {}).get(slug)
                current = (
                    SkillRestriction.from_metadata(entry.get("allowed_tools"))
                    if entry
                    else SkillRestriction.deny()
                )
            except ValueError:
                historical = current = SkillRestriction.deny()
            prior = ctx.active_skill_tools.get(slug, SkillRestriction())
            ctx.active_skill_tools[slug] = prior.intersect(historical).intersect(
                current
            )
            self._restore_ceiling_snapshot(message, ctx)

    @staticmethod
    def _restore_ceiling_snapshot(message, ctx) -> None:
        metadata = message.get("metadata")
        if not isinstance(metadata, dict) or "skill_workflow_ceiling" not in metadata:
            return
        snapshot = metadata["skill_workflow_ceiling"]
        try:
            if (
                not isinstance(snapshot, dict)
                or type(snapshot.get("version")) is not int
                or snapshot.get("version") != 1
            ):
                raise ValueError("Invalid historical workflow state")
            active = snapshot.get("active")
            if not isinstance(active, dict):
                raise ValueError("Invalid historical workflow state")
            for slug in set(ctx.active_skill_tools) | set(active):
                if not isinstance(slug, str) or normalize_skill_slug(slug) != slug:
                    raise ValueError("Invalid historical workflow slug")
                prior = ctx.active_skill_tools.get(slug, SkillRestriction())
                try:
                    historical = SkillRestriction.from_data(active[slug])
                    entry = ctx.extra.get("skills_index", {}).get(slug)
                    current = (
                        SkillRestriction.from_metadata(entry.get("allowed_tools"))
                        if entry
                        else SkillRestriction.deny()
                    )
                except KeyError, ValueError:
                    historical = current = SkillRestriction.deny()
                ctx.active_skill_tools[slug] = prior.intersect(historical).intersect(
                    current
                )
        except ValueError:
            for slug in ctx.active_skill_tools:
                ctx.active_skill_tools[slug] = SkillRestriction.deny()

    async def _refresh_skills(self, ctx: ToolContext) -> None:
        ctx.skills_enabled = await skills_enabled()
        if not ctx.skills_enabled:
            ctx.extra["skills_index"] = {}
            ctx.extra.pop("skills_cache", None)
            return
        repository = ctx.repository_context
        repo_index = (
            await asyncio.to_thread(repository.discover_skills) if repository else {}
        )
        ctx.extra["skills_index"] = {
            **repo_index,
            **ctx.extra.get("admin_skills_index", {}),
        }
        for slug, historical in ctx.active_skill_tools.items():
            entry = ctx.extra["skills_index"].get(slug)
            try:
                current = (
                    SkillRestriction.from_metadata(entry.get("allowed_tools"))
                    if entry
                    else SkillRestriction.deny()
                )
            except ValueError:
                current = SkillRestriction.deny()
            ctx.active_skill_tools[slug] = historical.intersect(current)

    def _project_model_messages(self, ctx: ToolContext) -> list[dict[str, Any]]:
        """Project durable originals; no repository snapshot or disabled Skill
        body is replayed or compressed, and assistant/tool pairing is retained.
        """
        skill_calls = set()
        result = []
        initial = True
        for original in self.messages:
            message = dict(original)
            if {"context_compaction", "harness_event"} & message.get(
                "metadata", {}
            ).keys():
                continue
            if message.get("metadata", {}).get("repository_context"):
                continue
            for call in message.get("tool_calls") or []:
                if call.get("function", {}).get("name") == "use_skill":
                    skill_calls.add(call.get("id"))
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") in skill_calls
                and not ctx.skills_enabled
            ):
                message["content"] = json.dumps({"skills_disabled": True})
                message.pop("metadata", None)
            elif (
                message.get("role") == "user"
                and initial
                and not self._is_guidance_message(message)
                and not message.get("metadata", {}).get("completion_reminder")
                and not message.get("metadata", {}).get("strategy_self_check")
            ):
                initial = False
                args = dict(self._task_context)
                if not ctx.skills_enabled:
                    args["skills_summary"] = ""
                message["content"] = self._build_user_message(**args)
            result.append(message)
        return result

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
        # Non-tool hooks mutate too. Only a matching durable completion closes
        # an admission; removed hooks do not make interrupted effects replayable.
        effects = [
            event
            for message in self.messages
            if (event := message.get("metadata", {}).get("harness_event", {})).get(
                "kind"
            )
            == "hook_effect"
            and event.get("effect") == "workspace_write"
        ]
        completed_effects = {
            event.get("effect_id")
            for event in effects
            if event.get("status") == "completed" and event.get("effect_id")
        }
        if any(
            not event.get("tool_call_id")
            and event.get("status") == "admitted"
            and (
                not event.get("effect_id")
                or event["effect_id"] not in completed_effects
            )
            for event in effects
        ):
            return FullStackResult(
                False,
                "中断的生命周期写操作需要核对工作区后再恢复",
                error="reconciliation_required",
            )
        hook_mutations = {
            event.get("tool_call_id")
            for message in self.messages
            if (event := message.get("metadata", {}).get("harness_event", {})).get(
                "kind"
            )
            == "hook_effect"
            and event.get("effect") == "workspace_write"
        }
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
                and (
                    not self.tool_executor.metadata(name).read_only
                    or ident in hook_mutations
                )
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
        # Recover the last verified batch's scope before sending its persisted
        # results back to the model. Rules themselves must be reread from the
        # current workspace; historical user text never supplies authority.
        for message in reversed(self.messages):
            verified = [
                SimpleNamespace(function=SimpleNamespace(**call["function"]))
                for call in message.get("tool_calls") or []
                if states.get(call["id"], {}).get("status") == "completed"
                and call["id"] in results
                and "error" not in results[call["id"]]
                and self.tool_executor.metadata(
                    call["function"].get("name", "")
                ).workspace_access
            ]
            if verified:
                ctx.repository_targets, ctx.repository_whole_scope = (
                    self.tool_executor.repository_targets(verified, ctx)
                )
                break
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
