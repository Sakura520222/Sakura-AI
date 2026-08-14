"""审查任务Worker"""

import asyncio
import subprocess
import time
import uuid
from typing import Any

from loguru import logger
from sqlalchemy.exc import InterfaceError, OperationalError

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.config import (
    get_dynamic_config,
    get_settings,
    get_strategy_config,
    get_user_dynamic_config,
)
from backend.core.github_app import GitHubAppClient
from backend.core.time_service import now_utc
from backend.models.database import (
    CommentSeverity,
    CommentType,
    PRIssueLink,
    PRReview,
    PRStatus,
    ReviewComment,
    ReviewDecision,
    ReviewStrategy,
)
from backend.services.ai_reviewer import AIReviewer
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.services.check_run_service import (
    CheckRunService,
    ReviewProgressSnapshot,
    ReviewRunKey,
)
from backend.services.comment_service import CommentService
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeAdmissionClosed,
    ensure_background_admission,
    register_background_task,
)
from backend.services.decision_engine import get_decision_engine
from backend.services.label_service import label_service
from backend.services.pr_analyzer import PRAnalysis, PRAnalyzer

settings = get_settings()

# 审查并发控制信号量
_review_semaphore: asyncio.Semaphore | None = None


async def _get_review_semaphore() -> asyncio.Semaphore:
    """获取审查并发信号量（懒初始化，支持动态更新）"""
    global _review_semaphore
    if _review_semaphore is None:
        max_concurrent = await _load_max_concurrent()
        _review_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info("审查并发信号量初始化: 最大 {} 个并发任务", max_concurrent)
    return _review_semaphore


def reset_review_semaphore():
    """重置审查信号量（配置更新时调用）"""
    global _review_semaphore
    _review_semaphore = None
    logger.info("审查并发信号量已重置，下次任务将重新初始化")


async def _load_max_concurrent() -> int:
    """从动态配置读取最大并发审查数"""
    try:
        val = await get_dynamic_config("max_concurrent_reviews")
        return int(val) if val is not None else get_settings().max_concurrent_reviews
    except Exception as e:
        logger.warning("读取 max_concurrent_reviews 配置失败，使用默认值: {}", e)
        return get_settings().max_concurrent_reviews


def _get_label_rec_setting(key: str, default=None):
    """获取标签推荐配置，读取失败时降级到默认值"""
    try:
        from backend.core.config import get_label_config

        return get_label_config().get_recommendation_settings().get(key, default)
    except (OSError, AttributeError) as e:
        logger.debug("读取标签推荐配置 [{}] 失败，使用降级值: {}", key, e)
        return default


def _make_label_event_callback(label_execution, task_id):
    """构造标签推荐可观测回调，把请求/响应消息写入辅助 summary Thread。

    Build a best-effort callback that persists label-recommendation message
    events onto the auxiliary summary thread so the live activity monitor can
    surface distinguishable "label recommendation request/response" cards.
    Only ``message`` events are forwarded; observability write failures are
    swallowed so they never break the label recommendation flow.
    """
    thread = label_execution.thread
    work_unit = label_execution.work_unit
    tool_service = label_execution.tool_service
    lease = label_execution.lease

    async def _callback(event_type, data):
        if event_type != "message":
            return
        try:
            await tool_service.append_conversation_message(
                thread_id=thread.id,
                work_unit_id=work_unit.id,
                message=data,
                lease=lease,
            )
        except Exception as exc:
            logger.debug("[{}] 标签推荐可观测回调失败: {}", task_id, exc)

    return _callback


def get_async_session():
    """获取异步会话工厂（动态导入）"""
    from backend.models.database import async_session

    if async_session is None:
        raise RuntimeError("数据库未初始化，请确保 init_db() 已被调用")
    return async_session


async def _db_retry(func, max_retries=3, delay=1):
    """数据库操作重试，处理连接断开的情况"""
    for attempt in range(max_retries):
        try:
            return await func()
        except (OperationalError, InterfaceError) as e:
            error_str = str(e).lower()
            is_connection_error = any(
                keyword in error_str
                for keyword in [
                    "lost connection",
                    "server has gone away",
                    "connection was killed",
                    "timeout",
                    "pool exhausted",
                    "can't connect",
                ]
            )

            if is_connection_error and attempt < max_retries - 1:
                logger.warning(
                    f"数据库连接异常，第{attempt + 1}次重试（共{max_retries}次）: {e}"
                )
                await asyncio.sleep(delay * (attempt + 1))
                continue
            raise


class ReviewWorker:
    """审查任务Worker"""

    def __init__(self):
        self.github_app = GitHubAppClient()
        self.analyzer = PRAnalyzer()
        self.ai_reviewer = AIReviewer()
        self.comment_service = CommentService()
        self.check_run_service = CheckRunService()
        self._background_tasks: set = set()
        # Cancel signal management: task_key -> asyncio.Event
        # Shared by all tasks for the same PR (owner/repo#pr_number)
        # Thread-safety: dict operations are atomic under GIL; asyncio.Event
        # is signal-only (set/check), safe for concurrent async coroutines.
        self._cancel_events: dict[str, asyncio.Event] = {}
        # 任务阶段仅用于将 AI 审查预算与 GitHub reporting 收尾隔离：一旦审查
        # 结果已落库并进入 reporting，整体 AI 审查预算不再中断发布收尾。
        self._task_stages: dict[str, str] = {}
        # The new observability admission/orchestration boundary is injected at
        # the worker edge; legacy checkpoint objects remain compatibility-only.
        from backend.services.activity_observability.integration_service import (
            ActivityIntegrationService,
        )

        self.activity_integration = ActivityIntegrationService()

    def _normalize_review_result_for_diff(
        self,
        review_result: dict[str, Any],
        analysis: PRAnalysis | None,
        task_id: str,
    ) -> dict[str, Any]:
        """Filter GitHub-submittable inline comments without changing findings."""
        inline_comments = review_result.get("inline_comments", [])
        if (
            not inline_comments
            or not analysis
            or not getattr(analysis, "changed_lines_map", None)
        ):
            return review_result

        validated_comments = self.comment_service._validate_inline_comments(
            inline_comments, analysis
        )

        normalized_result = dict(review_result)
        normalized_result["review_body_inline_comments"] = list(
            review_result.get("review_body_inline_comments", inline_comments)
        )
        normalized_result["inline_comments"] = validated_comments

        # 验证后数量未减少，无需额外日志
        if len(validated_comments) == len(inline_comments):
            return normalized_result

        logger.info(
            "[{}] 过滤掉 {} 条无法作为 GitHub 行内评论提交的评论，报告汇总保留原始 findings",
            task_id,
            len(inline_comments) - len(validated_comments),
        )
        return normalized_result

    @staticmethod
    async def _log_activity(
        review_id: int | None,
        event_type: str,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Legacy activity event hook — now a no-op.

        The new observability system (ActivityOutbox + user-scoped SSE, driven by
        ``execution.finish`` and the Attempt observer) replaces the legacy
        ``activity_events`` table and global ``activity:*`` SSE channel. Retained
        as a shim so existing call sites remain harmless; it writes nothing.
        """
        return

    @staticmethod
    def _make_task_key(pr_info: dict[str, Any]) -> str:
        """Generate unique task key: owner/repo#pr_number"""
        return f"{pr_info['repo_full_name']}#{pr_info['pr_number']}"

    async def _inject_external_ci_failures(
        self,
        context: dict[str, Any],
        pr_info: dict[str, Any],
        task_id: str,
    ) -> None:
        """注入外部 CI 失败上下文（失败不影响主审查流程）。

        CI 失败由 check_run/workflow_job webhook 事件驱动预先采集到数据库；
        这里在审查启动时按 repo + head_sha 读取快照并放入 context。
        """
        try:
            from backend.services.ci_failure_service import CIFailureService

            # 增量审查（synchronize）时 after 是新 head，优先取以读取新提交的 CI 失败；
            # 首次审查（opened）无 after，回退到 head_sha。
            head_sha = pr_info.get("after") or pr_info.get("head_sha")
            if not head_sha:
                return
            ci_failures = await CIFailureService().fetch_for_review(
                pr_info["repo_full_name"], head_sha
            )
            if ci_failures:
                context["external_ci_failures"] = ci_failures
                logger.info(
                    "[{}] 已注入 {} 条外部 CI 失败记录",
                    task_id,
                    len(ci_failures),
                )
        except Exception as e:
            logger.warning(
                f"[{task_id}] 外部 CI 失败注入失败（不影响审查）: {e}",
                exc_info=True,
            )

    def _register_task(self, task_key: str, force_new: bool = False) -> asyncio.Event:
        """Register a review task and return its cancel event.

        Idempotent: multiple registrations for the same task_key share the same
        event — any cancellation signal affects all of them.

        If an existing event is already set (stale from a cancelled task),
        create a fresh one so the new task doesn't inherit the cancelled state.

        Args:
            task_key: Unique key for the PR (owner/repo#pr_number)
            force_new: If True, always create a fresh event (used by
                       submit_review_task to ensure a clean state before
                       the coroutine starts executing).
        """
        if not hasattr(self, "_task_stages"):
            self._task_stages = {}
        if force_new:
            event = asyncio.Event()
            self._cancel_events[task_key] = event
            self._task_stages[task_key] = "reviewing"
            return event
        existing = self._cancel_events.get(task_key)
        if existing and not existing.is_set():
            return existing
        # Always create a fresh event (covers: no existing, or stale set event)
        event = asyncio.Event()
        self._cancel_events[task_key] = event
        self._task_stages[task_key] = "reviewing"
        return event

    def _unregister_task(self, task_key: str):
        """Remove task state after task completes or fails."""
        self._cancel_events.pop(task_key, None)
        getattr(self, "_task_stages", {}).pop(task_key, None)

    def _mark_task_reporting(self, task_key: str) -> None:
        """Mark that AI review results are durable and GitHub publication began."""
        if not hasattr(self, "_task_stages"):
            self._task_stages = {}
        self._task_stages[task_key] = "reporting"

    def is_task_reporting(self, task_key: str) -> bool:
        """Return whether a task has entered the post-analysis reporting stage."""
        return getattr(self, "_task_stages", {}).get(task_key) == "reporting"

    def cancel_task(self, task_key: str) -> bool:
        """Signal cancellation for a PR's review task(s). Called from webhook.

        Returns True if there was an active task to cancel.
        """
        event = self._cancel_events.get(task_key)
        if event and not event.is_set():
            event.set()
            logger.info("[cancel] 已设置取消信号: {}", task_key)
            return True
        return False

    def _check_cancelled(self, task_key: str) -> bool:
        """Check if cancellation has been signaled for this task"""
        event = self._cancel_events.get(task_key)
        return event is not None and event.is_set()

    async def _cancel_and_cleanup(
        self,
        task_id: str,
        task_key: str,
        review_obj,
        review_id: int | None,
        reason: str,
        pr_info: dict[str, Any] | None = None,
        output_language: str | None = None,
        head_sha: str | None = None,
    ):
        """Common cleanup for all cancel checkpoints.

        Deletes placeholder comment and updates DB status to CANCELLED.
        Safe to call when review_obj/review_id are None (early checkpoints).
        Also updates the GitHub Check Run to cancelled; head_sha 显式传入以跟踪
        增量消费后的最新 head（pr_info["head_sha"] 是审查开始时的静态值，增量
        推进后会过时，用它会在旧 commit 留下悬挂 check run）。
        """
        logger.info("[{}] 任务已被取消，{}: {}", task_id, reason, task_key)
        if review_obj:
            await self.comment_service.delete_placeholder_comment(review_obj)
        if review_id:
            await self._update_review_status(review_id, PRStatus.CANCELLED)
        if pr_info and head_sha:
            await self.check_run_service.cancel_active_runs_by_sha(
                pr_info["repo_owner"],
                pr_info["repo_name"],
                head_sha,
                cancel_reason="worker_cancelled",
                output_language=output_language,
            )
        return task_id

    @staticmethod
    def _count_severity(inline_comments: list) -> dict[str, int]:
        """从 inline_comments 统计 severity 分级（主 Review 与 Findings 同源取数）。"""
        counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
        for c in inline_comments:
            sev = (
                (c.get("severity") or "minor").lower()
                if isinstance(c, dict)
                else "minor"
            )
            if sev not in counts:
                sev = "minor"
            counts[sev] += 1
        return counts

    @staticmethod
    def _infer_failed_stage(check_run_stages: list[str]) -> str:
        """据已完成阶段推断失败阶段（最后一个完成阶段的下一阶段）。"""
        order = ["fetching", "indexing", "summary", "reviewing", "reporting"]
        if not check_run_stages:
            return "fetching"
        last = check_run_stages[-1]
        if last in order:
            idx = order.index(last)
            return order[idx + 1] if idx + 1 < len(order) else "reporting"
        return "reviewing"

    async def _persist_review_check_run_ids(
        self, review_id: Any, run_key: ReviewRunKey
    ) -> None:
        """持久化三 Check 的 run_id 到 PRReview（仅写非空且 DB 未存的字段，不覆盖）。

        跨进程恢复主索引；external_id 兜底已处理恢复，此处仅作性能优化 +
        审计记录。异常吞掉，不影响主流程。
        """
        if not review_id:
            return
        try:
            from backend.models import database as _db
            from backend.models.database import PRReview as _PRReview

            svc = self.check_run_service
            _ids = {
                "review_check_run_id": svc.get_cached_check_run_id(
                    run_key, svc.CHECK_RUN_NAME_REVIEW
                ),
                "analysis_check_run_id": svc.get_cached_check_run_id(
                    run_key, svc.CHECK_RUN_NAME_ANALYSIS
                ),
                "findings_check_run_id": svc.get_cached_check_run_id(
                    run_key, svc.CHECK_RUN_NAME_FINDINGS
                ),
            }
            async with _db.async_session() as session:
                row = await session.get(_PRReview, review_id)
                if row is None:
                    return
                _changed = False
                for _col, _id in _ids.items():
                    if _id is not None and getattr(row, _col, None) is None:
                        setattr(row, _col, _id)
                        _changed = True
                if _changed:
                    await session.commit()
        except Exception as exc:
            logger.debug("持久化 check_run_id 失败: {}", exc)

    async def _persist_error_reference(
        self, review_id: Any, error_reference: str | None, error_summary: str = ""
    ) -> None:
        """持久化脱敏故障编号到 PRReview（仅写非空且未存的字段）。

        完整异常堆栈在日志（带 error_reference tag）；此处只存短编号 + 摘要便于检索。
        """
        if not review_id or not error_reference:
            return
        try:
            from backend.models import database as _db
            from backend.models.database import PRReview as _PRReview

            async with _db.async_session() as session:
                row = await session.get(_PRReview, review_id)
                if row is not None and not row.error_reference:
                    row.error_reference = error_reference
                    if error_summary:
                        row.error_summary = error_summary[:255]
                    await session.commit()
        except Exception as exc:
            logger.debug("持久化 error_reference 失败: {}", exc)

    async def process_review_task(self, pr_info: dict[str, Any]) -> str:
        """处理审查任务"""
        task_id = str(uuid.uuid4())[
            :8
        ]  # 日志追踪用短 ID，碰撞概率约 1/43亿，不用于持久化唯一键
        task_key = self._make_task_key(pr_info)
        review_obj = None  # 用于保存 GitHub Review 对象
        review_id = None  # 用于保存数据库审查记录 ID
        execution = None
        execution_status = None
        execution_target_status = None
        output_language = None  # 用户级输出语言；异常路径会复用该值
        head_sha = pr_info.get("head_sha")  # 审查绑定的 head；增量消费后切换到新 commit
        # try 外预初始化，防止异常路径（analyze_pr / _create_review_record 等早期失败）
        # 引用未定义的 check_run_stages，UnboundLocalError 会掩盖原始异常并跳过
        # 失败 Check Run / error_reference 收敛
        check_run_stages: list[str] = []

        async def _finish_execution(
            status: str, *, error_message: str | None = None
        ) -> None:
            """Best-effort finish; retain the status until the operation succeeds."""
            nonlocal execution_status, execution_target_status
            if execution is None or execution_status == status:
                return
            execution_target_status = status
            try:
                await execution.finish(status, error_message=error_message)
            except Exception as exc:
                logger.warning(
                    "[{}] observability execution finish failed (status={}): {}",
                    task_id,
                    status,
                    exc,
                )
                return
            execution_status = status

        # Cancel event was already registered in submit_review_task
        # before asyncio.create_task, so cancel_task works immediately

        # 获取并发信号量，限制同时运行的审查任务数
        semaphore = await _get_review_semaphore()
        async with semaphore:
            try:
                logger.info(
                    f"[{task_id}] 开始处理审查任务: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )

                # Check if already cancelled (e.g. PR closed while queued)
                # Note: review_id is None at this point, no DB record to update
                if self._check_cancelled(task_key):
                    return await self._cancel_and_cleanup(
                        task_id,
                        task_key,
                        None,
                        None,
                        "PR 已关闭/合并，跳过审查",
                        pr_info=pr_info,
                        output_language=output_language,
                        head_sha=head_sha,
                    )

                # Admit and immediately start the new invocation lane.  Once
                # admission succeeds, observability failures are fail-closed;
                # never continue the external GitHub publication path as legacy.
                admission = await self.activity_integration.admit(
                    pr_info,
                    trigger_kind=(
                        "synchronize"
                        if pr_info.get("action") == "synchronize"
                        else "manual"
                    ),
                    delivery_id=pr_info.get("delivery_id"),
                    actor_id=str(
                        pr_info.get("user_id") or pr_info.get("sender") or "worker"
                    ),
                    manual_nonce=task_id,
                    base_sha=pr_info.get("before") or pr_info.get("base_sha"),
                    head_sha=pr_info.get("after") or head_sha,
                )
                role_snapshot = None
                resolver = getattr(
                    getattr(self.ai_reviewer, "api_client", None),
                    "resolve_role_config_snapshot",
                    None,
                )
                if resolver is not None:
                    role_snapshot = await resolver("main")
                execution = await self.activity_integration.start_execution(
                    session_id=admission.session_id,
                    trigger_ids=[admission.trigger_id],
                    role_snapshot=role_snapshot,
                    role="main",
                    task_type="pr",
                    task_id=None,
                )
                if getattr(execution, "merged", False):
                    logger.info(
                        "[{}] Trigger 已合并到正在运行的 Invocation {}，当前 Worker 不重复执行",
                        task_id,
                        execution.invocation_id,
                    )
                    return task_id

                # 1. 分析PR
                analysis = await self.analyzer.analyze_pr(pr_info)

                # 2. 检查是否应该跳过
                if analysis.should_skip:
                    logger.info("[{}] 跳过审查: {}", task_id, analysis.skip_reason)
                    skip_lang = await get_user_dynamic_config(
                        "output_language", pr_info.get("user_id")
                    )
                    await self._save_skip_record(analysis, pr_info)
                    # 增量（synchronize）命中跳过时，必须终结 pending 增量队列，
                    # 否则 drained 任务跳过后队列行会永久残留
                    # （_drain_pending_incremental 对 synchronize 不再兜底）。
                    if pr_info.get("action") == "synchronize":
                        try:
                            from backend.services.pr_review_incremental_queue import (
                                PRReviewIncrementalQueueService,
                            )

                            skipped = await PRReviewIncrementalQueueService().mark_skipped_for_pr(
                                pr_info["repo_full_name"],
                                int(pr_info["pr_number"]),
                            )
                            if skipped:
                                logger.info(
                                    "[{}] 增量跳过审查，已终结 {} 条 pending 增量队列",
                                    task_id,
                                    skipped,
                                )
                        except Exception as e:
                            logger.warning(
                                "[{}] mark_skipped_for_pr 失败（队列可能残留）: {}",
                                task_id,
                                e,
                            )
                    # 增量任务的 head_sha 可能仍是完整审查的旧 head，优先用 after
                    # （增量新 head）收尾 check run，避免新 head 的 queued check 残留。
                    _skip_sha = pr_info.get("after") or pr_info.get("head_sha")
                    if _skip_sha:
                        await self.check_run_service.report_skipped(
                            ReviewRunKey(
                                repo_full_name=pr_info.get("repo_full_name")
                                or f"{pr_info['repo_owner']}/{pr_info['repo_name']}",
                                pr_number=pr_info.get("pr_number", 0),
                                head_sha=_skip_sha,
                                review_job_id="skip",
                            ),
                            reason=analysis.skip_reason or "",
                            output_language=skip_lang,
                        )
                    await _finish_execution("completed")
                    return task_id
                if self._check_cancelled(task_key):
                    await _finish_execution("cancelled")
                    return await self._cancel_and_cleanup(
                        task_id,
                        task_key,
                        None,
                        None,
                        "跳过代码索引",
                        pr_info=pr_info,
                        output_language=output_language,
                        head_sha=head_sha,
                    )

                # 3. 创建数据库记录（尽早落库 PENDING）：增量队列的
                # find_active_review 依赖 PRReview 行的存在来判定"是否有活跃审查"。
                # 若延后到代码索引之后（索引耗时数十秒），此窗口内到达的
                # synchronize webhook 会查不到 active review，enqueue 返回 None，
                # 从而误触发第二个完整审查，造成并发 + 限流雪崩。
                review_id = await self._create_review_record(analysis, pr_info, task_id)

                # 提前获取用户输出语言：Check Run / 占位评论 / 审查上下文均依赖它。
                # get_user_dynamic_config 带缓存，提前调用零成本。
                output_language = await get_user_dynamic_config(
                    "output_language", pr_info.get("user_id")
                )
                # Check Run 进度追踪：记录已完成的阶段，供后续 report_stage_progress 展示
                check_run_stages: list[str] = []

                # ReviewRunKey：执行上下文标识（repo/pr/sha/review_job_id），
                # 驱动多 Check 定位/收敛/external_id 关联。review_job_id = PRReview.id。
                run_key = ReviewRunKey(
                    repo_full_name=pr_info.get("repo_full_name")
                    or f"{pr_info['repo_owner']}/{pr_info['repo_name']}",
                    pr_number=pr_info.get("pr_number", 0),
                    head_sha=head_sha or "",
                    review_job_id=str(review_id),
                )

                # 创建 GitHub Check Run（queued），将审查进度可视化到 Checks 面板
                if head_sha:
                    await self.check_run_service.report_queued(
                        run_key,
                        pr_number=pr_info.get("pr_number"),
                        output_language=output_language,
                    )
                    # fetching 阶段：PR 元信息/diff/关联 Issue 已获取（pr_info 齐全）
                    await self.check_run_service.report_stage_progress(
                        run_key,
                        stage="fetching",
                        completed_stages=list(check_run_stages),
                        output_language=output_language,
                    )
                    check_run_stages.append("fetching")

                # 记录审查创建前的关键阶段（review_id 此时有效）
                await self._log_activity(
                    review_id,
                    "thinking",
                    {
                        "message": f"分析 PR #{pr_info.get('pr_number')} ...",
                        "repo_full_name": pr_info.get("repo_full_name"),
                    },
                )
                await self._log_activity(
                    review_id,
                    "status",
                    {
                        "status": "pending",
                        "message": f"PR #{pr_info.get('pr_number')} 审查已创建",
                        "strategy": analysis.strategy,
                        "repo_full_name": pr_info.get("repo_full_name"),
                    },
                )

                # 2.5 代码索引（在 AI 审查前完成，确保 search_code_context 工具可用）
                if settings.auto_index_pr_changes and settings.enable_code_index:
                    try:
                        from backend.services.pr_code_indexer import get_pr_code_indexer

                        indexer = get_pr_code_indexer()
                        logger.info("[{}] 开始代码索引...", task_id)
                        if head_sha:
                            await self.check_run_service.report_stage_progress(
                                run_key,
                                stage="indexing",
                                completed_stages=list(check_run_stages),
                                output_language=output_language,
                            )
                            check_run_stages.append("indexing")
                        await self._log_activity(
                            review_id,
                            "tool_call",
                            {
                                "tool": "index_pr_changes",
                                "status": "running",
                                "detail": pr_info["repo_full_name"],
                            },
                        )
                        await indexer.index_pr_changes(
                            repo_full_name=pr_info["repo_full_name"],
                            pr_number=pr_info["pr_number"],
                            install_id=pr_info.get("install_id", 0),
                        )
                        logger.info("[{}] 代码索引完成", task_id)
                        await self._log_activity(
                            review_id,
                            "tool_result",
                            {
                                "tool": "index_pr_changes",
                                "status": "completed",
                                "detail": "代码索引完成",
                            },
                        )
                    except Exception as e:
                        logger.warning(
                            "[{}] 代码索引失败（将继续审查）: {}",
                            task_id,
                            str(e),
                        )

                # 2.6 文档索引（如果启用 RAG 且仓库尚未索引过文档）
                if settings.enable_rag:
                    try:
                        from backend.services.rag_service import get_rag_service

                        rag_service = get_rag_service()
                        doc_count = await rag_service.vector_store.get_collection_count(
                            pr_info["repo_full_name"]
                        )

                        if doc_count == 0:
                            import shutil
                            import tempfile

                            logger.info(
                                f"[{task_id}] 仓库尚无文档索引，开始自动索引 .sakura/ 文档..."
                            )
                            temp_dir = tempfile.mkdtemp()
                            try:
                                client = self.github_app.get_repo_client(
                                    pr_info["repo_owner"], pr_info["repo_name"]
                                )
                                repo = await asyncio.to_thread(
                                    client.get_repo, pr_info["repo_full_name"]
                                )
                                # 获取 installation access token 用于 git clone 认证
                                installation = (
                                    self.github_app.integration.get_installation(
                                        owner=pr_info["repo_owner"],
                                        repo=pr_info["repo_name"],
                                    )
                                )
                                auth_token = (
                                    self.github_app.integration.get_access_token(
                                        installation.id
                                    )
                                )
                                clone_url = repo.clone_url.replace(
                                    "https://",
                                    f"https://x-access-token:{auth_token.token}@",
                                )
                                await asyncio.to_thread(
                                    subprocess.run,
                                    [
                                        "git",
                                        "clone",
                                        "--depth",
                                        "1",
                                        clone_url,
                                        temp_dir,
                                    ],
                                    check=True,
                                    capture_output=True,
                                    timeout=60,
                                )
                                result = await rag_service.index_repository_docs(
                                    pr_info["repo_full_name"], temp_dir
                                )
                                logger.info(
                                    f"[{task_id}] 文档索引完成: {result['total_files']} 文件, {result['total_chunks']} 块"
                                )
                            finally:
                                shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception as e:
                        logger.warning(
                            f"[{task_id}] 文档自动索引失败（将继续审查）: {e}"
                        )

                # 3. 创建数据库记录（已在代码索引前尽早完成，见上方）

                # 4. 获取PR对象用于后续操作
                client = self.github_app.get_repo_client(
                    pr_info["repo_owner"], pr_info["repo_name"]
                )
                repo = await asyncio.to_thread(
                    client.get_repo, pr_info["repo_full_name"]
                )
                pr = await asyncio.to_thread(repo.get_pull, pr_info["pr_number"])

                # Cancel checkpoint: before PR summary and AI review
                if self._check_cancelled(task_key):
                    await _finish_execution("cancelled")
                    return await self._cancel_and_cleanup(
                        task_id,
                        task_key,
                        review_obj,
                        review_id,
                        "跳过 PR 总结和 AI 审查",
                        pr_info=pr_info,
                        output_language=output_language,
                        head_sha=head_sha,
                    )

                # 保留刷新步骤以兼容长生命周期 AIReviewer，但不读取旧辅助配置。
                self.ai_reviewer._refresh_ai_clients()

                # 4.5 PR 变更总结（如果启用）
                pr_summary_text = None
                if settings.enable_pr_summary:
                    try:
                        from backend.services.ai_reviewer.pr_summary import (
                            PRSummaryService,
                        )

                        summary_service = PRSummaryService(
                            self.ai_reviewer.api_client,
                            model="",
                        )
                        if head_sha:
                            await self.check_run_service.report_stage_progress(
                                run_key,
                                stage="summary",
                                completed_stages=list(check_run_stages),
                                output_language=output_language,
                            )
                            check_run_stages.append("summary")
                        summary = await summary_service.generate_summary(
                            analysis, pr_info, pr
                        )
                        await summary_service.update_pr_body(pr, summary)
                        pr_summary_text = summary
                        logger.info("[{}] PR 变更总结已更新", task_id)
                    except Exception as e:
                        logger.warning("[{}] PR 变更总结生成失败: {}", task_id, str(e))

                # 4.6 PR 依赖图生成（如果启用）
                if settings.enable_pr_dependency_graph:
                    try:
                        from backend.services.ai_reviewer.pr_dependency_graph import (
                            PRDependencyGraphService,
                        )

                        depgraph_service = PRDependencyGraphService(
                            self.ai_reviewer.api_client,
                            model="",
                        )
                        await depgraph_service.generate_dependency_graph(
                            analysis, pr_info, pr
                        )
                        logger.info("[{}] PR 依赖图已生成并注入", task_id)
                    except Exception as e:
                        logger.warning(
                            "[{}] PR 依赖图生成失败（不影响审查）: {}", task_id, e
                        )

                # output_language 已在 _create_review_record 之后提前获取

                # 5. 【第一阶段】创建占位评论
                logger.info("[{}] 创建占位评论...", task_id)
                review_obj = await self.comment_service.create_placeholder_comment(
                    pr, analysis.strategy, output_language=output_language
                )

                # 6. 准备审查上下文
                context = await self.analyzer.prepare_review_context(analysis, pr)
                context["user_id"] = pr_info.get("user_id")
                context["output_language"] = output_language

                # 6.1 注入 PR 变更总结到审查上下文
                if pr_summary_text:
                    context["pr_summary"] = pr_summary_text

                # 6.2 注入 .sakura/ 记忆上下文 / Inject .sakura/ memory context
                try:
                    from backend.services.sakura_memory_service import (
                        get_sakura_memory_service,
                    )

                    sakura_memory_service = get_sakura_memory_service()
                    sakura_context = await sakura_memory_service.get_sakura_context(
                        repo=pr.base.repo,
                        repo_full_name=pr_info["repo_full_name"],
                    )
                    if sakura_context:
                        context["sakura_docs_context"] = sakura_context
                        parts = []
                        if "sakura_md" in sakura_context:
                            parts.append(
                                f"SAKURA.md({len(sakura_context['sakura_md'])}字)"
                            )
                        if "memory_md" in sakura_context:
                            parts.append(
                                f"memory.md({len(sakura_context['memory_md'])}字)"
                            )
                        logger.info(
                            "[{}] 已注入 .sakura/ 记忆上下文: {}",
                            task_id,
                            ", ".join(parts) or "空",
                        )
                except Exception as e:
                    logger.warning(
                        f"[{task_id}] .sakura/ 记忆上下文注入失败（不影响审查）: {e}",
                        exc_info=True,
                    )

                # 6.3 注入外部 CI 失败（由 check_run/workflow_job webhook 预先采集）
                await self._inject_external_ci_failures(context, pr_info, task_id)

                # 6.5 解析并注入 Issue 上下文（如果启用）
                if (
                    hasattr(settings, "enable_pr_issue_linking")
                    and settings.enable_pr_issue_linking
                ):
                    try:
                        from backend.services.pr_issue_linker import PRIssueLinker

                        issue_linker = PRIssueLinker()

                        pr_body = pr_info.get("body", "") or ""
                        issue_numbers = await issue_linker.parse_issue_references(
                            pr_body
                        )

                        if issue_numbers:
                            issue_contents = await issue_linker.fetch_issue_content(
                                pr_info["repo_owner"],
                                pr_info["repo_name"],
                                issue_numbers,
                            )
                            context = await issue_linker.inject_issue_context(
                                context, issue_contents
                            )
                            logger.info(
                                f"[{task_id}] 关联了 {len(issue_contents)} 个 Issue 到审查上下文"
                            )
                    except Exception as e:
                        logger.warning(
                            "[{}] Issue 关联失败（不影响审查）: {}",
                            task_id,
                            str(e),
                        )

                # 6.6 语义 Issue 关联（如果启用）
                if (
                    hasattr(settings, "enable_semantic_issue_linking")
                    and settings.enable_semantic_issue_linking
                ):
                    try:
                        from backend.services.issue_embedding_service import (
                            IssueEmbeddingService,
                        )
                        from backend.services.pr_issue_linker import PRIssueLinker

                        issue_emb_service = IssueEmbeddingService()
                        max_links = getattr(settings, "semantic_issue_max_links", 5)
                        threshold = getattr(
                            settings, "semantic_issue_similarity_threshold", 0.65
                        )

                        # 已显式引用的 issues（排除）
                        explicit_numbers = context.get("linked_issue_numbers", [])
                        # 排除 PR 自身编号（PR 在 GitHub 中也是 issue）
                        explicit_numbers = list(
                            set(explicit_numbers + [pr_info["pr_number"]])
                        )

                        related_issues = await issue_emb_service.search_related_issues(
                            repo_owner=pr_info["repo_owner"],
                            repo_name=pr_info["repo_name"],
                            pr_title=pr_info.get("title", ""),
                            pr_body=pr_info.get("body", ""),
                            exclude_numbers=explicit_numbers,
                            top_k=max_links,
                            similarity_threshold=threshold,
                        )

                        if related_issues:
                            # AI 验证：过滤误判的候选 issues
                            # 构建变更文件列表（含 patch 摘要）
                            file_list = ""
                            if analysis and analysis.code_files:
                                file_parts = []
                                total_len = 0
                                for f in analysis.code_files:
                                    part = f"- {f.path} ({f.status})"
                                    if f.patch:
                                        part += f"\n```diff\n{f.patch}\n```"
                                    file_parts.append(part)
                                    total_len += len(part)
                                    if total_len > 4000:
                                        break
                                file_list = "\n".join(file_parts)
                            related_issues = (
                                await issue_emb_service.verify_related_issues(
                                    pr_title=pr_info.get("title", ""),
                                    pr_body=pr_info.get("body", ""),
                                    candidates=related_issues,
                                    pr_summary=context.get("pr_summary", ""),
                                    pr_files=file_list,
                                )
                            )

                        if related_issues:
                            # 更新 PR body（添加 "Resolves #xxx"）
                            # 重新获取最新 PR body（PR Summary / Dependency Graph 可能已修改）
                            semantic_linker = PRIssueLinker()

                            latest_pr = await asyncio.to_thread(
                                repo.get_pull, pr_info["pr_number"]
                            )
                            current_body = latest_pr.body or ""
                            new_body = semantic_linker.build_updated_pr_body(
                                current_body, related_issues
                            )
                            if new_body != current_body:
                                await asyncio.to_thread(latest_pr.edit, body=new_body)

                            # 注入上下文
                            context["semantically_linked_issues"] = related_issues

                            # 保存到数据库
                            AsyncSession = get_async_session()
                            async with AsyncSession() as db_session:
                                for issue in related_issues:
                                    link = PRIssueLink(
                                        pr_id=pr_info["pr_number"],
                                        repo_name=pr_info["repo_full_name"],
                                        issue_number=issue["number"],
                                        link_type="semantic",
                                        reference_text=(f"Resolves #{issue['number']}"),
                                        inference_reason=(
                                            f"similarity: {issue['similarity']}"
                                        ),
                                    )
                                    db_session.add(link)
                                await db_session.commit()

                            logger.info(
                                f"[{task_id}] 语义关联了 {len(related_issues)} 个 Issues: "
                                f"{[i['number'] for i in related_issues]}"
                            )
                    except Exception as e:
                        logger.warning(
                            "[{}] 语义 Issue 关联失败（不影响审查）: {}",
                            task_id,
                            e,
                            exc_info=True,
                        )

                # Cancel checkpoint: before AI review (critical — most expensive step)
                if self._check_cancelled(task_key):
                    await _finish_execution("cancelled")
                    return await self._cancel_and_cleanup(
                        task_id,
                        task_key,
                        review_obj,
                        review_id,
                        "跳过 AI 审查",
                        pr_info=pr_info,
                        output_language=output_language,
                        head_sha=head_sha,
                    )

                # 7. 并行执行AI审查和标签推荐
                await self._update_review_status(review_id, PRStatus.REVIEWING)
                # Analysis Check 计数器/计时（工具模式下由 progress 事件 lazy 驱动）
                analysis_tool_call_count = 0
                last_analysis_snapshot = None
                analysis_start_ts = time.monotonic()
                if head_sha:
                    await self.check_run_service.report_stage_progress(
                        run_key,
                        stage="reviewing",
                        completed_stages=list(check_run_stages),
                        output_language=output_language,
                    )
                    check_run_stages.append("reviewing")

                # 增量审查复用同一 PR 的长期 reviewer Thread：上一轮的 Canonical
                # Message 天然累积在同一 Thread 中，本轮直接读取即可恢复完整对话
                # 历史，无需再创建 per-run session 或跨 session 复制消息。
                initial_messages: list[dict[str, Any]] = []
                if (
                    execution is not None
                    and execution.thread is not None
                    and analysis.is_incremental
                ):
                    try:
                        initial_messages = (
                            await execution.tool_service.load_conversation_messages(
                                execution.thread.id
                            )
                        )
                        if initial_messages:
                            logger.info(
                                "[{}] 已恢复 reviewer Thread 历史: thread_id={} messages={}",
                                task_id,
                                execution.thread.id,
                                len(initial_messages),
                            )
                    except Exception as restore_exc:
                        logger.warning(
                            "[{}] 恢复 reviewer 历史失败（继续当前增量上下文）: {}",
                            task_id,
                            restore_exc,
                        )

                from backend.services.pr_review_incremental_queue import (
                    PRReviewIncrementalQueueService,
                )

                queue_service = PRReviewIncrementalQueueService()
                pending_incremental = None

                async def _pending_incremental_message():
                    nonlocal pending_incremental, head_sha, run_key
                    if pending_incremental is not None:
                        return None
                    pending_incremental = (
                        await queue_service.prepare_pending_for_review(
                            pr_info=pr_info,
                            repo=repo,
                        )
                    )
                    if pending_incremental is None:
                        return None
                    trigger_ids = list(
                        getattr(
                            pending_incremental,
                            "observability_trigger_ids",
                            None,
                        )
                        or []
                    )
                    pending_session_id = getattr(
                        pending_incremental,
                        "observability_session_id",
                        None,
                    )
                    if trigger_ids:
                        if pending_session_id is not None and int(
                            pending_session_id
                        ) != int(execution.session.id):
                            raise RuntimeError(
                                "pending incremental triggers belong to another session"
                            )
                        await execution.observability.merge_invocation_triggers(
                            execution.invocation.id,
                            trigger_ids,
                        )
                    # 增量消费：PR head 已推进到新 commit。GitHub 不允许修改已创建
                    # check run 的 head_sha，因此收尾旧 head 的 check run（标注为被
                    # 增量取代），后续 report 切换到新 head，使审查完成 conclusion
                    # 体现在 PR 最新 commit 上（否则 check 留在旧 commit，面板看不到）。
                    # 同步重建 run_key，使后续 report/finalize/持久化都基于新 head
                    # （否则 external_id 写入旧 commit、缓存键错位、副 Check 收敛失效）。
                    new_head = getattr(pending_incremental, "head_sha", None)
                    if new_head and new_head != head_sha:
                        old_head = head_sha
                        head_sha = new_head
                        run_key = ReviewRunKey(
                            repo_full_name=run_key.repo_full_name,
                            pr_number=run_key.pr_number,
                            head_sha=new_head,
                            review_job_id=run_key.review_job_id,
                        )
                        if old_head:
                            await self.check_run_service.cancel_active_runs_by_sha(
                                pr_info["repo_owner"],
                                pr_info["repo_name"],
                                old_head,
                                cancel_reason="superseded",
                                output_language=output_language,
                            )
                        await self.check_run_service.report_stage_progress(
                            run_key,
                            stage="reviewing",
                            completed_stages=list(check_run_stages),
                            output_language=output_language,
                        )
                    return pending_incremental.message

                # 构造事件回调：将 AI 审查过程中的消息持久化到新可观测性 Canonical
                # Transcript（同一 PR 的长期 reviewer Thread），替代旧 checkpoint 表。
                async def _review_event_callback(event_type, data):
                    """Persist reviewer dialogue onto the canonical thread.

                    额外处理 "progress" 事件（reviewer._run_tool_loop 每轮快照），
                    桥接到 Analysis Check；并累计 AI 工具调用次数。
                    """
                    nonlocal \
                        pending_incremental, \
                        analysis_tool_call_count, \
                        last_analysis_snapshot
                    if execution is None or execution.thread is None:
                        return
                    try:
                        if event_type == "message":
                            origin_attempt_id = (
                                getattr(
                                    execution.observer,
                                    "last_attempt_id",
                                    None,
                                )
                                if data.get("role") in {"assistant", "tool"}
                                else None
                            )
                            msg = await execution.tool_service.append_conversation_message(
                                thread_id=execution.thread.id,
                                work_unit_id=execution.work_unit.id,
                                message=data,
                                origin_attempt_id=origin_attempt_id,
                                lease=execution.lease,
                            )
                            if data.get("role") == "tool" and data.get("tool_call_id"):
                                if execution.tool_service.is_failed_tool_result(data):
                                    await execution.tool_service.mark_tool_execution_failed(
                                        execution.work_unit.id,
                                        data["tool_call_id"],
                                    )
                                else:
                                    await execution.tool_service.mark_tool_execution_completed(
                                        execution.work_unit.id, data["tool_call_id"]
                                    )
                            if data.get("role") == "assistant" and data.get(
                                "tool_calls"
                            ):
                                analysis_tool_call_count += len(data["tool_calls"])
                            if (
                                pending_incremental is not None
                                and data is pending_incremental.message
                            ):
                                try:
                                    await queue_service.mark_consumed(
                                        pending_incremental.queue_ids,
                                        review_id=review_id,
                                        session_id=execution.session.id,
                                        consumed_message_id=msg.id,
                                    )
                                    pending_incremental = None
                                except Exception as consume_exc:
                                    logger.warning(
                                        "mark_consumed failed, queue items remain pending: {}",
                                        consume_exc,
                                    )
                                    pending_incremental = None
                            return msg
                        elif event_type == "tool_running":
                            await execution.tool_service.mark_tool_execution_running(
                                execution.work_unit.id, data
                            )
                        elif event_type == "progress":
                            # reviewer._run_tool_loop 本轮快照 → Analysis Check
                            if head_sha:
                                _tu = data.get("token_usage") or {}
                                _snapshot = ReviewProgressSnapshot(
                                    current_round=data.get("iteration", 0),
                                    max_rounds=data.get("max_iterations", 0),
                                    tool_call_count=analysis_tool_call_count,
                                    total_input_tokens=_tu.get("prompt_tokens"),
                                    total_output_tokens=_tu.get("completion_tokens"),
                                    current_context_tokens=data.get("current_tokens"),
                                    context_limit=data.get("safe_context"),
                                    model_name=data.get("model"),
                                )
                                last_analysis_snapshot = _snapshot
                                await self.check_run_service.report_analysis_snapshot(
                                    run_key,
                                    _snapshot,
                                    output_language=output_language,
                                )
                    except Exception as exc:
                        logger.debug("checkpoint callback failed: {}", exc)

                # 检查是否启用标签推荐功能
                enable_label_recommendation = _get_label_rec_setting("enabled", True)
                label_execution = None
                if enable_label_recommendation:
                    try:
                        summary_snapshot = (
                            await resolver("summary") if resolver is not None else None
                        )
                        label_execution = (
                            await self.activity_integration.start_auxiliary_execution(
                                session_id=execution.session.id,
                                invocation_id=execution.invocation.id,
                                role="summary",
                                role_snapshot=summary_snapshot,
                                requirement="detached",
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "[{}] 标签推荐可观测通道创建失败，继续执行但不记录该通道: {}",
                            task_id,
                            exc,
                        )

                # 根据配置决定是否使用AI工具
                enable_tools = (
                    settings.enable_ai_tools
                    if hasattr(settings, "enable_ai_tools")
                    else True
                )

                # 准备并行任务
                tasks = []

                if enable_tools:
                    logger.info(f"[{task_id}] 使用AI工具驱动模式进行审查")
                    tasks.append(
                        self.ai_reviewer.review_pr_with_tools(
                            context,
                            analysis.strategy,
                            repo,
                            pr,
                            event_callback=_review_event_callback,
                            pending_user_message_callback=(
                                _pending_incremental_message
                                if analysis.is_incremental
                                else None
                            ),
                            initial_messages=initial_messages or None,
                            cancel_event=self._cancel_events.get(task_key),
                            invocation_context=execution.invocation_context,
                            observer=execution.observer,
                            publication_coordinator=execution.publication_coordinator,
                        )
                    )
                else:
                    logger.info("[{}] 使用标准模式进行审查", task_id)
                    tasks.append(
                        self.ai_reviewer.review_pr(
                            context,
                            analysis.strategy,
                            cancel_event=self._cancel_events.get(task_key),
                            invocation_context=execution.invocation_context,
                            observer=execution.observer,
                            publication_coordinator=execution.publication_coordinator,
                        )
                    )

                # 任务2: AI标签推荐
                if enable_label_recommendation:
                    logger.info("[{}] 并行启动AI标签推荐...", task_id)

                    async def run_label_recommendation():
                        label_status = "completed"
                        try:
                            # 获取仓库可用标签
                            available_labels = await label_service.get_repo_labels(
                                pr_info["repo_owner"], pr_info["repo_name"]
                            )

                            # 获取 PR 已有标签（用于增量审查冲突检测）
                            pr_existing_labels = (
                                await label_service.get_pr_existing_labels(
                                    pr_info["repo_owner"],
                                    pr_info["repo_name"],
                                    pr_info["pr_number"],
                                )
                            )

                            # 构造标签推荐可观测回调，把请求/响应写入辅助 summary
                            # Thread，使实时监控可区分"标签推荐请求/响应"卡片。
                            # Build a callback so the label recommendation
                            # request/response land on the summary thread.
                            label_callback = (
                                _make_label_event_callback(label_execution, task_id)
                                if label_execution is not None
                                else None
                            )

                            # AI推荐标签（传入已有标签）
                            recommendations = await self.ai_reviewer.recommend_labels(
                                context,
                                available_labels,
                                pr_info,
                                existing_labels=pr_existing_labels,
                                invocation_context=(
                                    label_execution.invocation_context
                                    if label_execution is not None
                                    else None
                                ),
                                observer=(
                                    label_execution.observer
                                    if label_execution is not None
                                    else None
                                ),
                                event_callback=label_callback,
                                propagate_errors=True,
                            )

                            if recommendations:
                                # 应用标签到PR（传入已有标签用于冲突检测）
                                confidence_threshold = _get_label_rec_setting(
                                    "confidence_threshold", 0.7
                                )
                                auto_create_labels = _get_label_rec_setting(
                                    "auto_create", False
                                )

                                label_results = await label_service.apply_labels_to_pr(
                                    pr_info["repo_owner"],
                                    pr_info["repo_name"],
                                    pr_info["pr_number"],
                                    recommendations,
                                    confidence_threshold=confidence_threshold,
                                    auto_create=auto_create_labels,
                                    existing_labels=pr_existing_labels,
                                )

                                logger.info(
                                    f"[{task_id}] 标签应用完成: "
                                    f"已应用 {len(label_results.get('applied', []))} 个, "
                                    f"建议 {len(label_results.get('suggested', []))} 个, "
                                    f"冲突跳过 {len(label_results.get('conflict_blocked', []))} 个"
                                )
                                return label_results
                            else:
                                logger.info("[{}] AI未推荐任何标签", task_id)
                                return None

                        except asyncio.CancelledError:
                            label_status = "cancelled"
                            raise
                        except Exception as label_error:
                            label_status = "failed"
                            logger.warning(
                                "[{}] 标签推荐失败（不影响审查）: {}",
                                task_id,
                                str(label_error),
                            )
                            return None
                        finally:
                            if label_execution is not None:
                                try:
                                    await label_execution.finish(
                                        label_status,
                                        error_message=(
                                            "label recommendation failed"
                                            if label_status == "failed"
                                            else None
                                        ),
                                    )
                                except Exception as finish_error:
                                    logger.warning(
                                        "[{}] 标签推荐可观测通道收尾失败: {}",
                                        task_id,
                                        finish_error,
                                    )

                    tasks.append(run_label_recommendation())

                # 并行执行所有任务
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 解析结果
                review_result = results[0]
                if not isinstance(review_result, Exception):
                    review_result = self._normalize_review_result_for_diff(
                        review_result,
                        analysis,
                        task_id,
                    )
                    # 8. 保存审查结果
                    await self._save_review_results(review_id, review_result, analysis)
                    # WorkUnit 终态由 execution.finish("completed") 统一收敛（见下方
                    # 成功路径），审查结果摘要持久化在 PRReview 记录中。
                else:
                    logger.error("[{}] AI审查失败: {}", task_id, str(review_result))
                    raise review_result

                # 获取标签推荐结果
                label_results = None
                if enable_label_recommendation and len(results) > 1:
                    if isinstance(results[1], Exception):
                        logger.warning(
                            "[{}] 标签推荐任务异常: {}", task_id, str(results[1])
                        )
                    else:
                        label_results = results[1]

                # 9. 【第二阶段】删除占位评论，准备创建最终Review
                self._mark_task_reporting(task_key)
                if review_obj:
                    logger.info("[{}] 删除占位评论...", task_id)
                    await self.comment_service.delete_placeholder_comment(review_obj)

                # 10. 【新增】决策引擎：做出审查决定并提交到GitHub（包含行内评论）
                logger.info("[{}] 执行决策引擎...", task_id)
                if head_sha:
                    await self.check_run_service.report_stage_progress(
                        run_key,
                        stage="reporting",
                        completed_stages=list(check_run_stages),
                        output_language=output_language,
                    )
                    check_run_stages.append("reporting")
                    # Analysis Check 定格（success）：进入 reporting 即审查分析完成。
                    # 仅工具模式（有 progress 快照）才 finalize；标准模式无 Analysis Check，
                    # 避免误建一个空壳 completed Analysis。
                    if last_analysis_snapshot is not None:
                        _elapsed = time.monotonic() - analysis_start_ts
                        _base = last_analysis_snapshot
                        _analysis_final = ReviewProgressSnapshot(
                            current_round=_base.current_round,
                            max_rounds=_base.max_rounds,
                            tool_call_count=analysis_tool_call_count,
                            total_input_tokens=_base.total_input_tokens,
                            total_output_tokens=_base.total_output_tokens,
                            current_context_tokens=_base.current_context_tokens,
                            context_limit=_base.context_limit,
                            model_name=_base.model_name,
                            elapsed_seconds=_elapsed,
                        )
                        await self.check_run_service.finalize_analysis(
                            run_key,
                            "success",
                            snapshot=_analysis_final,
                            output_language=output_language,
                        )
                # Findings Check 创建（in_progress）：发布前展示统计，让面板看到
                # "正在发布"。发布完成后由 finalize_findings 定格。
                _pre_findings = review_result.get("inline_comments") or []
                _findings_pre_created = False
                # 仅在启用 inline comments 时创建 Findings：禁用时 _make_and_submit_decision
                # 会清空 inline_comments（total=0），pre-create 反而触发误 failure
                if _pre_findings and head_sha and get_settings().enable_inline_comments:
                    _pre_sev = self._count_severity(_pre_findings)
                    _pre_files = len(
                        {
                            (c.get("file_path") or c.get("file"))
                            for c in _pre_findings
                            if isinstance(c, dict)
                        }
                    )
                    await self.check_run_service.report_findings_snapshot(
                        run_key,
                        severity_counts=_pre_sev,
                        files_count=_pre_files,
                        total_count=len(_pre_findings),
                        published_count=0,
                        failed_count=0,
                        output_language=output_language,
                    )
                    _findings_pre_created = True
                (
                    decision,
                    decision_reason,
                    publish_result,
                ) = await self._make_and_submit_decision(
                    pr_info,
                    review_result,
                    review_id,
                    task_id,
                    pr,
                    analysis,
                    label_results,
                    output_language=output_language,
                    execution=execution,
                )

                # 11. 更新状态为完成
                await self._update_review_status(
                    review_id,
                    PRStatus.COMPLETED,
                    overall_score=review_result.get("overall_score"),
                    decision=decision,
                    decision_reason=decision_reason,
                )
                if head_sha:
                    _inline_comments = review_result.get("inline_comments") or []
                    _severity_counts = self._count_severity(_inline_comments)
                    await self.check_run_service.report_completed(
                        run_key,
                        decision=decision,
                        overall_score=review_result.get("overall_score"),
                        findings_count=len(_inline_comments),
                        severity_counts=_severity_counts,
                        summary_excerpt=str(review_result.get("summary") or ""),
                        output_language=output_language,
                    )
                    # Findings Check：仅有 publishable findings 时创建，据发布结果定格。
                    # 发布成功→neutral（不因 severity 标 failure）；发布失败→failure。
                    # 已创建 pre-Findings 但 publish_result 不可用（异常/total=0）时，
                    # 显式 finalize failure，避免停在 in_progress 悬挂。
                    _files = len(
                        {
                            (c.get("file_path") or c.get("file"))
                            for c in _inline_comments
                            if isinstance(c, dict)
                        }
                    )
                    if publish_result and publish_result.get("total", 0) > 0:
                        # conclusion 基于发布完整性，不只 success：降级 fallback 时
                        # success=True 但 inline 未发布（failed>0）→ failure
                        _findings_conclusion = (
                            "neutral"
                            if publish_result.get("failed", 0) == 0
                            else "failure"
                        )
                        await self.check_run_service.finalize_findings(
                            run_key,
                            _findings_conclusion,
                            severity_counts=_severity_counts,
                            files_count=_files,
                            total_count=publish_result["total"],
                            published_count=publish_result["published"],
                            failed_count=publish_result["failed"],
                            output_language=output_language,
                        )
                    elif _findings_pre_created:
                        await self.check_run_service.finalize_findings(
                            run_key,
                            "failure",
                            severity_counts=_severity_counts,
                            files_count=_files,
                            total_count=len(_inline_comments),
                            published_count=0,
                            failed_count=len(_inline_comments),
                            output_language=output_language,
                        )
                    # 持久化三 Check 的 run_id 到 PRReview（跨进程恢复主索引）
                    await self._persist_review_check_run_ids(review_id, run_key)
                await self._log_activity(
                    review_id,
                    "result",
                    {
                        "status": "completed",
                        "message": "审查完成",
                        "decision": decision.value if decision else "unknown",
                        "overall_score": review_result.get("overall_score"),
                    },
                )
                await self._notify_agent_team_review_completed(review_id, task_id)

                # 11.5 异步触发 .sakura/ 反思 / Trigger .sakura/ reflection async
                try:
                    if (
                        settings.sakura_memory_enabled
                        and settings.sakura_reflection_enabled
                    ):
                        from backend.services.sakura_memory_service import (
                            get_sakura_memory_service,
                        )

                        sakura_memory_service = get_sakura_memory_service()
                        # 将 decision 写入 review_result 供反思使用
                        review_result["decision"] = (
                            decision.value if decision else "unknown"
                        )

                        # 增量审查时为反思获取历史摘要。审查会话本身已通过
                        # _restore_incremental_activity_history 恢复完整历史，
                        # 历史摘要不再注入审查 prompt；此处仅供独立运行的反思
                        # 任务提供历史上下文，并在后台 task 内获取以免阻塞收尾。
                        async def _reflect_with_history() -> None:
                            history_summary = (
                                await self._fetch_reflection_history_summary(
                                    analysis, pr_info, task_id
                                )
                            )
                            await sakura_memory_service.reflect(
                                repo=pr.base.repo,
                                repo_full_name=pr_info["repo_full_name"],
                                pr=pr,
                                review_result=review_result,
                                analysis=analysis,
                                pr_info=pr_info,
                                history_summary=history_summary,
                                review_id=review_id,
                            )

                        ensure_background_admission("review_reflection")
                        task = asyncio.create_task(_reflect_with_history())
                        try:
                            register_background_task(task, "review_reflection")
                        except DatabaseResetRuntimeAdmissionClosed:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                            raise
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                        logger.info("[{}] 已触发 .sakura/ 反思任务", task_id)
                except Exception as e:
                    logger.warning(
                        "[{}] 触发 .sakura/ 反思失败（不影响审查）: {}",
                        task_id,
                        str(e),
                    )

                # 12. 发送Telegram审查完成通知
                await self._send_review_complete_notification(pr_info, review_result)

                await _finish_execution("completed")

                logger.info(
                    f"[{task_id}] 审查任务完成: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
                    f"decision={decision.value if decision else 'N/A'}"
                )
                return task_id

            except ReviewCancelledError:
                # AI 工具循环 / 退避等待响应了外部取消信号（如 PR 关闭）：
                # 复用 _cancel_and_cleanup 走 CANCELLED 收尾（删占位评论 +
                # 状态置 CANCELLED + Check Run 置 cancelled），避免被通用
                # except Exception 当作 FAILED 处理。
                execution_target_status = "cancelled"
                await self._cancel_and_cleanup(
                    task_id,
                    task_key,
                    review_obj,
                    review_id,
                    "AI 审查被外部取消",
                    pr_info=pr_info,
                    output_language=output_language,
                    head_sha=head_sha,
                )
                await _finish_execution("cancelled")
                return task_id
            except Exception as e:
                logger.error(
                    "[{}] 处理审查任务时出错: {}",
                    task_id,
                    str(e),
                    exc_info=True,
                )

                if review_id:
                    await self._log_activity(
                        review_id,
                        "error",
                        {
                            "message": f"审查失败: {e!s}",
                        },
                    )
                    # 收尾当前 review 状态，避免 worker 死后留僵尸
                    # （_save_error_record 只新建 FAILED 记录，不动当前 review）
                    try:
                        await self._update_review_status(review_id, PRStatus.FAILED)
                    except Exception as status_error:
                        logger.warning(
                            "[{}] 异常路径更新审查状态为失败失败: {}",
                            task_id,
                            status_error,
                        )

                _fail_sha = head_sha  # 增量消费后 head_sha 可能已切换到新 commit
                if _fail_sha:
                    _error_reference = uuid.uuid4().hex[:8]
                    _failed_stage = self._infer_failed_stage(check_run_stages)
                    logger.error(
                        "[{}] 审查失败，故障编号 {}（阶段={}）",
                        task_id,
                        _error_reference,
                        _failed_stage,
                    )
                    await self.check_run_service.finalize_review_run(
                        ReviewRunKey(
                            repo_full_name=pr_info.get("repo_full_name")
                            or f"{pr_info['repo_owner']}/{pr_info['repo_name']}",
                            pr_number=pr_info.get("pr_number", 0),
                            head_sha=_fail_sha,
                            review_job_id=str(review_id)
                            if review_id
                            else "failed-no-review",
                        ),
                        "failure",
                        failed_stage=_failed_stage,
                        error_reference=_error_reference,
                        completed_stages=list(check_run_stages),
                        output_language=output_language,
                    )
                    if review_id:
                        await self._persist_error_reference(review_id, _error_reference)

                # 【错误处理】更新占位评论为错误消息
                if review_obj:
                    try:
                        await self.comment_service.update_review_with_error(
                            review_obj,
                            str(e),
                            pr,
                            output_language=output_language,
                        )
                        logger.info("[{}] 已更新占位评论为错误状态", task_id)
                    except Exception as update_error:
                        logger.error(
                            "[{}] 更新错误消息失败: {}",
                            task_id,
                            str(update_error),
                        )

                # 保存错误信息到数据库
                try:
                    await self._save_error_record(pr_info, str(e), task_id)
                except Exception as save_error:
                    logger.error("保存错误记录失败: {}", str(save_error))
                await _finish_execution("failed", error_message=str(e))
                raise
            except asyncio.CancelledError:
                # 超时（_run_review_task_with_timeout 的 wait_for）或外部取消：
                # except Exception 不接 CancelledError，需单独收尾 review 状态，防止僵尸。
                execution_target_status = "cancelled"
                if review_id:
                    try:
                        await self._update_review_status(review_id, PRStatus.FAILED)
                    except Exception as status_error:
                        logger.warning(
                            "[{}] 取消路径更新审查状态为失败失败: {}",
                            task_id,
                            status_error,
                        )
                _cancel_fail_sha = head_sha  # 增量消费后 head_sha 可能已切换到新 commit
                if _cancel_fail_sha:
                    _error_reference = uuid.uuid4().hex[:8]
                    _failed_stage = self._infer_failed_stage(check_run_stages)
                    logger.error(
                        "[{}] 审查被取消/超时，故障编号 {}（阶段={}）",
                        task_id,
                        _error_reference,
                        _failed_stage,
                    )
                    await self.check_run_service.finalize_review_run(
                        ReviewRunKey(
                            repo_full_name=pr_info.get("repo_full_name")
                            or f"{pr_info['repo_owner']}/{pr_info['repo_name']}",
                            pr_number=pr_info.get("pr_number", 0),
                            head_sha=_cancel_fail_sha,
                            review_job_id=str(review_id)
                            if review_id
                            else "failed-no-review",
                        ),
                        "failure",
                        failed_stage=_failed_stage,
                        error_reference=_error_reference,
                        completed_stages=list(check_run_stages),
                        output_language=output_language,
                    )
                    if review_id:
                        await self._persist_error_reference(review_id, _error_reference)
                await _finish_execution("cancelled")
                raise
            finally:
                if execution is not None and execution_status is None:
                    await _finish_execution(
                        execution_target_status or "failed",
                        error_message=(
                            "execution terminated without a terminal status"
                            if execution_target_status is None
                            else None
                        ),
                    )
                # Always unregister task to clean up cancel event
                self._unregister_task(task_key)

    async def _fetch_reflection_history_summary(
        self,
        analysis: PRAnalysis,
        pr_info: dict[str, Any],
        task_id: str,
    ) -> str | None:
        """为 .sakura/ 反思获取增量历史摘要。

        审查会话已经恢复完整历史消息，不再把摘要注入 prompt；反思是独立任务，
        需要单独获取历史摘要作为上下文。失败时返回 None，不影响主审查或反思流程。
        """
        if not analysis.is_incremental:
            return None

        try:
            from backend.services.history_context_service import HistoryContextService

            history_service = HistoryContextService(
                self.ai_reviewer.api_client,
                model="",
            )
            return await history_service.fetch_history_summary(
                pr_id=analysis.pr_id,
                repo_name=pr_info["repo_name"],
                repo_owner=pr_info["repo_owner"],
            )
        except Exception as hist_exc:
            logger.warning(
                f"[{task_id}] 反思历史摘要获取失败（不影响反思）: {hist_exc}",
                exc_info=True,
            )
            return None

    async def _notify_agent_team_review_completed(
        self, review_id: int, task_id: str
    ) -> None:
        """通知 Agent Team 处理 PR Review 完成后的闭环反馈。"""
        try:
            from backend.services.agent_team.pr_review_feedback import (
                AgentTeamPRReviewFeedbackService,
            )

            service = AgentTeamPRReviewFeedbackService()
            result = await service.handle_review_completed_with_result(review_id)
            if result.handled:
                logger.info(
                    "[{}] Agent Team PR 闭环已处理 review_id={}, action={}",
                    task_id,
                    review_id,
                    result.action,
                )
            else:
                logger.info(
                    "[{}] Agent Team PR 闭环跳过 review_id={}, reason={}",
                    task_id,
                    review_id,
                    result.reason,
                )
        except Exception as exc:
            logger.warning(
                "[{}] Agent Team PR 闭环处理失败（不影响 PR Review 完成）: {}",
                task_id,
                exc,
            )

    async def _create_review_record(
        self, analysis: PRAnalysis, pr_info: dict[str, Any], task_id: str
    ) -> int:
        """创建审查记录"""

        async def _do():
            AsyncSession = get_async_session()
            async with AsyncSession() as session:
                record = PRReview(
                    pr_id=analysis.pr_id,
                    pr_number=analysis.pr_number,
                    repo_name=pr_info["repo_name"],
                    repo_owner=pr_info["repo_owner"],
                    author=pr_info["author"],
                    title=pr_info["title"],
                    branch=pr_info["branch"],
                    head_sha=pr_info.get("head_sha"),
                    file_count=analysis.total_files,
                    line_count=analysis.total_changes,
                    code_file_count=analysis.code_file_count,
                    strategy=ReviewStrategy(analysis.strategy),
                    status=PRStatus.PENDING,
                )
                session.add(record)
                await session.commit()
                await session.refresh(record)
                logger.info("[{}] 创建审查记录: {}", task_id, record.id)
                return record.id

        return await _db_retry(_do)

    async def _update_review_status(
        self,
        review_id: int,
        status: PRStatus,
        overall_score: int | None = None,
        decision: ReviewDecision | None = None,
        decision_reason: str | None = None,
    ):
        """更新审查状态"""

        async def _do():
            AsyncSession = get_async_session()
            async with AsyncSession() as session:
                record = await session.get(PRReview, review_id)
                if record:
                    record.status = status
                    if status in (
                        PRStatus.COMPLETED,
                        PRStatus.CANCELLED,
                        PRStatus.FAILED,
                    ):
                        record.completed_at = now_utc()
                    if overall_score is not None:
                        record.overall_score = overall_score
                    if decision is not None:
                        record.decision = decision
                    if decision_reason is not None:
                        record.decision_reason = decision_reason
                    await session.commit()

        await _db_retry(_do)

        # 发布 SSE 事件通知前端
        try:
            from backend.webui.sse import publish_event

            await publish_event(
                "review:status_changed",
                {
                    "review_id": review_id,
                    "status": status.value if hasattr(status, "value") else str(status),
                },
            )
        except Exception as e:
            logger.warning("发布 SSE 事件失败（不影响主流程）: {}", str(e))

    async def _save_review_results(
        self, review_id: int, review_result: dict[str, Any], analysis: PRAnalysis
    ):
        """保存审查结果"""

        async def _do():
            AsyncSession = get_async_session()
            async with AsyncSession() as session:
                # 更新摘要
                record = await session.get(PRReview, review_id)
                if record:
                    record.review_summary = review_result.get("summary", "")

                    # 保存 Token 消耗数据
                    token_usage = review_result.get("token_usage", {})
                    record.prompt_tokens = token_usage.get("prompt_tokens", 0)
                    record.completion_tokens = token_usage.get("completion_tokens", 0)

                    # 计算预估成本
                    s = get_settings()
                    tracker = TokenTracker()
                    tracker.prompt_tokens = record.prompt_tokens
                    tracker.completion_tokens = record.completion_tokens
                    record.estimated_cost = tracker.calculate_cost(
                        s.review_price_per_1k_prompt, s.review_price_per_1k_completion
                    )

                # 保存整体评论
                comments = review_result.get("comments", [])
                for comment_data in comments:
                    comment = ReviewComment(
                        review_id=review_id,
                        file_path=None,  # 整体评论没有文件路径
                        line_number=None,
                        comment_type=CommentType.OVERALL,
                        severity=CommentSeverity(
                            comment_data.get("severity", "suggestion")
                        ),
                        content=comment_data["content"],
                    )
                    session.add(comment)

                # 保存行内评论
                inline_comments = review_result.get("inline_comments", [])
                for comment_data in inline_comments:
                    comment = ReviewComment(
                        review_id=review_id,
                        file_path=comment_data.get("file_path"),
                        line_number=comment_data.get("line_number"),
                        comment_type=CommentType.LINE,
                        severity=CommentSeverity(
                            comment_data.get("severity", "suggestion")
                        ),
                        content=comment_data.get("body", ""),
                    )
                    session.add(comment)

                await session.commit()
                logger.info(
                    f"保存了 {len(comments)} 条整体评论和 {len(inline_comments)} 条行内评论"
                )

        await _db_retry(_do)

    async def _save_skip_record(self, analysis: PRAnalysis, pr_info: dict[str, Any]):
        """保存跳过记录"""

        async def _do():
            AsyncSession = get_async_session()
            async with AsyncSession() as session:
                record = PRReview(
                    pr_id=analysis.pr_id,
                    pr_number=analysis.pr_number,
                    repo_name=pr_info["repo_name"],
                    repo_owner=pr_info["repo_owner"],
                    author=pr_info["author"],
                    title=pr_info["title"],
                    branch=pr_info["branch"],
                    head_sha=pr_info.get("head_sha"),
                    file_count=analysis.total_files,
                    line_count=analysis.total_changes,
                    code_file_count=analysis.code_file_count,
                    strategy=ReviewStrategy.SKIP,
                    status=PRStatus.COMPLETED,
                    review_summary=f"跳过审查: {analysis.skip_reason}",
                )
                session.add(record)
                await session.commit()

        await _db_retry(_do)

    async def _save_error_record(
        self, pr_info: dict[str, Any], error_message: str, task_id: str
    ):
        """保存错误记录"""

        async def _do():
            AsyncSession = get_async_session()
            async with AsyncSession() as session:
                record = PRReview(
                    pr_id=pr_info["pr_id"],
                    pr_number=pr_info["pr_number"],
                    repo_name=pr_info["repo_name"],
                    repo_owner=pr_info["repo_owner"],
                    author=pr_info["author"],
                    title=pr_info["title"],
                    branch=pr_info["branch"],
                    head_sha=pr_info.get("head_sha"),
                    file_count=0,
                    line_count=0,
                    code_file_count=0,
                    strategy=ReviewStrategy.STANDARD,
                    status=PRStatus.FAILED,
                    error_message=error_message,
                )
                session.add(record)
                await session.commit()
                logger.info("[{}] 保存错误记录", task_id)

        await _db_retry(_do)

    async def _make_and_submit_decision(
        self,
        pr_info: dict[str, Any],
        review_result: dict[str, Any],
        review_id: int,
        task_id: str,
        pr: Any,
        analysis: PRAnalysis,
        label_results: dict[str, Any] | None = None,
        output_language: str | None = None,
        execution: Any = None,
    ) -> tuple[ReviewDecision | None, str | None, dict[str, Any] | None]:
        """做出审查决定并提交到GitHub（包含行内评论）

        Args:
            pr_info: PR信息
            review_result: AI审查结果
            review_id: 审查记录ID
            task_id: 任务ID
            pr: GitHub PR对象
            analysis: PR分析结果
            label_results: 标签应用结果

        Returns:
            (决策类型, 决策理由)
        """
        try:
            # 1. 获取决策引擎
            decision_engine = get_decision_engine()

            # 2. 做出决策
            decision, decision_reason = decision_engine.make_decision(
                review_result=review_result,
                repo_full_name=pr_info["repo_full_name"],
            )

            logger.info(
                f"[{task_id}] 决策引擎结果: decision={decision.value}, "
                f"reason={decision_reason}"
            )

            # 3. 获取策略名称
            strategy_info = get_strategy_config().get_strategy(analysis.strategy)
            strategy_name = strategy_info.get("name", "代码审查")

            # 4. 格式化审查评论（包含标签信息和策略名称）
            review_body = decision_engine.format_review_body(
                decision=decision,
                review_result=review_result,
                decision_reason=decision_reason,
                label_results=label_results,
                strategy_name=strategy_name,
                output_language=output_language,
            )

            # 5. 获取行内评论
            inline_comments = review_result.get("inline_comments", [])

            # 5.0 检查行内评论开关（关闭时清空，保留 AI 输出但跳过提交）
            if not settings.enable_inline_comments:
                if inline_comments:
                    logger.info(
                        f"[{task_id}] 行内评论功能已关闭，跳过 {len(inline_comments)} 条行内评论"
                    )
                inline_comments = []

            # 6. 提交Review到GitHub（包含行内评论）
            # 检查是否启用幂等性检查
            policy = decision_engine._get_repo_policy(pr_info["repo_full_name"])
            enable_idempotency = policy.get("enable_idempotency_check", True)

            # synchronize 事件的处理逻辑
            is_incremental = analysis.is_incremental if analysis else False
            is_synchronize = pr_info.get("action") == "synchronize"

            # 获取机器人用户名（用于幂等性检查和撤回Review）
            bot_username = await asyncio.to_thread(
                self.github_app.get_bot_username,
                pr_info["repo_owner"],
                pr_info["repo_name"],
            )

            if is_synchronize and bot_username:
                if is_incremental:
                    # Incremental review: old reviews already dismissed in webhook handler,
                    # skip idempotency check to allow new review submission
                    enable_idempotency = False
                    logger.info("[{}] 增量审查模式，跳过幂等性检查", task_id)
                else:
                    # Full review fallback (force push etc.):
                    # dismiss again in case webhook dismiss was missed or failed
                    dismissed = await asyncio.to_thread(
                        self.github_app.dismiss_bot_reviews,
                        pr_info["repo_owner"],
                        pr_info["repo_name"],
                        pr_info["pr_number"],
                        bot_username,
                    )
                    if dismissed > 0:
                        logger.info(
                            f"[{task_id}] 已撤回 {dismissed} 条旧 Review，将提交全量审查"
                        )
                    else:
                        logger.debug("[{}] 全量审查模式，无旧 Review 需撤回", task_id)

            # 使用 submit_review_with_inline_comments 方法（带重试机制）
            max_retries = 1  # 失败后重试1次
            # submit_review_with_inline_comments 返回结构化 dict（兼容旧 bool）：
            # {success, inline_published, fallback_body_only}。降级（仅 body）时
            # inline_published=0，避免 Findings 误计 published=total。
            _submit_result = {
                "success": False,
                "inline_published": 0,
                "fallback_body_only": False,
            }
            success = False
            review_event = decision.value.upper()  # APPROVE, REQUEST_CHANGES, COMMENT
            author = pr_info.get("author", "")
            bot_names = (
                {bot_username, f"{bot_username}[bot]"} if bot_username else set()
            )
            if review_event in ("APPROVE", "REQUEST_CHANGES") and author in bot_names:
                logger.info(
                    f"[{task_id}] Bot 自身创建的 PR 不能 "
                    f"{review_event}，降级为 COMMENT: "
                    f"author={author}"
                )
                review_event = "COMMENT"

            activity_result_id = review_result.get("_activity_result_id")
            if execution is not None and isinstance(activity_result_id, int):
                success, _submit_result = await self._publish_review_with_observability(
                    execution=execution,
                    result_id=activity_result_id,
                    pr_info=pr_info,
                    review_event=review_event,
                    review_body=review_body,
                    inline_comments=inline_comments,
                    bot_username=bot_username,
                    output_language=output_language,
                )
            else:
                for attempt in range(max_retries + 1):
                    _submit_result = await asyncio.to_thread(
                        self.github_app.submit_review_with_inline_comments,
                        repo_owner=pr_info["repo_owner"],
                        repo_name=pr_info["repo_name"],
                        pr_number=pr_info["pr_number"],
                        event=review_event,
                        body=review_body,
                        inline_comments=inline_comments,
                        bot_username=bot_username,
                        enable_idempotency_check=enable_idempotency,
                        output_language=output_language,
                    )
                    # 兼容旧 bool 返回
                    if isinstance(_submit_result, bool):
                        _submit_result = {
                            "success": _submit_result,
                            "inline_published": len(inline_comments)
                            if _submit_result
                            else 0,
                            "fallback_body_only": False,
                        }
                    success = _submit_result["success"]

                    if success:
                        break  # 成功，退出重试循环

                    if attempt < max_retries:
                        logger.warning(
                            f"[{task_id}] 第 {attempt + 1} 次提交Review失败，1秒后重试..."
                        )
                        await asyncio.sleep(1)

                # 最终兜底：带行内评论的 Review 提交失败时，尝试仅提交 Review Body
                if not success and inline_comments:
                    logger.warning(
                        f"[{task_id}] 带行内评论的Review提交失败，尝试仅提交Review Body..."
                    )
                    success = self.github_app.submit_review(
                        repo_owner=pr_info["repo_owner"],
                        repo_name=pr_info["repo_name"],
                        pr_number=pr_info["pr_number"],
                        event=review_event,
                        body=review_body,
                        bot_username=bot_username,
                        enable_idempotency_check=enable_idempotency,
                    )
                    if success:
                        # submit_review 仅提交 body，inline 评论未发布 → fallback 语义
                        _submit_result = {
                            "success": True,
                            "inline_published": 0,
                            "fallback_body_only": True,
                        }
                        logger.info(
                            "[{}] ✅ 降级成功: 已提交无行内评论的Review",
                            task_id,
                        )
                    else:
                        logger.error(
                            "[{}] 重试 {} 次后仍然失败",
                            task_id,
                            max_retries,
                        )

            if success:
                if inline_comments:
                    logger.info(
                        f"[{task_id}] ✅ 成功提交Review到GitHub: {review_event.lower()} "
                        f"(含{len(inline_comments)}条行内评论)"
                    )
                else:
                    logger.info(
                        f"[{task_id}] ✅ 成功提交Review到GitHub: {review_event.lower()} "
                        f"(无行内评论)"
                    )
            else:
                logger.warning(
                    f"[{task_id}] ⚠️ 提交Review到GitHub失败，但已保存到数据库"
                )

            # 发布结果（Findings Check 数据来源）：total = publishable_findings 数，
            # published 取实际 inline 发布数（降级 fallback 时为 0，不算已发布）
            _total = len(inline_comments) if inline_comments else 0
            # 幂等跳过（review 已存在）：本次未尝试发布，不计 failed（之前已发过）
            if _submit_result.get("skipped_existing"):
                _published = _total
                _failed = 0
            else:
                _published = _submit_result.get(
                    "inline_published", _total if success else 0
                )
                _failed = _total - _published
            _publish_result = {
                "total": _total,
                "published": _published,
                "failed": _failed,
                "success": bool(success),
            }
            return decision, decision_reason, _publish_result

        except Exception as e:
            logger.error("[{}] 决策引擎执行失败: {}", task_id, str(e), exc_info=True)
            # 出错时返回None，不影响审查完成
            return None, f"决策过程异常: {e!s}", None

    async def _publish_review_with_observability(
        self,
        *,
        execution: Any,
        result_id: int,
        pr_info: dict[str, Any],
        review_event: str,
        review_body: str,
        inline_comments: list[dict[str, Any]],
        bot_username: str | None,
        output_language: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Publish exactly once under the authoritative Publication state machine."""
        external_key = (
            f"pr-review-{execution.session.id}-"
            f"{execution.invocation.id}-{execution.work_unit.id}"
        )
        publication = await execution.publication_service.create_pending(
            result_id,
            "pr_review",
            external_key,
        )
        resource_identity = {
            "source_system_instance": pr_info.get(
                "source_system_instance", "github.com"
            ),
            "repository_external_id": pr_info.get(
                "repository_external_id", pr_info.get("repo_full_name", "")
            ),
            "resource_type": "pr",
            "resource_number": str(pr_info["pr_number"]),
        }
        captured: dict[str, Any] = {}

        async def _sender(
            _kind: str,
            body_with_marker: str,
            _resource_identity: dict[str, Any],
        ) -> Any:
            response = await asyncio.to_thread(
                self.github_app.submit_review_with_inline_comments,
                repo_owner=pr_info["repo_owner"],
                repo_name=pr_info["repo_name"],
                pr_number=pr_info["pr_number"],
                event=review_event,
                body=body_with_marker,
                inline_comments=inline_comments,
                bot_username=bot_username,
                # The Publication marker and state machine are authoritative.
                enable_idempotency_check=False,
                raise_on_error=True,
                output_language=output_language,
            )
            if isinstance(response, dict):
                captured.update(response)
            elif isinstance(response, bool):
                captured["success"] = response
            return response

        terminal = await execution.publication_service.send(
            publication.id,
            body=review_body,
            sender=_sender,
            resource_identity=resource_identity,
        )
        succeeded = terminal.status == "succeeded"
        if succeeded and not captured:
            captured.update(
                {
                    "success": True,
                    "inline_published": len(inline_comments),
                    "fallback_body_only": False,
                    "skipped_existing": True,
                }
            )
        else:
            captured.setdefault("success", succeeded)
            captured.setdefault("inline_published", 0)
            captured.setdefault("fallback_body_only", False)
        return succeeded, captured

    async def _send_review_complete_notification(
        self, pr_info: dict[str, Any], review_result: dict[str, Any]
    ):
        """发送审查完成通知到Telegram"""
        try:
            from backend.models.database import async_session
            from backend.services.telegram_service import TelegramService
            from backend.telegram.notifications import get_notification_sender

            notification_sender = get_notification_sender()
            if not notification_sender:
                logger.debug("Telegram通知发送器未初始化，跳过通知")
                return

            # 计算严重问题数量（使用 issues 字典，与 decision_engine 保持一致）
            issues = review_result.get("issues", {})
            critical_count = len(issues.get("critical", []))

            # 获取评分
            score = review_result.get("overall_score", 0)

            # 构建PR URL
            pr_url = f"https://github.com/{pr_info['repo_full_name']}/pull/{pr_info['pr_number']}"

            # 收集通知目标：作者 + 订阅者
            chat_ids = []
            async with async_session() as session:
                service = TelegramService(session)
                chat_ids = await service.get_notification_targets(
                    pr_info["repo_full_name"], pr_info.get("author", "")
                )

            if not chat_ids:
                logger.debug(
                    f"无通知目标: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )
                return

            # 发送通知
            await notification_sender.send_review_complete(
                repo_name=pr_info["repo_full_name"],
                pr_number=pr_info["pr_number"],
                score=score,
                critical_count=critical_count,
                pr_url=pr_url,
                chat_ids=chat_ids,
            )

            logger.info(
                f"已发送审查完成通知: {pr_info['repo_full_name']}#{pr_info['pr_number']} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error("发送Telegram通知失败: {}", str(e), exc_info=True)


# 全局Worker实例
_worker_instance: ReviewWorker | None = None


def get_worker() -> ReviewWorker:
    """获取Worker实例"""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = ReviewWorker()
    return _worker_instance


async def submit_review_task(pr_info: dict[str, Any]) -> str:
    """提交审查任务（从Webhook调用）

    Returns:
        str: Task key in format "owner/repo#pr_number", used for cancellation.
             The internal task_id (short UUID) is logged by process_review_task.
    """
    ensure_background_admission("review")
    worker = get_worker()
    task_key = ReviewWorker._make_task_key(pr_info)
    worker._register_task(task_key, force_new=True)

    # 直接异步执行，并保留后台任务引用避免被 GC。
    task = asyncio.create_task(_run_review_task_with_timeout(worker, pr_info, task_key))
    try:
        register_background_task(task, "review")
    except DatabaseResetRuntimeAdmissionClosed:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise
    worker._background_tasks.add(task)
    task.add_done_callback(worker._background_tasks.discard)

    # 返回任务标识（owner/repo#pr_number），可用于取消
    return task_key


async def _run_review_task_with_timeout(
    worker: ReviewWorker,
    pr_info: dict[str, Any],
    task_key: str,
) -> str:
    """按配置限制 AI 审查阶段，允许已开始的 reporting 完成收尾。"""
    timeout_seconds = get_settings().review_timeout_seconds
    review_task = asyncio.create_task(worker.process_review_task(pr_info))
    try:
        register_background_task(review_task, "review_process")
    except DatabaseResetRuntimeAdmissionClosed:
        review_task.cancel()
        try:
            await review_task
        except asyncio.CancelledError:
            pass
        raise
    try:
        result = await asyncio.wait_for(
            asyncio.shield(review_task),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        is_reporting = getattr(worker, "is_task_reporting", lambda _key: False)
        if is_reporting(task_key):
            logger.warning(
                "审查 AI 阶段已在 {} 秒预算内完成，继续等待 reporting 收尾: {}",
                timeout_seconds,
                task_key,
            )
            result = await review_task
        else:
            worker.cancel_task(task_key)
            review_task.cancel()
            try:
                await review_task
            except asyncio.CancelledError:
                pass
            task_id = "timeout"
            message = f"审查任务超时（{timeout_seconds}秒）"
            logger.error(
                "{}: {}",
                message,
                task_key,
            )
            try:
                await worker._save_error_record(pr_info, message, task_id)
            except Exception as save_error:
                logger.error(
                    "保存超时错误记录失败: {}",
                    str(save_error),
                )
            raise RuntimeError(f"{message}: {task_key}") from exc
    except asyncio.CancelledError:
        review_task.cancel()
        try:
            await review_task
        except asyncio.CancelledError:
            pass
        raise

    # 兜底：非增量审查顺利完成后，若仍有 pending 增量（审查期间到达的新提交，
    # 本次未消费），触发一个增量审查去消费。此时 process_review_task 的 finally
    # 已 unregister task_key，触发新任务不会与当前任务的 cancel event 冲突。
    try:
        await _drain_pending_incremental(pr_info)
    except Exception as exc:
        logger.warning("兜底消费 pending 增量失败: {}", exc)

    return result


async def _drain_pending_incremental(pr_info: dict[str, Any]) -> None:
    """非增量审查完成后的兜底：触发增量审查消费残留 pending 增量。

    首次/完整审查（opened/reopened/ready_for_review/full_review 等非 synchronize
    事件）不会消费增量队列；若审查期间到达了新提交（synchronize 入队给本次 active
    review），这些增量会残留 pending、本轮不被审查。这里在审查顺利结束后检查并
    触发一个增量审查去消费，避免新提交被困在队列里。

    增量审查（synchronize）自身已消费 pending，不在此兜底，避免循环；失败的审查
    走 except 分支不触发，避免 compare 失败时反复触发。
    """
    if pr_info.get("action") == "synchronize":
        return

    from backend.services.pr_review_incremental_queue import (
        PRReviewIncrementalQueueService,
    )

    pending = await PRReviewIncrementalQueueService().list_pending(pr_info)
    if not pending:
        return

    drain_pr_info = {
        **pr_info,
        "action": "synchronize",
        "before": pending[0].base_sha or pr_info.get("before"),
        "after": pending[-1].head_sha,
    }
    logger.info(
        "完整审查完成后发现 {} 条 pending 增量，触发增量审查消费: {}#{}",
        len(pending),
        pr_info.get("repo_full_name"),
        pr_info.get("pr_number"),
    )
    await submit_review_task(drain_pr_info)
