"""Agent 专家团队 - 迭代反馈循环服务

管理全栈专家 → 专业审查的迭代循环：
1. 全栈专家通过工具调用自主完成代码修改
2. 专业审查通过工具调用自主审查代码
3. 审查未通过时，将反馈返回给全栈专家继续修改
4. 最多迭代 max_iterations 轮
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from backend.models.agent_team_models import AgentTeamUserPrompt
from backend.models.database import async_session, utc_now
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
    ResumeCursor,
)
from backend.services.agent_team.conversation_context import (
    AgentTeamConversationContextService,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.fullstack_expert import (
    FullStackExpertAgent,
    FullStackResult,
)
from backend.services.agent_team.professional_reviewer import (
    ProfessionalReviewAgent,
    ReviewResult,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@dataclass
class IterationOutcome:
    """迭代循环最终结果。"""

    success: bool
    reason: str
    iterations: int
    fullstack_result: FullStackResult | None = None
    review_result: ReviewResult | None = None
    modified_files: list[str] = field(default_factory=list)
    total_tool_calls: int = 0


class IterationLoopService:
    """迭代反馈循环服务。"""

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
        git_workspace_service: AgentTeamGitWorkspaceService | None = None,
        task_id: int | None = None,
        checkpoint: ConversationCheckpointService | None = None,
        resume_cursor: ResumeCursor | None = None,
        resume_index: int = 0,
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
        self.conversation_context = AgentTeamConversationContextService(task_id)

    async def run(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        max_iterations: int = 3,
        sakura_memory: str = "",
        skills_summary: str = "",
        skills_context: dict[str, Any] | None = None,
        github_repo: Any | None = None,
        sakura_ref: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> IterationOutcome:
        """运行迭代循环。"""
        total_tool_calls = 0
        feedback = ""
        resume_cursor = self.resume_cursor
        start_iteration = resume_cursor.iteration_number if resume_cursor else 1

        for iteration in range(start_iteration, max_iterations + 1):
            if cancel_check and cancel_check():
                return IterationOutcome(
                    success=False,
                    reason="任务已取消",
                    iterations=iteration - 1,
                    total_tool_calls=total_tool_calls,
                )
            logger.info(
                "Agent 迭代循环 第 {}/{} 轮 - 任务: {}",
                iteration,
                max_iterations,
                task_title,
            )

            fullstack_handoff_context = await self.conversation_context.build_handoff_context(
                "fullstack", iteration
            )
            fullstack_role_memory = await self.conversation_context.build_role_memory(
                "fullstack", iteration
            )
            reviewer_handoff_context = ""
            reviewer_role_memory = ""

            # 消费待处理的管理员指导，合入 feedback 跨迭代传递
            iteration_guidance = await self._consume_pending_prompts()
            if iteration_guidance and feedback:
                feedback = f"{feedback}\n\n{iteration_guidance}"
            elif iteration_guidance:
                feedback = iteration_guidance

            # ── 全栈专家执行 ──
            if resume_cursor and resume_cursor.role_name == "reviewer":
                fs_result = await self._restore_fullstack_result(iteration)
            elif (
                resume_cursor
                and resume_cursor.role_name == "fullstack"
                and resume_cursor.status == "completed"
            ):
                fs_result = await self._restore_fullstack_result(iteration)
                resume_cursor = None
            else:
                expert = await self._create_agent(
                    "fullstack", iteration, resume_cursor, FullStackExpertAgent
                )
                fs_result = await expert.execute(
                    task_title=task_title,
                    task_summary=task_summary,
                    source_type=source_type,
                    source_issue_number=source_issue_number,
                    sakura_memory=sakura_memory,
                    skills_summary=skills_summary,
                    skills_context=skills_context,
                    feedback=feedback,
                    handoff_context=fullstack_handoff_context,
                    role_memory_context=fullstack_role_memory,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    cancel_check=cancel_check,
                    guidance_callback=self._consume_pending_prompts,
                )
                total_tool_calls += fs_result.tool_calls_count
                await self._complete_session(
                    getattr(expert, "session_id", None), fs_result.tool_calls_count
                )
                if self.checkpoint and getattr(expert, "session_id", None):
                    try:
                        await self.checkpoint.save_session_result(
                            expert.session_id,
                            {
                                "success": fs_result.success,
                                "summary": fs_result.summary,
                                "modified_files": fs_result.modified_files,
                                "risk_level": fs_result.risk_level,
                                "test_result": fs_result.test_result,
                                "tool_calls_count": fs_result.tool_calls_count,
                                "error": fs_result.error,
                            },
                        )
                    except Exception as exc:
                        logger.warning("保存 fullstack 结构化结果失败，将使用消息解析回退: {}", exc)
                if resume_cursor and resume_cursor.role_name == "fullstack":
                    resume_cursor = None

            can_review_partial_changes = (
                fs_result.error == "max_rounds_reached_with_changes"
            )
            if not fs_result.success and not can_review_partial_changes:
                return IterationOutcome(
                    success=False,
                    reason=f"全栈专家执行失败: {fs_result.error or fs_result.summary}",
                    iterations=iteration,
                    fullstack_result=fs_result,
                    modified_files=fs_result.modified_files,
                    total_tool_calls=total_tool_calls,
                )

            if not fs_result.success:
                logger.info(
                    "全栈专家达到工具轮次上限但已修改 {} 个文件，继续进入专业审查: {}",
                    len(fs_result.modified_files),
                    fs_result.error or fs_result.summary,
                )

            if not fs_result.modified_files:
                return IterationOutcome(
                    success=False,
                    reason="全栈专家未修改任何文件",
                    iterations=iteration,
                    fullstack_result=fs_result,
                    total_tool_calls=total_tool_calls,
                )

            logger.info(
                "全栈专家完成: {} 个文件被修改, 总结: {}",
                len(fs_result.modified_files),
                fs_result.summary[:100],
            )
            await self.conversation_context.record_fullstack_turn(iteration, fs_result)
            reviewer_handoff_context = await self.conversation_context.build_handoff_context(
                "reviewer", iteration + 1
            )
            reviewer_role_memory = await self.conversation_context.build_role_memory(
                "reviewer", iteration
            )

            # ── 专业审查 ──
            if cancel_check and cancel_check():
                return IterationOutcome(
                    success=False,
                    reason="任务已取消",
                    iterations=iteration,
                    fullstack_result=fs_result,
                    modified_files=fs_result.modified_files,
                    total_tool_calls=total_tool_calls,
                )
            diff_summary = ""
            try:
                diff_summary = await self.git_workspace_service.get_diff_summary(
                    self.workspace
                )
            except Exception:
                logger.warning("获取 diff summary 失败，审查员将不携带 diff 摘要")

            reviewer = await self._create_agent(
                "reviewer", iteration, resume_cursor, ProfessionalReviewAgent
            )
            rev_result = await reviewer.review(
                task_title=task_title,
                task_summary=task_summary,
                modified_files=fs_result.modified_files,
                fullstack_summary=fs_result.summary,
                diff_summary=diff_summary,
                handoff_context=reviewer_handoff_context,
                role_memory_context=reviewer_role_memory,
                skills_summary=skills_summary,
                skills_context=skills_context,
                github_repo=github_repo,
                sakura_ref=sakura_ref,
                user_guidance="",
                cancel_check=cancel_check,
                guidance_callback=self._consume_pending_prompts,
            )
            total_tool_calls += rev_result.tool_calls_count
            await self._complete_session(
                getattr(reviewer, "session_id", None), rev_result.tool_calls_count
            )
            if resume_cursor and resume_cursor.role_name == "reviewer":
                resume_cursor = None

            logger.info(
                "审查结果: verdict={}, score={}, findings={}",
                rev_result.verdict,
                rev_result.score,
                len(rev_result.findings),
            )
            await self.conversation_context.record_reviewer_turn(iteration, rev_result)

            if rev_result.passed:
                return IterationOutcome(
                    success=True,
                    reason=f"审查通过 (第 {iteration} 轮, 分数 {rev_result.score})",
                    iterations=iteration,
                    fullstack_result=fs_result,
                    review_result=rev_result,
                    modified_files=fs_result.modified_files,
                    total_tool_calls=total_tool_calls,
                )

            # ── 未通过：准备反馈 ──
            if iteration < max_iterations:
                feedback = self._build_feedback(rev_result, iteration=iteration)
                logger.info("准备第 {} 轮迭代反馈", iteration + 1)
            else:
                # 最后一轮也提交（即使未通过），让外部决定
                logger.info("达到最大迭代次数 {}，以当前状态提交", max_iterations)

        return IterationOutcome(
            success=rev_result.passed if rev_result else False,
            reason=f"达到最大迭代次数 {max_iterations}"
            + (f"，最终分数 {rev_result.score}" if rev_result else ""),
            iterations=max_iterations,
            fullstack_result=fs_result,
            review_result=rev_result,
            modified_files=fs_result.modified_files if fs_result else [],
            total_tool_calls=total_tool_calls,
        )

    async def _restore_fullstack_result(self, iteration: int) -> FullStackResult:
        if not self.checkpoint:
            raise RuntimeError("缺少 checkpoint，无法恢复 fullstack 结果")
        session_id = await self.checkpoint.get_latest_completed_session(
            iteration, "fullstack"
        )
        if session_id is None:
            raise RuntimeError("缺少已完成的 fullstack session，无法续跑 reviewer")

        # Prefer structured result payload
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

        # Fallback: legacy message-based recovery for sessions before migration
        return await self._restore_fullstack_result_from_messages(session_id)

    async def _restore_fullstack_result_from_messages(
        self, session_id: int
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
        raise RuntimeError("无法从 fullstack messages 中恢复完成结果")

    async def _create_agent(
        self,
        role_name: str,
        iteration: int,
        resume_cursor: ResumeCursor | None,
        agent_class: type[FullStackExpertAgent | ProfessionalReviewAgent],
    ):
        initial_messages = None
        session_id = None
        if (
            self.checkpoint
            and resume_cursor
            and resume_cursor.role_name == role_name
            and resume_cursor.iteration_number == iteration
        ):
            session_id = resume_cursor.session_id
            initial_messages = await self.checkpoint.load_messages(session_id)
        elif self.checkpoint:
            agent_session = await self.checkpoint.create_session(
                iteration,
                role_name,
                resume_index=self.resume_index,
            )
            session_id = agent_session.id
        if not self.checkpoint:
            return agent_class(self.workspace, self.workspace_service)
        return agent_class(
            self.workspace,
            self.workspace_service,
            checkpoint=self.checkpoint,
            session_id=session_id,
            initial_messages=initial_messages,
        )

    async def _complete_session(
        self, session_id: int | None, tool_calls_count: int
    ) -> None:
        if self.checkpoint and session_id:
            await self.checkpoint.complete_session(session_id, tool_calls_count)

    def _build_feedback(self, review: ReviewResult, iteration: int = 0) -> str:
        """将审查结果转为结构化反馈文本。"""
        parts = [
            f"## 审查反馈 - 迭代 {iteration} (分数: {review.score}/10)\n",
            f"### 审查总结\n{review.summary}\n",
        ]

        # 按严重性分组
        blocking = [f for f in review.findings if f.severity in ("critical", "major")]
        optional = [f for f in review.findings if f.severity in ("minor", "suggestion")]

        if blocking:
            parts.append("### 必须修复（阻塞）")
            # 按文件分组
            by_file: dict[str, list[Any]] = {}
            for f in blocking:
                by_file.setdefault(f.file, []).append(f)
            for file_path, items in sorted(by_file.items()):
                parts.append(f"\n**{file_path}**")
                for item in items:
                    line = f"- [{item.severity}] {item.message}"
                    if item.suggestion:
                        line += f"\n  修复建议: {item.suggestion}"
                    parts.append(line)
            parts.append("")

        if optional:
            parts.append("### 可选改进")
            for f in optional:
                line = f"- [{f.severity}] {f.file}: {f.message}"
                if f.suggestion:
                    line += f"\n  建议: {f.suggestion}"
                parts.append(line)
            parts.append("")

        if review.improvement_suggestions:
            parts.append("### 改进建议")
            for s in review.improvement_suggestions:
                parts.append(f"- {s}")
            parts.append("")

        parts.append("### 重要提示")
        parts.append("- 仅解决上述问题，不要重新设计未标记的区域")
        parts.append("- 不要撤销之前的修复")
        parts.append("- 修复后运行代码检查和测试验证")
        return "\n".join(parts)

    async def _consume_pending_prompts(self) -> str:
        """消费 pending 状态的管理员 Prompt，返回格式化指导文本。"""
        if not self.task_id:
            return ""
        try:
            from sqlalchemy import select

            async with async_session() as session:
                result = await session.execute(
                    select(AgentTeamUserPrompt)
                    .where(
                        AgentTeamUserPrompt.task_id == self.task_id,
                        AgentTeamUserPrompt.status == "pending",
                    )
                    .order_by(AgentTeamUserPrompt.created_at)
                )
                prompts = result.scalars().all()
                if not prompts:
                    return ""

                parts = []
                for prompt in prompts:
                    prompt.status = "consumed"
                    prompt.consumed_at = utc_now()
                    parts.append(prompt.content)
                await session.commit()

            logger.info(
                "已消费 {} 条管理员 Prompt (task_id={})",
                len(parts), self.task_id,
            )
            return "## 管理员指导\n请遵循以下方向执行任务：\n" + "\n".join(
                f"- {p}" for p in parts
            )
        except Exception:
            return ""
