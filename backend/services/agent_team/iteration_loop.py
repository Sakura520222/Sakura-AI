"""Single implementation-Agent execution service.

The historical implementation ran a fullstack Agent followed by an internal
professional reviewer.  Agent Team now executes one Agent per
worker run.  External Sakura PR Review remains the feedback boundary; its
feedback schedules another run of the same Agent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self

from loguru import logger

from backend.models import database as db_module
from backend.models.agent_team_models import AgentTeamUserPrompt
from backend.models.database import utc_now
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
    ResumeCursor,
)
from backend.services.agent_team.conversation_context import (
    AgentTeamConversationContextService,
)
from backend.services.agent_team.execution import ExecutionRunner
from backend.services.agent_team.fullstack_expert import (
    FullStackExpertAgent,
    FullStackResult,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.prompt_config import (
    IMPLEMENTATION_SYSTEM_PROMPT,
    build_implementation_user_message,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.services.ai_reviewer.token_tracker import TokenTracker


@dataclass
class IterationOutcome:
    """Result of one Agent execution.

    ``fullstack_result`` and ``review_result`` remain as compatibility fields
    for the existing persistence/API shape.  New runs populate only
    ``fullstack_result``; no internal review result is produced.
    """

    success: bool
    reason: str
    iterations: int
    fullstack_result: FullStackResult | None = None
    review_result: Any | None = None
    modified_files: list[str] = field(default_factory=list)
    total_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class IterationLoopService:
    """Run one Agent and persist its checkpoint."""

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
        git_workspace_service: AgentTeamGitWorkspaceService | None = None,
        task_id: int | None = None,
        checkpoint: ConversationCheckpointService | None = None,
        resume_cursor: ResumeCursor | None = None,
        resume_index: int = 0,
        execution_runner: ExecutionRunner | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.git_workspace_service = (
            git_workspace_service or AgentTeamGitWorkspaceService()
        )
        self.task_id = task_id
        self.checkpoint = checkpoint
        self.resume_cursor = resume_cursor
        self.resume_index = resume_index
        self.execution_runner = execution_runner
        self.conversation_context = AgentTeamConversationContextService(task_id)
        self._active_agent: FullStackExpertAgent | None = None

    async def run(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        sakura_memory: str = "",
        skills_summary: str = "",
        skills_context: dict[str, Any] | None = None,
        reference_context: str = "",
        github_repo: Any | None = None,
        sakura_ref: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        initial_feedback: str = "",
        iteration_offset: int = 0,
        cancel_event: asyncio.Event | None = None,
        # Kept as a source-compatible no-op for callers from before the
        # migration.  Product execution no longer reads or enforces it.
        max_iterations: int | None = None,
        skip_internal_review: bool = False,
    ) -> IterationOutcome:
        """Execute exactly one Agent.

        ``max_iterations`` and ``skip_internal_review`` are accepted only so
        old integrations can deploy without a synchronized API migration. They
        are deliberately ignored: the worker controls lifecycle through
        cancellation, natural completion, errors, and the external PR review
        state machine.
        """
        del github_repo, sakura_ref, max_iterations, skip_internal_review

        tracker = TokenTracker()
        resume_cursor = self.resume_cursor
        run_number = self._run_number(resume_cursor, iteration_offset)

        if cancel_check and cancel_check():
            return IterationOutcome(
                success=False,
                reason="任务已取消",
                iterations=0,
            )

        try:
            execution_context = await self.conversation_context.build_agent_context(
                run_number
            )
        except Exception as exc:
            # Context is an optional historical aid. A database/context read
            # failure must not prevent the Agent from running.
            logger.warning("读取 Agent 历史上下文失败: {}", exc)
            execution_context = ""

        initial_user_message = build_implementation_user_message(
            task_title=task_title,
            task_summary=task_summary,
            source_type=source_type,
            source_issue_number=source_issue_number,
            sakura_memory=sakura_memory,
            skills_summary=skills_summary,
            reference_context=reference_context,
            feedback=initial_feedback,
            handoff_context=execution_context,
            role_memory_context="",
        )

        agent = await self._create_agent(
            "agent",
            run_number,
            resume_cursor,
            FullStackExpertAgent,
            initial_user_message=(
                initial_user_message
                if resume_cursor and resume_cursor.role_name == "fullstack"
                else None
            ),
        )
        self._active_agent = agent
        try:
            result = await agent.execute(
                task_title=task_title,
                task_summary=task_summary,
                source_type=source_type,
                source_issue_number=source_issue_number,
                sakura_memory=sakura_memory,
                skills_summary=skills_summary,
                skills_context=skills_context,
                reference_context=reference_context,
                feedback=initial_feedback,
                handoff_context=execution_context,
                role_memory_context="",
                iteration=run_number,
                cancel_check=cancel_check,
                guidance_callback=self._consume_pending_prompts,
                guidance_ack_callback=self._ack_pending_prompts,
                cancel_event=cancel_event,
            )
        finally:
            self._active_agent = None

        tracker.add_tokens(result.prompt_tokens, result.completion_tokens)
        session_id = getattr(agent, "session_id", None)
        await self._complete_session(session_id, result.tool_calls_count)
        if self.checkpoint and session_id:
            try:
                await self.checkpoint.save_session_result(
                    session_id,
                    {
                        "success": result.success,
                        "summary": result.summary,
                        "modified_files": result.modified_files,
                        "risk_level": result.risk_level,
                        "test_result": result.test_result,
                        "tool_calls_count": result.tool_calls_count,
                        "error": result.error,
                    },
                )
            except Exception as exc:
                logger.warning("保存 Agent 结构化结果失败: {}", exc)

        if cancel_check and cancel_check():
            return IterationOutcome(
                success=False,
                reason="任务已取消",
                iterations=1,
                fullstack_result=result,
                modified_files=list(result.modified_files or []),
                total_tool_calls=result.tool_calls_count,
                prompt_tokens=tracker.prompt_tokens,
                completion_tokens=tracker.completion_tokens,
            )

        if not result.success:
            return IterationOutcome(
                success=False,
                reason=f"Agent 执行失败: {result.error or result.summary}",
                iterations=1,
                fullstack_result=result,
                modified_files=list(result.modified_files or []),
                total_tool_calls=result.tool_calls_count,
                prompt_tokens=tracker.prompt_tokens,
                completion_tokens=tracker.completion_tokens,
            )

        if not result.modified_files:
            return IterationOutcome(
                success=False,
                reason="Agent 未修改任何文件",
                iterations=1,
                fullstack_result=result,
                modified_files=[],
                total_tool_calls=result.tool_calls_count,
                prompt_tokens=tracker.prompt_tokens,
                completion_tokens=tracker.completion_tokens,
            )

        try:
            await self.conversation_context.record_agent_turn(run_number, result)
        except Exception as exc:
            logger.warning("保存 Agent 执行上下文失败: {}", exc)

        return IterationOutcome(
            success=True,
            reason="Agent 执行完成",
            iterations=1,
            fullstack_result=result,
            modified_files=list(result.modified_files),
            total_tool_calls=result.tool_calls_count,
            prompt_tokens=tracker.prompt_tokens,
            completion_tokens=tracker.completion_tokens,
        )

    @staticmethod
    def _run_number(
        resume_cursor: ResumeCursor | None,
        iteration_offset: int,
    ) -> int:
        if resume_cursor and resume_cursor.iteration_number > 0:
            return resume_cursor.iteration_number
        return max(1, int(iteration_offset or 0) + 1)

    async def _create_agent(
        self,
        role_name: str,
        iteration: int,
        resume_cursor: ResumeCursor | None,
        agent_class: type[FullStackExpertAgent] = FullStackExpertAgent,
        initial_user_message: str | None = None,
    ) -> FullStackExpertAgent:
        """Create/resume the single Agent session.

        ``fullstack`` and ``reviewer`` cursors are historical input only. They
        never get resumed as new role sessions; a new ``role_name='agent'``
        session is created. Fullstack history can be copied into that session;
        reviewer history is intentionally not treated as implementation state.
        """
        initial_messages: list[dict[str, Any]] | None = None
        session_id: int | None = None
        can_resume = (
            self.checkpoint
            and resume_cursor
            and resume_cursor.role_name == "agent"
            and resume_cursor.iteration_number == iteration
        )
        if can_resume:
            session_id = resume_cursor.session_id
            initial_messages = await self.checkpoint.load_messages(session_id)
        elif self.checkpoint:
            if resume_cursor and resume_cursor.role_name == "fullstack":
                try:
                    initial_messages = await self.checkpoint.load_messages(
                        resume_cursor.session_id
                    )
                except Exception as exc:
                    logger.warning("读取历史 implementation checkpoint 失败: {}", exc)
                    initial_messages = None
            agent_session = await self.checkpoint.create_session(
                iteration,
                "agent",
                resume_index=self.resume_index,
            )
            session_id = agent_session.id
            if initial_messages:
                initial_messages = _normalize_legacy_messages(
                    initial_messages,
                    initial_user_message=initial_user_message,
                )
                for message in initial_messages:
                    await self.checkpoint.append_message(session_id, message)
        if not self.checkpoint:
            # Keep the simple two-argument constructor used by local fakes and
            # by integrations that run without persistence.
            if self.execution_runner is None:
                return agent_class(self.workspace, self.workspace_service)
            return agent_class(
                self.workspace,
                self.workspace_service,
                execution_runner=self.execution_runner,
            )
        agent_kwargs: dict[str, Any] = {
            "checkpoint": self.checkpoint,
            "session_id": session_id,
            "initial_messages": initial_messages,
        }
        if self.execution_runner is not None:
            agent_kwargs["execution_runner"] = self.execution_runner
        return agent_class(
            self.workspace,
            self.workspace_service,
            **agent_kwargs,
        )

    async def _complete_session(
        self,
        session_id: int | None,
        tool_calls_count: int,
    ) -> None:
        if self.checkpoint and session_id:
            await self.checkpoint.complete_session(session_id, tool_calls_count)

    async def _consume_pending_prompts(self) -> PendingGuidance | str:
        """Load the next queued guidance item for model admission.

        The Agent persists this item as a user message through
        ``append_guidance_message``. That checkpoint operation consumes the
        queue rows in the same transaction; the fallback callback acknowledges
        them only after a normal message append succeeds.
        """
        if not self.task_id:
            return ""

        try:
            from sqlalchemy import select

            async with db_module.async_session() as session:
                result = await session.execute(
                    select(AgentTeamUserPrompt)
                    .where(
                        AgentTeamUserPrompt.task_id == self.task_id,
                        AgentTeamUserPrompt.status == "pending",
                    )
                    .order_by(
                        AgentTeamUserPrompt.created_at,
                        AgentTeamUserPrompt.id,
                    )
                )
                prompts = [
                    (int(prompt.id), str(prompt.content))
                    for prompt in result.scalars().all()
                ]
        except Exception as exc:
            logger.warning(
                "读取 Agent pending guidance 失败 (task_id={}): {}", self.task_id, exc
            )
            # Do not fail open: the Agent must not make the
            # next model call while queued human guidance cannot be read.
            raise RuntimeError("读取 Agent pending guidance 失败") from exc

        if not prompts:
            return ""

        # If a worker crashed after the checkpoint transaction but before the
        # queue state was observed, the stable IDs in restored messages make a
        # retry idempotent. Acknowledging those rows is safe because the append
        # already succeeded.
        active_agent = self._active_agent
        existing_ids = {
            prompt_id
            for message in getattr(active_agent, "messages", [])
            for prompt_id in _guidance_ids_from_message(message)
        }
        already_admitted = [
            prompt_id for prompt_id, _ in prompts if prompt_id in existing_ids
        ]
        if already_admitted:
            await self._ack_pending_prompts(tuple(already_admitted))
            prompts = [item for item in prompts if item[0] not in set(already_admitted)]
        if not prompts:
            return ""
        # Keep every queued item as its own user message.  A separator or an
        # audit prefix would change the submitted guidance body, so stable IDs
        # travel only in message metadata and the queue/event audit trail.
        prompt_ids = tuple(prompt_id for prompt_id, _ in prompts)
        return PendingGuidance(
            prompts[0][1],
            prompt_ids,
            items=tuple(prompts),
        )

    async def _ack_pending_prompts(
        self, prompt_ids: tuple[int, ...] | list[int]
    ) -> None:
        """Mark guidance consumed after its user message is checkpointed."""
        if not self.task_id or not prompt_ids:
            return
        try:
            from sqlalchemy import select

            async with db_module.async_session() as session:
                result = await session.execute(
                    select(AgentTeamUserPrompt).where(
                        AgentTeamUserPrompt.task_id == self.task_id,
                        AgentTeamUserPrompt.id.in_(
                            tuple(int(item) for item in prompt_ids)
                        ),
                        AgentTeamUserPrompt.status == "pending",
                    )
                )
                for prompt in result.scalars().all():
                    prompt.status = "consumed"
                    prompt.consumed_at = utc_now()
                await session.commit()
        except Exception as exc:
            logger.warning(
                "标记 Agent guidance consumed 失败 (task_id={}): {}",
                self.task_id,
                exc,
            )
            # The message may be retried only while the queue row remains
            # pending.  Surface the failure so the caller blocks the model
            # request instead of silently proceeding without durable audit.
            raise RuntimeError("标记 Agent guidance consumed 失败") from exc

    async def _restore_fullstack_result(self, iteration: int) -> FullStackResult:
        """Read a legacy fullstack result for checkpoint compatibility."""
        if not self.checkpoint:
            raise RuntimeError("缺少 checkpoint，无法恢复历史 Agent 结果")
        session_id = await self.checkpoint.get_latest_completed_session(
            iteration, "fullstack"
        )
        if session_id is None:
            raise RuntimeError("缺少历史 implementation session")

        payload = await self.checkpoint.load_session_result(session_id)
        if payload and isinstance(payload, dict):
            return FullStackResult(
                success=payload.get("success", True),
                summary=payload.get("summary", ""),
                modified_files=sorted(payload.get("modified_files", [])),
                risk_level=payload.get("risk_level", "medium"),
                test_result=payload.get("test_result", ""),
                tool_calls_count=payload.get("tool_calls_count", 0),
                error=payload.get("error", ""),
            )
        return await self._restore_fullstack_result_from_messages(session_id)

    async def _restore_fullstack_result_from_messages(
        self,
        session_id: int,
    ) -> FullStackResult:
        messages = await self.checkpoint.load_messages(session_id)
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            content = message.get("content") or "{}"
            if "\n\n[进度:" in content:
                content = content[: content.index("\n\n[进度:")]
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "summary" not in payload:
                continue
            ai_files = payload.get("modified_files", [])
            modified_files = ai_files if isinstance(ai_files, list) else []
            return FullStackResult(
                success=True,
                summary=payload.get("summary", ""),
                modified_files=sorted(modified_files),
                risk_level=payload.get("risk_level", "medium"),
                test_result=payload.get("test_result", ""),
            )
        raise RuntimeError("无法从历史 Agent messages 中恢复完成结果")


class PendingGuidance(str):
    """String-compatible guidance item carrying a stable queue ID."""

    def __new__(
        cls,
        content: str,
        prompt_ids: tuple[int, ...],
        *,
        items: tuple[tuple[int, str], ...] | None = None,
    ) -> Self:
        value = super().__new__(cls, content)
        value.prompt_ids = tuple(prompt_ids)
        value.items = tuple(
            items or ((value.prompt_ids[0], content),) if value.prompt_ids else ()
        )
        return value


def _guidance_ids_from_message(message: Any) -> tuple[int, ...]:
    if not isinstance(message, dict) or message.get("role") != "user":
        return ()
    metadata = message.get("metadata")
    raw_ids = message.get("guidance_ids")
    if raw_ids is None and isinstance(metadata, dict):
        raw_ids = metadata.get("guidance_ids")
    if isinstance(raw_ids, (list, tuple)):
        parsed: list[int] = []
        for raw_id in raw_ids:
            try:
                parsed.append(int(raw_id))
            except TypeError, ValueError:
                continue
        if parsed:
            return tuple(parsed)
    content = str(message.get("content") or "")
    prefix = "[human_guidance:"
    if not content.startswith(prefix):
        return ()
    try:
        return (int(content[len(prefix) :].split("]", 1)[0]),)
    except TypeError, ValueError:
        return ()


def _normalize_legacy_messages(
    messages: list[dict[str, Any]],
    *,
    initial_user_message: str | None = None,
) -> list[dict[str, Any]]:
    """Copy legacy fullstack history under the current static system policy."""
    normalized = [dict(message) for message in messages]
    for message in normalized:
        if message.get("role") == "system":
            message["content"] = IMPLEMENTATION_SYSTEM_PROMPT
            break
    else:
        normalized.insert(
            0, {"role": "system", "content": IMPLEMENTATION_SYSTEM_PROMPT}
        )

    if initial_user_message is not None:
        for index, message in enumerate(normalized):
            if message.get("role") != "user":
                continue
            # A legacy guidance message is real user input and must remain
            # byte-for-byte unchanged. Replace only the old first-run user
            # message, which has no guidance metadata.
            if not _guidance_ids_from_message(message):
                normalized[index] = {
                    "role": "user",
                    "content": initial_user_message,
                }
                break
        else:
            normalized.insert(
                1,
                {"role": "user", "content": initial_user_message},
            )
    return normalized


__all__ = ["IterationLoopService", "IterationOutcome", "PendingGuidance"]
