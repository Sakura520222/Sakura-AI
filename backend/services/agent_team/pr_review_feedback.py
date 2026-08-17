"""Bridge Sakura PR review results back into Agent Team tasks."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from backend.core.config import get_dynamic_config, get_settings
from backend.models import database as db_module
from backend.models.agent_team_models import (
    AgentTeamFeedback,
    AgentTeamFeedbackSource,
    AgentTeamSourceType,
    AgentTeamTask,
    AgentTeamTaskStatus,
)
from backend.models.database import PRReview, PRStatus, ReviewComment, utc_now


class AgentPRReviewOutcome(str, enum.Enum):
    PASSED = "passed"
    NEEDS_ITERATION = "needs_iteration"
    WAITING_HUMAN = "waiting_human"


@dataclass
class AgentPRReviewFeedbackResult:
    handled: bool
    task_id: int | None = None
    action: str = "ignored"
    reason: str = ""


def _enum_value(value: Any) -> str | None:
    """Return a comparable string for enum/string DB values."""
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def parse_blocking_severities(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip().lower() for item in value.split(",") if item.strip()}
    try:
        return {str(item).strip().lower() for item in value if str(item).strip()}
    except TypeError:
        text = str(value).strip().lower()
        return {text} if text else set()


async def schedule_agent_pr_review_iteration(task_id: int, review_id: int) -> Any:
    """Schedule an Agent Team iteration for Sakura PR review feedback."""
    from backend.workers.agent_team_worker import (
        submit_agent_team_pr_review_iteration,
    )

    return await submit_agent_team_pr_review_iteration(task_id, review_id)


def classify_agent_pr_review_outcome(
    review: PRReview,
    comments: list[ReviewComment],
    pass_score: int,
    blocking_severities: set[str],
) -> AgentPRReviewOutcome:
    severities = {(comment.severity or "").lower() for comment in comments}
    if severities & blocking_severities:
        return AgentPRReviewOutcome.NEEDS_ITERATION
    if review.overall_score is not None and int(review.overall_score) < pass_score:
        return AgentPRReviewOutcome.NEEDS_ITERATION
    if (
        review.overall_score is None
        and not comments
        and not getattr(
            review,
            "review_summary",
            None,
        )
    ):
        return AgentPRReviewOutcome.WAITING_HUMAN
    return AgentPRReviewOutcome.PASSED


class AgentTeamPRReviewFeedbackService:
    """Apply completed Sakura PR review results to matching Agent Team tasks."""

    async def handle_review_completed(self, review_id: int) -> bool:
        result = await self.handle_review_completed_with_result(review_id)
        return result.handled

    async def handle_review_completed_with_result(
        self,
        review_id: int,
    ) -> AgentPRReviewFeedbackResult:
        session_factory = db_module.async_session
        if session_factory is None:
            return AgentPRReviewFeedbackResult(False, reason="db_session_unavailable")

        async with session_factory() as session:
            review = await session.get(PRReview, review_id)
            if review is None:
                return AgentPRReviewFeedbackResult(False, reason="review_not_found")

            if _enum_value(review.status) != PRStatus.COMPLETED.value:
                return AgentPRReviewFeedbackResult(False, reason="review_not_completed")

            task = await self._find_task(session, review)
            if task is None:
                return AgentPRReviewFeedbackResult(False, reason="task_not_found")

            task_id = int(task.id)
            result_task = AgentPRReviewFeedbackResult(False, task_id=task_id)
            if _enum_value(task.status) not in {
                AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
                AgentTeamTaskStatus.PR_OPENED.value,
            }:
                result_task.reason = "task_status_not_reviewable"
                return result_task

            task_head_sha = getattr(task, "pr_head_sha", None)
            review_head_sha = getattr(review, "head_sha", None)
            if task_head_sha and review_head_sha and task_head_sha != review_head_sha:
                result_task.reason = "stale_head_sha"
                return result_task

            external_id = f"pr_review:{review.id}"
            if await self._feedback_exists(session, task_id, external_id):
                result_task.reason = "duplicate_feedback"
                return result_task

            comments = await self._load_comments(session, review.id)
            pass_score = await self._load_pass_score()
            blocking_severities = await self._load_blocking_severities()
            outcome = classify_agent_pr_review_outcome(
                review,
                comments,
                pass_score=pass_score,
                blocking_severities=blocking_severities,
            )

            feedback = AgentTeamFeedback(
                task_id=task_id,
                source=AgentTeamFeedbackSource.SAKURA_PR_REVIEW.value,
                external_id=external_id,
                author="Sakura PR Review",
                content=self._format_feedback(review, comments, outcome),
                resolved=0,
            )
            session.add(feedback)

            if outcome == AgentPRReviewOutcome.PASSED:
                task.status = AgentTeamTaskStatus.COMPLETED.value
                task.current_phase = AgentTeamTaskStatus.COMPLETED.value
                task.completed_at = utc_now()  # MySQL TIMESTAMP 列自动去除时区
                task.error_message = None
                await session.commit()
                return AgentPRReviewFeedbackResult(
                    True,
                    task_id=task_id,
                    action="completed",
                )

            iteration_count = int(getattr(task, "iteration_count", 0) or 0)
            max_iterations = int(getattr(task, "max_iterations", 0) or 0)
            at_iteration_limit = (
                max_iterations > 0 and iteration_count >= max_iterations
            )
            if outcome == AgentPRReviewOutcome.WAITING_HUMAN or at_iteration_limit:
                task.status = AgentTeamTaskStatus.WAITING_HUMAN.value
                task.current_phase = AgentTeamTaskStatus.WAITING_HUMAN.value
                if at_iteration_limit:
                    task.error_message = (
                        "达到 Agent 最大迭代轮数，请人工处理 Sakura PR Review 反馈。"
                    )
                else:
                    task.error_message = "Sakura PR Review 结果需要人工确认。"
                await session.commit()
                return AgentPRReviewFeedbackResult(
                    True,
                    task_id=task_id,
                    action="waiting_human",
                )

            task.status = AgentTeamTaskStatus.ITERATING.value
            task.current_phase = AgentTeamTaskStatus.ITERATING.value
            task.error_message = None
            await session.commit()

        await schedule_agent_pr_review_iteration(task_id, review.id)
        return AgentPRReviewFeedbackResult(
            True,
            task_id=task_id,
            action="scheduled_iteration",
        )

    async def _find_task(self, session: Any, review: PRReview) -> AgentTeamTask | None:
        # 策略 1: Agent 修复 PR 的直接审查 → 通过 branch_name 匹配
        # NOTE: PRReview.pr_id stores the GitHub *node* ID (e.g. 3776490879),
        # while AgentTeamTask.pr_number stores the human-readable PR number (e.g. 376).
        # Match by repo + branch first, which uniquely identifies an active Agent task.
        statement = (
            select(AgentTeamTask)
            .where(
                AgentTeamTask.repo_owner == review.repo_owner,
                AgentTeamTask.repo_name == review.repo_name,
                AgentTeamTask.branch_name == review.branch,
                AgentTeamTask.status.notin_(
                    [
                        AgentTeamTaskStatus.FAILED.value,
                        AgentTeamTaskStatus.CANCELLED.value,
                        AgentTeamTaskStatus.ABANDONED.value,
                        AgentTeamTaskStatus.COMPLETED.value,
                    ]
                ),
            )
            .order_by(AgentTeamTask.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(statement)
        task = result.scalars().first()
        if task is not None:
            return task

        # 策略 2: 源 PR 被再次审查时回环到同一 Agent 任务
        # 当 Agent 修复 PR 合并回源 PR 后，源 PR 的新审查 review.branch 是源 PR 分支，
        # 无法直接匹配 Agent 任务的 branch_name（修复分支）。
        # 此时通过 pr_review 来源 + 同 repo + 同源 PR number + 非终态查找原 Agent 任务。
        # 安全性说明：AgentTeamTask.source_issue_number 与 PRReview.pr_number 同为
        # 仓库本地 PR number，二者相等即同一 PR；duplicate guard（candidate_service
        # #build_pr_review_task_draft）按 repo + pr_number 阻止同 PR 重复任务，但
        # 不同 PR 可并存非终态任务，因此这里必须限定 pr_number，防止错绑其他
        # PR 的 Agent 任务。
        conditions = [
            AgentTeamTask.repo_owner == review.repo_owner,
            AgentTeamTask.repo_name == review.repo_name,
            AgentTeamTask.source_type == AgentTeamSourceType.PR_REVIEW.value,
            AgentTeamTask.status.notin_(
                [
                    AgentTeamTaskStatus.FAILED.value,
                    AgentTeamTaskStatus.CANCELLED.value,
                    AgentTeamTaskStatus.ABANDONED.value,
                    AgentTeamTaskStatus.COMPLETED.value,
                ]
            ),
        ]
        # 历史数据 pr_number 可能为空（列 nullable），无法精确关联时保持原
        # repo 级回退，宁可少处理也不错绑。
        if review.pr_number is not None:
            conditions.append(AgentTeamTask.source_issue_number == review.pr_number)
        statement = (
            select(AgentTeamTask)
            .where(*conditions)
            .order_by(AgentTeamTask.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(statement)
        return result.scalars().first()

    async def _feedback_exists(
        self,
        session: Any,
        task_id: int,
        external_id: str,
    ) -> bool:
        statement = (
            select(AgentTeamFeedback)
            .where(
                AgentTeamFeedback.task_id == task_id,
                AgentTeamFeedback.source
                == AgentTeamFeedbackSource.SAKURA_PR_REVIEW.value,
                AgentTeamFeedback.external_id == external_id,
            )
            .limit(1)
        )
        result = await session.execute(statement)
        return result.scalars().first() is not None

    async def _load_comments(self, session: Any, review_id: int) -> list[ReviewComment]:
        statement = (
            select(ReviewComment)
            .where(ReviewComment.review_id == review_id)
            .order_by(ReviewComment.id.asc())
        )
        result = await session.execute(statement)
        return list(result.scalars().all())

    async def _load_pass_score(self) -> int:
        fallback = 8
        try:
            fallback = int(
                getattr(get_settings(), "agent_team_pr_review_pass_score", 8)
            )
        except TypeError, ValueError:
            fallback = 8

        try:
            configured = await get_dynamic_config("agent_team_pr_review_pass_score")
        except Exception:
            return fallback
        if configured is None:
            return fallback
        try:
            return int(configured)
        except TypeError, ValueError:
            return fallback

    async def _load_blocking_severities(self) -> set[str]:
        fallback = {"critical", "major"}
        try:
            settings_value = getattr(
                get_settings(),
                "agent_team_pr_review_blocking_severities",
                None,
            )
            parsed_settings = parse_blocking_severities(settings_value)
            if parsed_settings:
                fallback = parsed_settings
        except Exception:
            fallback = {"critical", "major"}

        try:
            configured = await get_dynamic_config(
                "agent_team_pr_review_blocking_severities",
            )
        except Exception:
            return fallback
        parsed = parse_blocking_severities(configured)
        return parsed or fallback

    def _format_feedback(
        self,
        review: PRReview,
        comments: list[ReviewComment],
        outcome: AgentPRReviewOutcome,
    ) -> str:
        lines = [
            "Sakura PR Review completed.",
            f"Outcome: {outcome.value}",
            f"Review ID: {review.id}",
            f"Decision: {getattr(review, 'decision', None) or 'unknown'}",
        ]
        if getattr(review, "overall_score", None) is not None:
            lines.append(f"Overall score: {review.overall_score}")
        if getattr(review, "review_summary", None):
            lines.extend(["", "Summary:", str(review.review_summary)])

        if comments:
            lines.extend(["", "Comments:"])
            for index, comment in enumerate(comments, start=1):
                severity = getattr(comment, "severity", None) or "unknown"
                file_path = getattr(comment, "file_path", None)
                line_number = getattr(comment, "line_number", None)
                location = ""
                if file_path and line_number:
                    location = f" ({file_path}:{line_number})"
                elif file_path:
                    location = f" ({file_path})"
                lines.append(f"{index}. [{severity}]{location}")
                lines.append(str(getattr(comment, "content", None) or ""))

        return "\n".join(lines)
