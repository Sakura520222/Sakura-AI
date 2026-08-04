"""Persistent queue for PR review incremental synchronize events."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import desc, select

from backend.core.config import get_settings
from backend.models import database as db_module
from backend.models.database import PRReview, PRReviewIncrementalQueue, PRStatus
from backend.services.activity_observability.integration_service import (
    ActivityIntegrationService,
)


@dataclass
class PreparedIncrementalMessage:
    message: dict[str, Any]
    queue_ids: list[int]
    head_sha: str | None = None
    observability_session_id: int | None = None
    observability_trigger_ids: list[int] | None = None


class PRReviewIncrementalQueueService:
    """Manage synchronize events that arrive while a PR review is active."""

    @staticmethod
    def _repo_full_name(pr_info: dict[str, Any]) -> str:
        repo_full_name = pr_info.get("repo_full_name")
        if repo_full_name:
            return str(repo_full_name)
        return f"{pr_info.get('repo_owner', '')}/{pr_info.get('repo_name', '')}"

    @staticmethod
    def _head_sha(pr_info: dict[str, Any]) -> str | None:
        head_sha = pr_info.get("head_sha") or pr_info.get("after")
        return str(head_sha) if head_sha else None

    @staticmethod
    def _is_stale_review(review: PRReview) -> bool:
        """判断 active review 是否已超时成为僵尸（worker 很可能已死）。

        超过整体审查超时 review_timeout_seconds 仍处于 PENDING/REVIEWING 的 review，
        其 worker 极可能已异常退出或被取消但状态未收尾。此时不应再把新增量挂到它
        身上，否则增量会永远 pending（死锁）。
        """
        if not review.created_at:
            return False
        timeout_seconds = get_settings().review_timeout_seconds
        age = (datetime.utcnow() - review.created_at).total_seconds()
        return age > timeout_seconds

    async def find_active_review(self, pr_info: dict[str, Any]) -> PRReview | None:
        """Find the latest pending/reviewing DB review for this PR."""
        repo_owner = pr_info.get("repo_owner")
        repo_name = pr_info.get("repo_name")
        pr_id = pr_info.get("pr_id")
        if not repo_owner or not repo_name or pr_id is None:
            return None

        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReview)
                .where(
                    PRReview.repo_owner == repo_owner,
                    PRReview.repo_name == repo_name,
                    PRReview.pr_id == pr_id,
                    PRReview.status.in_(
                        [PRStatus.PENDING.value, PRStatus.REVIEWING.value]
                    ),
                )
                .order_by(desc(PRReview.created_at), desc(PRReview.id))
            )
            return result.scalars().first()

    async def enqueue_from_webhook(
        self,
        pr_info: dict[str, Any],
        delivery_id: str | None = None,
    ) -> PRReviewIncrementalQueue | None:
        """Persist a synchronize event when an active review exists."""
        active_review = await self.find_active_review(pr_info)
        if active_review is None:
            return None

        # 僵尸 review 检测：超时仍 PENDING/REVIEWING 视为 worker 已死，
        # 不挂载新增量，否则增量会永远 pending（死锁）。
        # 返回 None 让 webhook 改走 submit_review_task，新审查会消费 pending 增量。
        if self._is_stale_review(active_review):
            logger.warning(
                "PR 增量检测到僵尸 review (id={}, status={}, created_at={})，"
                "视为无 active review: {}#{}",
                active_review.id,
                active_review.status,
                active_review.created_at,
                self._repo_full_name(pr_info),
                pr_info.get("pr_number"),
            )
            return None

        head_sha = self._head_sha(pr_info)
        if not head_sha:
            logger.warning(
                "无法入队 PR 增量：缺少 head_sha repo={} pr={}",
                self._repo_full_name(pr_info),
                pr_info.get("pr_number"),
            )
            return None

        repo_full_name = self._repo_full_name(pr_info)
        pr_number = int(pr_info["pr_number"])
        observability_session_id = None
        observability_trigger_id = None
        if (
            pr_info.get("repository_external_id")
            and pr_info.get("source_system_instance")
            and delivery_id
        ):
            try:
                admission = await ActivityIntegrationService().admit_synchronize(
                    pr_info,
                    delivery_id=delivery_id,
                    base_sha=pr_info.get("before") or pr_info.get("base_sha"),
                    head_sha=head_sha,
                )
                observability_session_id = admission.session_id
                observability_trigger_id = admission.trigger_id
            except Exception as exc:
                logger.warning("PR 增量 observability admission skipped: {}", exc)
        async with db_module.async_session() as db:
            if delivery_id:
                existing_result = await db.execute(
                    select(PRReviewIncrementalQueue).where(
                        PRReviewIncrementalQueue.delivery_id == delivery_id
                    )
                )
                existing = existing_result.scalars().first()
                if existing:
                    return existing

            existing_result = await db.execute(
                select(PRReviewIncrementalQueue).where(
                    PRReviewIncrementalQueue.repo_full_name == repo_full_name,
                    PRReviewIncrementalQueue.pr_number == pr_number,
                    PRReviewIncrementalQueue.head_sha == head_sha,
                    PRReviewIncrementalQueue.status == "pending",
                )
            )
            existing = existing_result.scalars().first()
            if existing:
                return existing

            item = PRReviewIncrementalQueue(
                repo_owner=str(pr_info["repo_owner"]),
                repo_name=str(pr_info["repo_name"]),
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                base_sha=pr_info.get("before") or pr_info.get("base_sha"),
                head_sha=head_sha,
                delivery_id=delivery_id,
                observability_session_id=observability_session_id,
                observability_trigger_id=observability_trigger_id,
                status="pending",
                active_review_id=active_review.id,
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            logger.info(
                "PR 增量已入队: {}#{} {} -> {} active_review_id={}",
                repo_full_name,
                pr_number,
                item.base_sha,
                item.head_sha,
                active_review.id,
            )
            return item

    async def find_by_delivery_id(
        self, delivery_id: str | None
    ) -> PRReviewIncrementalQueue | None:
        """按 delivery_id 查找已入队的增量（用于配额扣费前去重）。

        GitHub 会重试 webhook 投递；同一 delivery_id 若已入队（任意状态），
        重试不应再次扣费或重复入队。
        """
        if not delivery_id:
            return None
        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue).where(
                    PRReviewIncrementalQueue.delivery_id == delivery_id
                )
            )
            return result.scalars().first()

    async def consume_pending_for_review(
        self,
        *,
        pr_info: dict[str, Any],
        review_id: int,
        session_id: int,
        repo: Any,
        consumed_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Merge pending increments into one user message and mark them consumed."""
        prepared = await self.prepare_pending_for_review(
            pr_info=pr_info,
            repo=repo,
        )
        if prepared is None:
            return None

        await self.mark_consumed(
            prepared.queue_ids,
            review_id=review_id,
            session_id=session_id,
            consumed_message_id=consumed_message_id,
        )
        return prepared.message

    async def prepare_pending_for_review(
        self,
        *,
        pr_info: dict[str, Any],
        repo: Any,
    ) -> PreparedIncrementalMessage | None:
        """Build a merged incremental message without changing queue state."""
        repo_full_name = self._repo_full_name(pr_info)
        pr_number = int(pr_info["pr_number"])

        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue)
                .where(
                    PRReviewIncrementalQueue.repo_full_name == repo_full_name,
                    PRReviewIncrementalQueue.pr_number == pr_number,
                    PRReviewIncrementalQueue.status == "pending",
                )
                .order_by(
                    PRReviewIncrementalQueue.created_at,
                    PRReviewIncrementalQueue.id,
                )
            )
            pending = list(result.scalars().all())
            if not pending:
                return None

            base_sha = pending[0].base_sha or pr_info.get("before")
            head_sha = pending[-1].head_sha
            if not base_sha or not head_sha:
                logger.warning(
                    "PR 增量队列缺少 compare SHA，保持 pending: {}#{}",
                    repo_full_name,
                    pr_number,
                )
                return None

            try:
                comparison = await asyncio.to_thread(repo.compare, base_sha, head_sha)
                message = self._build_incremental_user_message(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    base_sha=str(base_sha),
                    head_sha=str(head_sha),
                    comparison=comparison,
                    queue_items=pending,
                )
            except Exception as exc:
                logger.warning(
                    "生成 PR 增量 compare 失败，保持队列 pending: {}#{} {} -> {}: {}",
                    repo_full_name,
                    pr_number,
                    base_sha,
                    head_sha,
                    exc,
                )
                return None

            return PreparedIncrementalMessage(
                message=message,
                queue_ids=[int(item.id) for item in pending if item.id is not None],
                head_sha=str(head_sha),
                observability_session_id=(
                    int(pending[0].observability_session_id)
                    if pending[0].observability_session_id is not None
                    else None
                ),
                observability_trigger_ids=[
                    int(item.observability_trigger_id)
                    for item in pending
                    if item.observability_trigger_id is not None
                ],
            )

    async def mark_consumed(
        self,
        queue_ids: list[int],
        *,
        review_id: int,
        session_id: int,
        consumed_message_id: int | None = None,
    ) -> None:
        """Mark prepared queue rows as consumed after checkpoint persistence."""
        if not queue_ids:
            return

        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue).where(
                    PRReviewIncrementalQueue.id.in_(queue_ids),
                    PRReviewIncrementalQueue.status == "pending",
                )
            )
            pending = list(result.scalars().all())
            if not pending:
                return

            consumed_at = datetime.utcnow()
            for item in pending:
                item.status = "consumed"
                item.consumed_review_id = review_id
                item.consumed_session_id = session_id
                item.consumed_message_id = consumed_message_id
                item.consumed_at = consumed_at
            await db.commit()
            logger.info(
                "已消费 {} 条 PR 增量队列: {}#{} {} -> {} session_id={}",
                len(pending),
                pending[0].repo_full_name,
                pending[0].pr_number,
                pending[0].base_sha,
                pending[-1].head_sha,
                session_id,
            )

    async def mark_skipped_for_pr(self, repo_full_name: str, pr_number: int) -> int:
        """将 PR 的所有 pending 增量标记为 skipped（终态，无需 review 行）。

        用于 drained synchronize 任务命中 should_skip（如纯文档增量）时收尾：
        避免队列行与新 head 的 queued check run 永久残留 pending。区别于
        cancel_pending_for_pr（PR 关闭语义）与 mark_consumed（已挂载到 review）。

        Returns:
            被标记为 skipped 的增量条数
        """
        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue).where(
                    PRReviewIncrementalQueue.repo_full_name == repo_full_name,
                    PRReviewIncrementalQueue.pr_number == pr_number,
                    PRReviewIncrementalQueue.status == "pending",
                )
            )
            pending = list(result.scalars().all())
            if not pending:
                return 0
            consumed_at = datetime.utcnow()
            for item in pending:
                item.status = "skipped"
                item.consumed_at = consumed_at
            await db.commit()
            logger.info(
                "已跳过 {} 条 PR 增量队列（drain skip）: {}#{}",
                len(pending),
                repo_full_name,
                pr_number,
            )
            return len(pending)

    async def cancel_pending_for_pr(
        self,
        repo_full_name: str,
        pr_number: int,
    ) -> int:
        """PR 关闭/合并时，将其所有 pending 增量标记为 cancelled。

        避免 pending 增量永久残留，以及 PR 重开时被新审查误消费（过时上下文污染）。

        Returns:
            被取消的增量条数
        """
        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue).where(
                    PRReviewIncrementalQueue.repo_full_name == repo_full_name,
                    PRReviewIncrementalQueue.pr_number == pr_number,
                    PRReviewIncrementalQueue.status == "pending",
                )
            )
            pending = list(result.scalars().all())
            if not pending:
                return 0
            for item in pending:
                item.status = "cancelled"
            await db.commit()
            logger.info(
                "PR 关闭，取消 {} 条 pending 增量: {}#{}",
                len(pending),
                repo_full_name,
                pr_number,
            )
            return len(pending)

    async def list_pending(
        self,
        pr_info: dict[str, Any],
    ) -> list[PRReviewIncrementalQueue]:
        """返回该 PR 所有 pending 增量（按入队时间排序）。"""
        repo_full_name = self._repo_full_name(pr_info)
        pr_number = int(pr_info["pr_number"])
        async with db_module.async_session() as db:
            result = await db.execute(
                select(PRReviewIncrementalQueue)
                .where(
                    PRReviewIncrementalQueue.repo_full_name == repo_full_name,
                    PRReviewIncrementalQueue.pr_number == pr_number,
                    PRReviewIncrementalQueue.status == "pending",
                )
                .order_by(
                    PRReviewIncrementalQueue.created_at,
                    PRReviewIncrementalQueue.id,
                )
            )
            return list(result.scalars().all())

    def _build_incremental_user_message(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
        comparison: Any,
        queue_items: list[PRReviewIncrementalQueue],
    ) -> dict[str, Any]:
        files = list(getattr(comparison, "files", []) or [])
        commits = list(getattr(comparison, "commits", []) or [])

        sections = [
            "=== BEGIN UNTRUSTED INCREMENTAL REVIEW EVIDENCE ===",
            (
                "New commits were pushed while this PR review was already running. "
                "Review only the newly added changes and update any earlier "
                "conclusions that these changes affect."
            ),
            "",
            f"Repository: {repo_full_name}",
            f"Pull Request: #{pr_number}",
            f"Compare range: {base_sha}...{head_sha}",
            f"Queued events merged: {len(queue_items)}",
            "",
        ]

        if commits:
            sections.append("Commits:")
            for commit in commits:
                sections.append(self._format_commit(commit))
            sections.append("")

        sections.append(f"Changed files ({len(files)}):")
        for file in files:
            filename = getattr(file, "filename", "")
            status = getattr(file, "status", "")
            additions = getattr(file, "additions", 0)
            deletions = getattr(file, "deletions", 0)
            sections.append(f"- {filename} ({status}, +{additions}/-{deletions})")
        sections.append("")

        sections.append("Diffs:")
        for file in files:
            filename = getattr(file, "filename", "")
            status = getattr(file, "status", "")
            additions = getattr(file, "additions", 0)
            deletions = getattr(file, "deletions", 0)
            patch = getattr(file, "patch", None)
            sections.append(f"### {filename} ({status}, +{additions}/-{deletions})")
            if patch:
                sections.append("```diff")
                sections.append(str(patch))
                sections.append("```")
            else:
                sections.append(
                    "(No textual patch is available for this file; use tools if needed.)"
                )
            sections.append("")

        sections.append(
            "Do not repeat prior findings unless this incremental range changes "
            "their severity, validity, or fix status."
        )
        sections.append("=== END UNTRUSTED INCREMENTAL REVIEW EVIDENCE ===")

        return {"role": "user", "content": "\n".join(sections)}

    @staticmethod
    def _format_commit(commit: Any) -> str:
        sha = str(getattr(commit, "sha", ""))[:8]
        commit_obj = getattr(commit, "commit", None)
        message = str(getattr(commit_obj, "message", "") or "").strip()
        title = message.splitlines()[0] if message else "(no commit message)"
        author_obj = getattr(commit_obj, "author", None)
        author = getattr(author_obj, "name", None) or "Unknown"
        return f"- {sha} {title} ({author})"
