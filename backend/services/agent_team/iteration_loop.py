"""Agent 专家团队 - 迭代反馈循环服务

管理全栈专家 → 专业审查的迭代循环：
1. 全栈专家通过工具调用自主完成代码修改
2. 专业审查通过工具调用自主审查代码
3. 审查未通过时，将反馈返回给全栈专家继续修改
4. 最多迭代 max_iterations 轮
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

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
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)

    async def run(
        self,
        task_title: str,
        task_summary: str,
        source_type: str = "",
        source_issue_number: int | None = None,
        max_iterations: int = 3,
        sakura_memory: str = "",
    ) -> IterationOutcome:
        """运行迭代循环。"""
        expert = FullStackExpertAgent(self.workspace, self.workspace_service)
        reviewer = ProfessionalReviewAgent(self.workspace, self.workspace_service)

        total_tool_calls = 0
        feedback = ""

        for iteration in range(1, max_iterations + 1):
            logger.info(
                "Agent 迭代循环 第 {}/{} 轮 - 任务: {}",
                iteration,
                max_iterations,
                task_title,
            )

            # ── 全栈专家执行 ──
            fs_result = await expert.execute(
                task_title=task_title,
                task_summary=task_summary,
                source_type=source_type,
                source_issue_number=source_issue_number,
                sakura_memory=sakura_memory,
                feedback=feedback,
            )
            total_tool_calls += fs_result.tool_calls_count

            if not fs_result.success:
                return IterationOutcome(
                    success=False,
                    reason=f"全栈专家执行失败: {fs_result.error or fs_result.summary}",
                    iterations=iteration,
                    fullstack_result=fs_result,
                    total_tool_calls=total_tool_calls,
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

            # ── 专业审查 ──
            rev_result = await reviewer.review(
                task_title=task_title,
                task_summary=task_summary,
                modified_files=fs_result.modified_files,
                fullstack_summary=fs_result.summary,
            )
            total_tool_calls += rev_result.tool_calls_count

            logger.info(
                "审查结果: verdict={}, score={}, findings={}",
                rev_result.verdict,
                rev_result.score,
                len(rev_result.findings),
            )

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
                feedback = self._build_feedback(rev_result)
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

    def _build_feedback(self, review: ReviewResult) -> str:
        """将审查结果转为反馈文本。"""
        parts = [
            f"### 审查结论: {review.verdict} (分数: {review.score}/10)\n",
            f"### 审查总结\n{review.summary}\n",
        ]

        if review.findings:
            parts.append("### 发现的问题")
            for f in review.findings:
                parts.append(
                    f"- [{f.severity}] {f.file}: {f.message}"
                    + (f"\n  建议: {f.suggestion}" if f.suggestion else "")
                )
            parts.append("")

        if review.improvement_suggestions:
            parts.append("### 改进建议")
            for s in review.improvement_suggestions:
                parts.append(f"- {s}")
            parts.append("")

        return "\n".join(parts)
