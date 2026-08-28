"""Issue 分析异步任务处理器"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_, select, update

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.config import (
    get_dynamic_config,
    get_sakura_memory_config,
    get_settings,
)
from backend.core.github_app import GitHubAppClient
from backend.models.database import (
    IssueAnalysis,
    IssueAnalysisStatus,
    async_session,
)
from backend.services.ai_task_deadline import AITaskDeadline
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeAdmissionClosed,
    ensure_background_admission,
    register_background_task,
)
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_service import issue_service

# Issue 分析并发控制信号量
_issue_semaphore: asyncio.Semaphore | None = None


async def _get_issue_semaphore() -> asyncio.Semaphore:
    """获取 Issue 分析并发信号量（懒初始化，支持动态更新）"""
    global _issue_semaphore
    if _issue_semaphore is None:
        max_concurrent = await _load_max_concurrent()
        _issue_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"Issue 分析并发信号量初始化: 最大 {max_concurrent} 个并发任务")
    return _issue_semaphore


def reset_issue_semaphore():
    """重置 Issue 分析信号量（配置更新时调用）"""
    global _issue_semaphore
    _issue_semaphore = None
    logger.info("Issue 分析并发信号量已重置，下次任务将重新初始化")


async def _load_max_concurrent() -> int:
    """从动态配置读取最大并发 Issue 分析数"""
    try:
        val = await get_dynamic_config("max_concurrent_issues")
        return int(val) if val is not None else get_settings().max_concurrent_issues
    except Exception as e:
        logger.warning(f"读取 max_concurrent_issues 配置失败，使用默认值: {e}")
        return get_settings().max_concurrent_issues


class IssueWorker:
    """Issue 分析任务处理器"""

    def __init__(self):
        self.analyzer = IssueAnalyzer()
        self.github_app = GitHubAppClient()
        self._background_tasks: set[asyncio.Task] = set()
        # Cancellation signals are scoped by issue, but each running task owns
        # its event so a later task can never inherit an already-set signal.
        self._cancel_events: dict[str, dict[str, asyncio.Event]] = {}
        # Keep task handles beside the event registry.  An Event alone cannot
        # wake a task blocked in ``Semaphore.acquire()``; the handle lets the
        # webhook interrupt and await that task before deleting/closing an
        # Issue.
        self._task_handles: dict[
            str, dict[str, asyncio.Task[Any] | None]
        ] = {}
        # Each task owns exactly one analysis row.  Store the immutable row
        # identity here so cancellation/failure cleanup never selects a newer
        # sibling run for the same Issue.
        self._task_analysis_records: dict[
            str, dict[str, tuple[int, int | None]]
        ] = {}
        self._task_executions: dict[str, dict[str, Any | None]] = {}
        self._task_execution_statuses: dict[str, dict[str, str | None]] = {}
        # A worker task can hand a synchronous GitHub mutation to
        # ``asyncio.to_thread``.  Cancelling the worker only cancels the await
        # around that thread, so retain every in-flight write until its child
        # task has really finished.
        self._task_external_writes: dict[
            str, dict[str, set[asyncio.Task[Any]]]
        ] = {}
        from backend.services.activity_observability.integration_service import (
            ActivityIntegrationService,
        )

        self.activity_integration = ActivityIntegrationService()

    @staticmethod
    def _make_task_key(issue_info: dict[str, Any]) -> str:
        """Return the shared cancellation key for an Issue."""
        repo_full_name = issue_info.get("repo_full_name") or (
            f"{issue_info.get('repo_owner', '')}/{issue_info.get('repo_name', '')}"
        )
        return f"{repo_full_name}#{issue_info.get('issue_number', 0)}"

    def _register_task(
        self,
        task_key: str,
        task_id: str,
        event: asyncio.Event | None = None,
    ) -> asyncio.Event:
        """Register one task before it is scheduled and return its signal."""
        if not hasattr(self, "_cancel_events"):
            self._cancel_events = {}
        task_events = self._cancel_events.setdefault(task_key, {})
        registered = event or asyncio.Event()
        task_events[task_id] = registered
        if not hasattr(self, "_task_handles"):
            self._task_handles = {}
        self._task_handles.setdefault(task_key, {})[task_id] = None
        if not hasattr(self, "_task_analysis_records"):
            self._task_analysis_records = {}
        self._task_analysis_records.setdefault(task_key, {})
        if not hasattr(self, "_task_executions"):
            self._task_executions = {}
        self._task_executions.setdefault(task_key, {})[task_id] = None
        if not hasattr(self, "_task_execution_statuses"):
            self._task_execution_statuses = {}
        self._task_execution_statuses.setdefault(task_key, {})[task_id] = None
        return registered

    def _bind_task_handle(
        self, task_key: str, task_id: str, task: asyncio.Task[Any]
    ) -> None:
        """Bind the scheduled task after its cancellation entry is created."""
        handles = getattr(self, "_task_handles", None)
        if handles is None:
            self._task_handles = handles = {}
        handles.setdefault(task_key, {})[task_id] = task

        # A cancellation can be requested by a test double or an unusual task
        # factory between registration and this binding.  Preserve that
        # request instead of allowing the task to start after it was cancelled.
        event = getattr(self, "_cancel_events", {}).get(task_key, {}).get(task_id)
        if event is not None and event.is_set() and task is not asyncio.current_task():
            task.cancel()

    def _bind_analysis_record(
        self, task_key: str, task_id: str, record: IssueAnalysis
    ) -> None:
        """Associate a worker task with its immutable analysis id/version."""
        records = getattr(self, "_task_analysis_records", None)
        if records is None:
            self._task_analysis_records = records = {}
        records.setdefault(task_key, {})[task_id] = (
            record.id,
            getattr(record, "analysis_version", None),
        )

    def _bind_execution(self, task_key: str, task_id: str, execution: Any) -> None:
        executions = getattr(self, "_task_executions", None)
        if executions is None:
            self._task_executions = executions = {}
        executions.setdefault(task_key, {})[task_id] = execution

    def _register_external_write(
        self, task_key: str, task_id: str, write_task: asyncio.Task[Any]
    ) -> None:
        writes = getattr(self, "_task_external_writes", None)
        if writes is None:
            self._task_external_writes = writes = {}
        writes.setdefault(task_key, {}).setdefault(task_id, set()).add(write_task)

    def _unregister_external_write(
        self, task_key: str, task_id: str, write_task: asyncio.Task[Any]
    ) -> None:
        writes = getattr(self, "_task_external_writes", {}).get(task_key)
        if writes is None:
            return
        task_writes = writes.get(task_id)
        if task_writes is None:
            return
        task_writes.discard(write_task)
        if not task_writes:
            writes.pop(task_id, None)
        if not writes:
            getattr(self, "_task_external_writes", {}).pop(task_key, None)

    def _get_external_writes(
        self, task_key: str, task_id: str
    ) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            write_task
            for write_task in getattr(self, "_task_external_writes", {})
            .get(task_key, {})
            .get(task_id, set())
            if not write_task.done()
        )

    async def _run_external_write(
        self,
        task_key: str,
        task_id: str,
        cancel_event: asyncio.Event,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one external mutation through a cancellation-safe child task.

        ``asyncio.to_thread`` cannot stop the synchronous function once its
        thread has started.  The child task is therefore shielded from the
        worker's cancellation, recorded while it is in flight, and drained
        before the original ``CancelledError`` is re-raised.  The operation
        factory is intentionally called only in the child after the last
        cancellation check, so a write is never started after cooperative
        cancellation has already been requested.
        """
        self._raise_if_cancelled(cancel_event)

        async def _run() -> Any:
            self._raise_if_cancelled(cancel_event)
            return await operation()

        write_task = asyncio.create_task(_run())
        self._register_external_write(task_key, task_id, write_task)
        cancellation: asyncio.CancelledError | None = None
        result: Any = None
        write_error: BaseException | None = None
        try:
            while not write_task.done():
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError as exc:
                    # Keep waiting even when duplicate lifecycle cancellation
                    # injects another CancelledError into this worker task.
                    if cancellation is None:
                        cancellation = exc
                except BaseException:
                    # The child is done; retrieve its result below so its
                    # exception is observed and not reported as unhandled.
                    break

            try:
                result = write_task.result()
            except BaseException as exc:
                write_error = exc

            # Cancellation of the worker takes precedence after the external
            # write has drained.  This preserves normal task cancellation while
            # still observing any write exception for diagnostics.
            if cancellation is not None:
                raise cancellation
            if write_error is not None:
                raise write_error
            return result
        finally:
            self._unregister_external_write(task_key, task_id, write_task)

    @staticmethod
    async def _await_tasks_cancel_safe(
        tasks: list[asyncio.Task[Any]],
    ) -> list[Any]:
        """Await task handles without letting caller cancellation abort drain."""
        if not tasks:
            return []
        gathered = asyncio.gather(*tasks, return_exceptions=True)
        cancellation: asyncio.CancelledError | None = None
        while not gathered.done():
            try:
                await asyncio.shield(gathered)
            except asyncio.CancelledError as exc:
                # A cancelled webhook request must still wait for the worker
                # it has already signalled before its lifecycle cleanup ends.
                if cancellation is None:
                    cancellation = exc

        results = list(gathered.result())
        if cancellation is not None:
            raise cancellation
        return results

    def _get_execution(self, task_key: str, task_id: str) -> Any | None:
        return getattr(self, "_task_executions", {}).get(task_key, {}).get(task_id)

    def _get_execution_status(self, task_key: str, task_id: str) -> str | None:
        return (
            getattr(self, "_task_execution_statuses", {})
            .get(task_key, {})
            .get(task_id)
        )

    def _set_execution_status(
        self, task_key: str, task_id: str, status: str
    ) -> None:
        statuses = getattr(self, "_task_execution_statuses", None)
        if statuses is None:
            self._task_execution_statuses = statuses = {}
        statuses.setdefault(task_key, {})[task_id] = status

    def _get_analysis_record_identity(
        self, task_key: str, task_id: str
    ) -> tuple[int, int | None] | None:
        return (
            getattr(self, "_task_analysis_records", {})
            .get(task_key, {})
            .get(task_id)
        )

    def _unregister_task(self, task_key: str, task_id: str) -> None:
        """Remove only this task's signal; keep concurrent siblings intact."""
        task_events = getattr(self, "_cancel_events", {}).get(task_key)
        if task_events is not None:
            task_events.pop(task_id, None)
            if not task_events:
                self._cancel_events.pop(task_key, None)

        handles = getattr(self, "_task_handles", {}).get(task_key)
        if handles is not None:
            handles.pop(task_id, None)
            if not handles:
                self._task_handles.pop(task_key, None)

        records = getattr(self, "_task_analysis_records", {}).get(task_key)
        if records is not None:
            identity = records.pop(task_id, None)
            if identity is not None:
                getattr(self, "_cancelled_analysis_notifications", set()).discard(
                    identity[0]
                )
            if not records:
                self._task_analysis_records.pop(task_key, None)

        executions = getattr(self, "_task_executions", {}).get(task_key)
        if executions is not None:
            executions.pop(task_id, None)
            if not executions:
                self._task_executions.pop(task_key, None)

        statuses = getattr(self, "_task_execution_statuses", {}).get(task_key)
        if statuses is not None:
            statuses.pop(task_id, None)
            if not statuses:
                self._task_execution_statuses.pop(task_key, None)

    async def cancel_task(self, task_key: str) -> bool:
        """Cancel and await every active task for one Issue.

        Setting the event is still important for cooperative cancellation, but
        it does not wake a task suspended in ``Semaphore.acquire``.  Cancel the
        registered task handles as well and await their completion so lifecycle
        webhooks can safely delete/close the Issue after worker cleanup.
        """
        task_events = getattr(self, "_cancel_events", {}).get(task_key, {})
        if not task_events:
            return False
        task_handles = getattr(self, "_task_handles", {}).get(task_key, {})
        changed = False
        pending: list[asyncio.Task[Any]] = []
        pending_ids: list[str] = []
        current_task = asyncio.current_task()
        for task_id, event in list(task_events.items()):
            was_set = event.is_set()
            if not was_set:
                event.set()
                changed = True
            task = task_handles.get(task_id)
            # ``was_set`` only controls whether another cancellation request is
            # needed.  A duplicate close/delete webhook must still await a task
            # whose first cancellation is currently converging; otherwise its
            # lifecycle cleanup can race the first caller's ``gather``.
            if task is None or task is current_task:
                continue
            try:
                if not task.done():
                    if not was_set and task.cancel():
                        changed = True
                    pending.append(task)
                    pending_ids.append(task_id)
            except (AttributeError, RuntimeError):
                # A synthetic test/task factory may expose a stale handle.  The
                # event remains set and the normal worker cleanup still runs.
                continue

        # Include writes registered by a worker even if a task-handle mock or
        # an admission edge case has temporarily lost the outer handle.  In
        # the normal path the outer task awaits its writes itself, so this is
        # an idempotent safety net.
        pending_write_tasks: list[asyncio.Task[Any]] = []
        for task_id in task_events:
            pending_write_tasks.extend(self._get_external_writes(task_key, task_id))
        pending_all: list[asyncio.Task[Any]] = []
        seen: set[int] = set()
        for task in (*pending, *pending_write_tasks):
            marker = id(task)
            if marker not in seen:
                seen.add(marker)
                pending_all.append(task)

        drain_cancellation: asyncio.CancelledError | None = None
        if pending_all:
            try:
                results = await self._await_tasks_cancel_safe(pending_all)
            except asyncio.CancelledError as exc:
                # Finish local registry cleanup below, then preserve the
                # cancellation of the webhook request itself.
                drain_cancellation = exc
                results = []
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning(
                        "[cancel] Issue 任务终止时出现异常 {}: {}",
                        task_key,
                        result,
                    )
            # A task cancelled before its coroutine first runs never reaches
            # ``process_issue_analysis``'s finally block.  Remove those stale
            # pre-registrations explicitly after the handle has been awaited.
            for task_id in pending_ids:
                self._unregister_task(task_key, task_id)
        if changed:
            logger.info("[cancel] 已终止 Issue 分析任务: {}", task_key)
        if drain_cancellation is not None:
            raise drain_cancellation
        return changed

    @staticmethod
    def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ReviewCancelledError("Issue 分析已被取消")

    async def _mark_active_analysis_cancelled(
        self,
        db,
        *,
        analysis_id: int | None,
        reason: str,
    ) -> tuple[IssueAnalysis | None, str | None]:
        """Converge this task's exact analysis and return its terminal status.

        A worker may have siblings analyzing the same Issue concurrently.  The
        task-bound primary key is therefore mandatory; selecting by issue and
        ordering by ``created_at`` can cancel or overwrite the wrong sibling.

        The second tuple item is the persisted status after the race.  It lets
        callers distinguish a real ``active -> cancelled`` transition (or a
        close/delete that already committed ``cancelled``) from a completed or
        failed row, without publishing a misleading cancelled event.
        """
        if analysis_id is None:
            return None, None

        result = await db.execute(
            select(IssueAnalysis).where(IssueAnalysis.id == analysis_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None, None

        active_statuses = {
            IssueAnalysisStatus.PENDING.value,
            IssueAnalysisStatus.ANALYZING.value,
        }
        current_status = getattr(record, "status", None)
        if current_status not in active_statuses:
            # An already-cancelled row is a valid lifecycle cancellation that
            # the worker still needs to recognize, while completed/failed rows
            # must retain their terminal observability status.
            return record, current_status

        update_result = await db.execute(
            update(IssueAnalysis)
            .where(
                and_(
                    IssueAnalysis.id == analysis_id,
                    IssueAnalysis.status.in_(active_statuses),
                )
            )
            .values(
                status=IssueAnalysisStatus.CANCELLED.value,
                error_message=reason,
            )
        )
        await db.commit()

        rowcount = getattr(update_result, "rowcount", None)
        if rowcount is None or rowcount > 0:
            # Keep lightweight test doubles and the identity map in sync with
            # the conditional UPDATE without issuing a second SELECT.
            record.status = IssueAnalysisStatus.CANCELLED.value
            record.error_message = reason
            await self._log_activity(
                record.id,
                "cancelled",
                {"message": reason},
            )
            return record, IssueAnalysisStatus.CANCELLED.value
        elif getattr(record, "status", None) != IssueAnalysisStatus.CANCELLED.value:
            # Another lifecycle transaction won the race.  Read the exact row
            # only; a missing row means the Issue was deleted.
            current_result = await db.execute(
                select(IssueAnalysis).where(IssueAnalysis.id == analysis_id)
            )
            record = current_result.scalar_one_or_none()
            if record is None:
                return None, None

        return record, getattr(record, "status", None)

    async def _converge_cancelled_analysis(
        self,
        db,
        *,
        analysis_id: int | None,
        issue_info: dict[str, Any],
        reason: str,
    ) -> tuple[IssueAnalysis | None, str | None]:
        """Persist cancellation and publish the matching Issue status event."""
        record, status = await self._mark_active_analysis_cancelled(
            db,
            analysis_id=analysis_id,
            reason=reason,
        )
        if status != IssueAnalysisStatus.CANCELLED.value:
            return record, status

        if record is not None:
            notified = getattr(self, "_cancelled_analysis_notifications", None)
            if notified is None:
                self._cancelled_analysis_notifications = notified = set()
            already_notified = analysis_id in notified
            if not already_notified:
                # Set before the await so duplicate cancellation paths cannot
                # emit two SSE events for the same analysis row.
                notified.add(analysis_id)
            try:
                if not already_notified:
                    from backend.webui.sse import publish_event

                    await publish_event(
                        "issue:status_changed",
                        {
                            "issue_number": issue_info.get("issue_number"),
                            "repo_name": issue_info.get("repo_name"),
                            "status": IssueAnalysisStatus.CANCELLED.value,
                        },
                    )
            except Exception as sse_exc:
                logger.debug(
                    "[{}] 发布取消 SSE 事件失败: {}",
                    issue_info.get("task_id", "unknown"),
                    sse_exc,
                )
        return record, status

    async def _mark_analysis_failed(
        self,
        db,
        *,
        analysis_id: int | None,
        reason: str,
    ) -> IssueAnalysis | None:
        """Conditionally mark this task's exact analysis row as failed."""
        if analysis_id is None:
            return None

        result = await db.execute(
            select(IssueAnalysis).where(IssueAnalysis.id == analysis_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None

        update_result = await db.execute(
            update(IssueAnalysis)
            .where(
                and_(
                    IssueAnalysis.id == analysis_id,
                    IssueAnalysis.status.in_(
                        [
                            IssueAnalysisStatus.PENDING.value,
                            IssueAnalysisStatus.ANALYZING.value,
                        ]
                    ),
                    or_(
                        IssueAnalysis.issue_state.is_(None),
                        IssueAnalysis.issue_state != "closed",
                    ),
                )
            )
            .values(
                status=IssueAnalysisStatus.FAILED.value,
                error_message=reason,
            )
        )
        await db.commit()

        rowcount = getattr(update_result, "rowcount", None)
        if rowcount is None or rowcount > 0:
            record.status = IssueAnalysisStatus.FAILED.value
            record.error_message = reason
            return record

        # A close/delete/cancel may have won between the exact read and the
        # conditional update.  Return the exact current row for classification;
        # never fall back to a newer sibling analysis.
        current_result = await db.execute(
            select(IssueAnalysis).where(IssueAnalysis.id == analysis_id)
        )
        return current_result.scalar_one_or_none()

    @staticmethod
    async def _log_activity(
        analysis_id: int,
        event_type: str,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Legacy activity event hook — now a no-op.

        The new observability system (ActivityOutbox + user-scoped SSE, driven by
        ``execution.finish`` and the Attempt observer) replaces the legacy
        ``activity_events`` table and global ``activity:*`` SSE channel. This shim
        is retained so existing call sites remain harmless; it writes nothing.
        """
        return

    async def process_issue_analysis(
        self,
        issue_info: dict[str, Any],
        *,
        deadline: AITaskDeadline | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Register and run one Issue analysis task.

        Submission registers the event before creating the asyncio task.  Direct
        callers are supported as well, so this wrapper registers a missing event
        synchronously when the coroutine starts and always removes only its own
        task entry after the worker has finished.
        """
        task_id = str(issue_info.get("task_id") or uuid.uuid4())
        task_key = self._make_task_key(issue_info)
        task_events = getattr(self, "_cancel_events", {}).get(task_key, {})
        registered_event = task_events.get(task_id)
        if registered_event is None:
            registered_event = self._register_task(
                task_key,
                task_id,
                event=cancel_event,
            )
        current_task = asyncio.current_task()
        if current_task is not None:
            self._bind_task_handle(task_key, task_id, current_task)
        try:
            return await self._run_issue_analysis(
                issue_info,
                deadline=deadline,
                cancel_event=registered_event,
                task_id=task_id,
            )
        except asyncio.CancelledError as cancellation:
            # ``task.cancel()`` can interrupt the worker outside the nested DB
            # try block (notably while waiting for the semaphore).  If this
            # task already created a row, finish that exact row in a fresh
            # session after the original session has rolled back.
            registered_event.set()
            identity = self._get_analysis_record_identity(task_key, task_id)
            terminal_status = IssueAnalysisStatus.CANCELLED.value
            if identity is not None:
                try:
                    async with async_session() as cleanup_db:
                        _, persisted_status = await self._converge_cancelled_analysis(
                            cleanup_db,
                            analysis_id=identity[0],
                            issue_info=issue_info,
                            reason=str(cancellation) or "Issue 分析已被取消",
                        )
                    if persisted_status in {
                        IssueAnalysisStatus.COMPLETED.value,
                        IssueAnalysisStatus.FAILED.value,
                    }:
                        terminal_status = persisted_status
                except Exception as cleanup_exc:
                    logger.warning(
                        "[{}] 取消时收敛 Issue 分析记录失败: {}",
                        task_id,
                        cleanup_exc,
                    )
            execution = self._get_execution(task_key, task_id)
            if (
                execution is not None
                and self._get_execution_status(task_key, task_id) is None
            ):
                try:
                    await execution.finish(terminal_status, error_message=None)
                except Exception as finish_exc:
                    logger.warning(
                        "[{}] 取消时 issue observability finish 失败 (status={}): {}",
                        task_id,
                        terminal_status,
                        finish_exc,
                    )
                else:
                    self._set_execution_status(task_key, task_id, terminal_status)
            # Preserve normal asyncio cancellation semantics for direct callers;
            # ``cancel_task`` consumes this result while awaiting the handle.
            raise
        finally:
            self._unregister_task(task_key, task_id)

    async def _run_issue_analysis(
        self,
        issue_info: dict[str, Any],
        *,
        deadline: AITaskDeadline | None = None,
        cancel_event: asyncio.Event,
        task_id: str,
    ) -> str:
        """处理 Issue 分析任务

        Args:
            issue_info: Issue 信息（来自 webhook）

        Returns:
            任务ID
        """
        # Start the task budget before any semaphore wait; callers that already
        # created it (the background submission path) retain the same deadline.
        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
        repo_owner = issue_info.get("repo_owner", "")
        repo_name = issue_info.get("repo_name", "")
        issue_number = issue_info.get("issue_number", 0)
        repo_full_name = issue_info.get("repo_full_name", f"{repo_owner}/{repo_name}")
        task_key = self._make_task_key(issue_info)
        analysis_id: int | None = None
        analysis_record: IssueAnalysis | None = None

        logger.info(f"[{task_id}] 开始处理 Issue 分析: {repo_full_name}#{issue_number}")

        execution = None
        execution_status: str | None = None
        execution_target_status: str | None = None

        async def _finish_execution(
            status: str, *, error_message: str | None = None
        ) -> None:
            """Best-effort terminal convergence for the observability bundle."""
            nonlocal execution_status, execution_target_status
            if execution is None or execution_status is not None:
                return
            execution_target_status = status
            try:
                await execution.finish(status, error_message=error_message)
            except Exception as finish_exc:
                logger.warning(
                    "[{}] issue observability finish failed (status={}): {}",
                    task_id,
                    status,
                    finish_exc,
                )
                return
            execution_status = status
            self._set_execution_status(task_key, task_id, status)

        @asynccontextmanager
        async def _execution_scope():
            """Keep cancellation cleanup around semaphore and DB admission."""
            cancellation_pending = False
            try:
                semaphore = await _get_issue_semaphore()
                async with semaphore:
                    yield
            except asyncio.CancelledError:
                # The ORM record captured by this task may be stale: an
                # independent lifecycle transaction can complete/fail it while
                # this task is being cancelled.  Let the outer
                # ``process_issue_analysis`` handler refresh the exact row in a
                # fresh session and decide the execution terminal state.
                cancellation_pending = True
                raise
            finally:
                if not cancellation_pending and execution is not None and execution_status is None:
                    await _finish_execution(
                        execution_target_status or "failed",
                        error_message=(
                            "Issue analysis terminated without a terminal status"
                            if execution_target_status is None
                            else None
                        ),
                    )

        async def _start_execution_cancel_safe(**kwargs: Any) -> Any:
            """Shield observability admission until its execution is bound.

            ``start_execution`` persists the invocation/work unit and lease
            before it finishes constructing the returned bundle.  Cancelling
            this worker while that await is in progress must not strand that
            durable work unit without a handle that can finish and release it.
            """
            self._raise_if_cancelled(cancel_event)

            admission_task = asyncio.create_task(
                self.activity_integration.start_execution(**kwargs)
            )
            cancellation: asyncio.CancelledError | None = None
            result: Any = None
            admission_error: BaseException | None = None
            while not admission_task.done():
                try:
                    await asyncio.shield(admission_task)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                except BaseException:
                    # The child is complete; retrieve its exception below so
                    # it is observed and classified by the caller.
                    break

            try:
                result = admission_task.result()
            except BaseException as exc:
                admission_error = exc

            if admission_error is not None:
                if cancellation is not None:
                    raise cancellation
                raise admission_error

            if not getattr(result, "merged", False):
                # Bind before propagating the cancellation.  The outer
                # process cancellation handler can then finish the exact
                # execution and release its lease.
                self._bind_execution(task_key, task_id, result)

            if cancellation is not None:
                raise cancellation
            return result

        try:
            admission = await self.activity_integration.admit_issue(
                issue_info,
                delivery_id=issue_info.get("delivery_id") or str(task_id),
                actor_id=str(
                    issue_info.get("actor_id") or issue_info.get("sender") or "worker"
                ),
                base_sha=issue_info.get("base_sha"),
                head_sha=issue_info.get("head_sha"),
            )
            role_snapshot = None
            analyzer_api_client = getattr(self.analyzer, "api_client", None)
            resolver = getattr(
                analyzer_api_client, "resolve_role_config_snapshot", None
            )
            if resolver is not None:
                role_snapshot = await resolver("main")
            execution = await _start_execution_cancel_safe(
                session_id=admission.session_id,
                trigger_ids=[admission.trigger_id],
                role_snapshot=role_snapshot,
                role="main",
                task_type="issue",
                task_id=None,
            )
            if getattr(execution, "merged", False):
                logger.info(
                    "[{}] Issue Trigger 已合并到正在运行的 Invocation {}，当前 Worker 不重复执行",
                    task_id,
                    execution.invocation_id,
                )
                return task_id
        except Exception as observability_exc:
            # Observability admission is best-effort for legacy callers that lack
            # immutable repository identity; the core analysis still runs, but
            # no invocation/attempt is recorded.  Once admission has succeeded,
            # downstream failures are handled via ``execution.finish`` below.
            logger.warning(
                "[{}] issue observability admission skipped: {}",
                task_id,
                observability_exc,
            )

        # 获取并发信号量，限制同时运行的 Issue 分析任务数。
        # The scope also owns cancellation cleanup for a started execution.
        async with _execution_scope():
            async with async_session() as db:
                try:
                    # Check after semaphore admission so a queued task that was
                    # cancelled never creates an analysis record or calls AI.
                    self._raise_if_cancelled(cancel_event)
                    # 1. 计算下一个分析版本号
                    max_version = await db.scalar(
                        select(func.max(IssueAnalysis.analysis_version)).where(
                            and_(
                                IssueAnalysis.repo_name == repo_name,
                                IssueAnalysis.repo_owner == repo_owner,
                                IssueAnalysis.issue_number == issue_number,
                            )
                        )
                    )
                    next_version = (max_version or 0) + 1

                    # 创建分析记录（PENDING）
                    record = IssueAnalysis(
                        issue_number=issue_number,
                        repo_name=repo_name,
                        repo_owner=repo_owner,
                        author=issue_info.get("author", ""),
                        title=issue_info.get("title", ""),
                        body=issue_info.get("body", ""),
                        status=IssueAnalysisStatus.PENDING.value,
                        analysis_version=next_version,
                        issue_state=issue_info.get("state", "open"),
                    )
                    db.add(record)
                    await db.commit()
                    analysis_id = record.id
                    analysis_record = record
                    self._bind_analysis_record(task_key, task_id, record)
                    await db.refresh(record)

                    # 2. 更新状态为 ANALYZING
                    record.status = IssueAnalysisStatus.ANALYZING.value
                    await db.commit()

                    await self._log_activity(
                        record.id,
                        "status",
                        {
                            "status": "analyzing",
                            "message": f"开始分析 Issue #{issue_number}",
                            "repo_name": repo_name,
                        },
                    )
                    await self._log_activity(
                        record.id,
                        "thinking",
                        {
                            "message": "AI 正在分析 Issue ...",
                        },
                    )

                    # 发布 SSE 事件通知前端
                    try:
                        from backend.webui.sse import publish_event

                        await publish_event(
                            "issue:status_changed",
                            {
                                "issue_number": issue_info.get("issue_number"),
                                "repo_name": issue_info.get("repo_name"),
                                "status": "analyzing",
                            },
                        )
                    except Exception as e:
                        logger.warning(f"发布 SSE 事件失败（不影响主流程）: {e}")

                    # 3. 获取 repo 对象
                    self._raise_if_cancelled(cancel_event)
                    client = self.github_app.get_repo_client(repo_owner, repo_name)
                    repo = None
                    if client:
                        self._raise_if_cancelled(cancel_event)
                        repo = client.get_repo(repo_full_name)

                    # 4. 调用 AI 分析：对话流持久化到新可观测性 Canonical Transcript
                    # （同一 Issue 的长期 issue_analyzer Thread），替代旧 checkpoint 表。
                    async def _issue_event_callback(event_type, data):
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
                                await (
                                    execution.tool_service.append_conversation_message(
                                        thread_id=execution.thread.id,
                                        work_unit_id=execution.work_unit.id,
                                        message=data,
                                        origin_attempt_id=origin_attempt_id,
                                        lease=execution.lease,
                                    )
                                )
                                if data.get("role") == "tool" and data.get(
                                    "tool_call_id"
                                ):
                                    if execution.tool_service.is_failed_tool_result(
                                        data
                                    ):
                                        await execution.tool_service.mark_tool_execution_failed(
                                            execution.work_unit.id,
                                            data["tool_call_id"],
                                        )
                                    else:
                                        await execution.tool_service.mark_tool_execution_completed(
                                            execution.work_unit.id, data["tool_call_id"]
                                        )
                            elif event_type == "tool_running":
                                await (
                                    execution.tool_service.mark_tool_execution_running(
                                        execution.work_unit.id, data
                                    )
                                )
                        except Exception as exc:
                            logger.debug("issue observability callback failed: {}", exc)

                    self._raise_if_cancelled(cancel_event)
                    analysis_result = await self.analyzer.analyze_issue(
                        issue_info=issue_info,
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        repo=repo,
                        event_callback=_issue_event_callback,
                        publication_coordinator=(
                            execution.publication_coordinator
                            if execution is not None
                            else None
                        ),
                        invocation_context=(
                            execution.invocation_context
                            if execution is not None
                            else None
                        ),
                        observer=execution.observer if execution is not None else None,
                        cancel_event=cancel_event,
                        deadline=task_deadline,
                    )
                    self._raise_if_cancelled(cancel_event)

                    # WorkUnit 终态由 execution.finish("completed") 统一收敛（见下方
                    # 成功路径），分析结果摘要持久化在 IssueAnalysis 记录中。

                    # 5. 保存分析结果（更新已有的 PENDING 记录）
                    self._raise_if_cancelled(cancel_event)
                    analysis_record = await issue_service.save_analysis_result(
                        analysis_result,
                        issue_info,
                        db,
                        analysis_id=analysis_id,
                    )

                    if not analysis_record:
                        # The conditional save can lose a close/cancel/delete
                        # race after the worker's initial read.  This is a
                        # normal stale-worker outcome, not an analysis failure.
                        logger.info(
                            "[{}] Issue 分析记录已由生命周期操作收敛，跳过后续副作用",
                            task_id,
                        )
                        try:
                            _, cancellation_status = await self._converge_cancelled_analysis(
                                db,
                                analysis_id=analysis_id,
                                issue_info=issue_info,
                                reason="Issue 分析记录已关闭或被删除",
                            )
                        except Exception as cancel_exc:
                            logger.warning(
                                "[{}] 保存竞态后的取消收敛失败: {}",
                                task_id,
                                cancel_exc,
                            )
                            cancellation_status = None
                        await _finish_execution(
                            cancellation_status
                            if cancellation_status
                            in {
                                IssueAnalysisStatus.COMPLETED.value,
                                IssueAnalysisStatus.FAILED.value,
                            }
                            else "cancelled"
                        )
                        return task_id

                    # 5.1 关联扫描记录（如果此 Issue 来自仓库扫描）
                    self._raise_if_cancelled(cancel_event)
                    try:
                        from backend.models.scan_models import RepoScan

                        scan = await db.scalar(
                            select(RepoScan).where(
                                RepoScan.report_issue_number == issue_number
                            )
                        )
                        if scan and not scan.issue_analysis_id:
                            scan.issue_analysis_id = analysis_record.id
                            self._raise_if_cancelled(cancel_event)
                            await db.commit()
                            logger.info(
                                f"[{task_id}] 已关联扫描记录到分析: scan_id={scan.id}"
                            )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 关联扫描记录失败: {e}")

                    # 5.5 使用 AI 摘要更新 Issue 向量
                    self._raise_if_cancelled(cancel_event)
                    try:
                        from backend.services.issue_embedding_service import (
                            IssueEmbeddingService,
                        )

                        summary = analysis_result.get("summary", "")
                        if summary and not task_deadline.is_expired():
                            emb_service = IssueEmbeddingService()
                            analysis_metadata = {
                                "category": analysis_result.get("category", ""),
                                "priority": analysis_result.get("priority", ""),
                                "feasibility": analysis_result.get("feasibility", ""),
                            }
                            self._raise_if_cancelled(cancel_event)
                            await emb_service.upsert_issue(
                                repo_owner,
                                repo_name,
                                issue_number,
                                title=issue_info.get("title", ""),
                                body=summary,
                                state=issue_info.get("state", "open"),
                                analysis_metadata=analysis_metadata,
                            )
                            logger.info(f"[{task_id}] 已使用 AI 摘要更新 Issue 向量")
                        elif summary:
                            logger.info(
                                f"[{task_id}] 软 deadline 已到达，"
                                "跳过 Issue 向量辅助调用"
                            )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 使用 AI 摘要更新向量失败: {e}")

                    # 6. 重复检测（优先使用 AI 摘要）
                    self._raise_if_cancelled(cancel_event)
                    if (
                        not task_deadline.is_expired()
                        and await get_dynamic_config("issue_detect_duplicates")
                    ):
                        self._raise_if_cancelled(cancel_event)
                        try:
                            summary = analysis_result.get("summary", "")
                            duplicates = await issue_service.detect_duplicates(
                                repo_owner,
                                repo_name,
                                issue_info.get("title", ""),
                                summary or issue_info.get("body", ""),
                                current_issue_number=issue_number,
                            )
                            if duplicates:
                                analysis_record.duplicate_of = duplicates[0].get(
                                    "issue_number"
                                )
                        except ReviewCancelledError:
                            raise
                        except Exception as e:
                            logger.warning(f"[{task_id}] 重复检测失败: {e}")

                    # 7. 查找关联 PR
                    self._raise_if_cancelled(cancel_event)
                    try:
                        related_prs = await issue_service.find_related_prs(
                            repo_owner, repo_name, issue_number
                        )
                        if related_prs:
                            analysis_record.related_prs = json.dumps(
                                related_prs, ensure_ascii=False
                            )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 查找关联 PR 失败: {e}")

                    self._raise_if_cancelled(cancel_event)
                    await db.commit()

                    # 发布 SSE 事件通知前端（完成）
                    self._raise_if_cancelled(cancel_event)
                    try:
                        from backend.webui.sse import publish_event

                        await publish_event(
                            "issue:status_changed",
                            {
                                "issue_number": issue_info.get("issue_number"),
                                "repo_name": issue_info.get("repo_name"),
                                "status": "completed",
                            },
                        )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"发布 SSE 事件失败（不影响主流程）: {e}")

                    self._raise_if_cancelled(cancel_event)
                    await self._log_activity(
                        record.id,
                        "result",
                        {
                            "status": "completed",
                            "message": f"Issue #{issue_number} 分析完成",
                            "category": analysis_result.get("category", ""),
                            "priority": analysis_result.get("priority", ""),
                        },
                    )

                    # 8. 自动评论
                    self._raise_if_cancelled(cancel_event)
                    try:
                        activity_result_id = analysis_result.get("_activity_result_id")
                        if execution is not None and isinstance(
                            activity_result_id, int
                        ):
                            body = issue_service.build_analysis_comment(analysis_record)
                            if not body:
                                success = False
                            else:
                                self._raise_if_cancelled(cancel_event)
                                publication = (
                                    await execution.publication_service.create_pending(
                                        activity_result_id,
                                        "issue_comment",
                                        (
                                            f"issue-comment-{execution.session.id}-"
                                            f"{execution.invocation.id}-"
                                            f"{execution.work_unit.id}"
                                        ),
                                    )
                                )
                                resource_identity = {
                                    "source_system_instance": issue_info.get(
                                        "source_system_instance", "github.com"
                                    ),
                                    "repository_external_id": issue_info.get(
                                        "repository_external_id", repo_full_name
                                    ),
                                    "resource_type": "issue",
                                    "resource_number": str(issue_number),
                                }

                                async def _sender(
                                    _kind: str,
                                    body_with_marker: str,
                                    _resource_identity: dict[str, Any],
                                ) -> Any:
                                    return await self._run_external_write(
                                        task_key,
                                        task_id,
                                        cancel_event,
                                        lambda: asyncio.to_thread(
                                            issue_service.github_app.create_issue_comment,
                                            repo_owner,
                                            repo_name,
                                            issue_number,
                                            body_with_marker,
                                            raise_on_error=True,
                                        ),
                                    )

                                self._raise_if_cancelled(cancel_event)
                                terminal = await execution.publication_service.send(
                                    publication.id,
                                    body=body,
                                    sender=_sender,
                                    resource_identity=resource_identity,
                                )
                                success = terminal.status == "succeeded"
                                if success:
                                    analysis_record.comment_posted = 1
                                    await db.commit()
                        else:
                            self._raise_if_cancelled(cancel_event)
                            success = await self._run_external_write(
                                task_key,
                                task_id,
                                cancel_event,
                                lambda: issue_service.post_analysis_comment(
                                    repo_owner,
                                    repo_name,
                                    issue_number,
                                    analysis_record,
                                    db,
                                ),
                            )
                        if success:
                            logger.info(f"[{task_id}] 已发布分析评论")
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 发布评论失败: {e}")

                    # 10. 应用建议标签；enabled、阈值与 auto_create 由 IssueService 统一处理。
                    self._raise_if_cancelled(cancel_event)
                    try:
                        labels_data = json.loads(
                            analysis_record.suggested_labels or "[]"
                        )
                        if labels_data:
                            self._raise_if_cancelled(cancel_event)
                            result = await self._run_external_write(
                                task_key,
                                task_id,
                                cancel_event,
                                lambda: issue_service.apply_suggested_labels(
                                    repo_owner,
                                    repo_name,
                                    issue_number,
                                    labels_data,
                                    db,
                                    cancellation_checkpoint=lambda: self._raise_if_cancelled(
                                        cancel_event
                                    ),
                                ),
                            )
                            if result.get("applied"):
                                logger.info(
                                    f"[{task_id}] 已应用标签: "
                                    f"{[label['name'] for label in result['applied']]}"
                                )
                            if result.get("created"):
                                logger.info(
                                    f"[{task_id}] 已创建标签: {result['created']}"
                                )
                            if result.get("failed"):
                                logger.warning(
                                    f"[{task_id}] 标签应用失败: {result['failed']}"
                                )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 应用标签失败: {e}")

                    # 10.5 应用建议指派人
                    self._raise_if_cancelled(cancel_event)
                    if await get_dynamic_config("issue_auto_assign"):
                        self._raise_if_cancelled(cancel_event)
                        try:
                            assignees_data = json.loads(
                                analysis_record.suggested_assignees or "[]"
                            )
                            if assignees_data:
                                self._raise_if_cancelled(cancel_event)
                                assign_result = (
                                    await self._run_external_write(
                                        task_key,
                                        task_id,
                                        cancel_event,
                                        lambda: issue_service.apply_suggested_assignees(
                                            repo_owner,
                                            repo_name,
                                            issue_number,
                                            assignees_data,
                                            cancellation_checkpoint=lambda: self._raise_if_cancelled(
                                                cancel_event
                                            ),
                                        ),
                                    )
                                )
                                if assign_result.get("applied"):
                                    logger.info(
                                        f"[{task_id}] 已指派: "
                                        f"{[a['username'] for a in assign_result['applied']]}"
                                    )
                                if assign_result.get("failed"):
                                    logger.warning(
                                        f"[{task_id}] 指派失败: "
                                        f"{[a['username'] for a in assign_result['failed']]}"
                                    )
                        except ReviewCancelledError:
                            raise
                        except Exception as e:
                            logger.warning(f"[{task_id}] 应用指派人失败: {e}")

                    # 10.7 自动改写标题（优先从 DB 读取配置）
                    self._raise_if_cancelled(cancel_event)
                    issue_auto_rewrite_title = await get_dynamic_config(
                        "issue_auto_rewrite_title"
                    )

                    if issue_auto_rewrite_title:
                        self._raise_if_cancelled(cancel_event)
                        try:
                            suggested_title = analysis_record.suggested_title
                            original_title = issue_info.get("title", "")
                            if suggested_title and suggested_title != original_title:
                                self._raise_if_cancelled(cancel_event)
                                success = await self._run_external_write(
                                    task_key,
                                    task_id,
                                    cancel_event,
                                    lambda: asyncio.to_thread(
                                        self.github_app.update_issue_title,
                                        repo_owner,
                                        repo_name,
                                        issue_number,
                                        suggested_title,
                                    ),
                                )
                                if success:
                                    logger.info(
                                        f"[{task_id}] 已改写标题: {suggested_title}"
                                    )
                        except ReviewCancelledError:
                            raise
                        except Exception as e:
                            logger.warning(f"[{task_id}] 改写标题失败: {e}")

                    # 11. Critical 告警
                    category = analysis_result.get("category", "")
                    priority = analysis_result.get("priority", "")

                    # 收集通知目标：作者 + 订阅者
                    notification_chat_ids = []
                    self._raise_if_cancelled(cancel_event)
                    try:
                        from backend.services.telegram_service import TelegramService

                        ts = TelegramService(db)
                        notification_chat_ids = await ts.get_notification_targets(
                            repo_full_name, issue_info.get("author", "")
                        )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 获取通知目标失败: {e}")

                    if priority == "critical":
                        self._raise_if_cancelled(cancel_event)
                        try:
                            from backend.telegram.notifications import (
                                get_notification_sender,
                            )

                            sender = get_notification_sender()
                            if sender and notification_chat_ids:
                                self._raise_if_cancelled(cancel_event)
                                await sender.send_critical_issue_alert(
                                    repo_name=repo_full_name,
                                    issue_number=issue_number,
                                    title=issue_info.get("title", ""),
                                    category=category,
                                    summary=analysis_result.get("summary", ""),
                                    feasibility=analysis_result.get("feasibility", ""),
                                    issue_url=issue_info.get("html_url", ""),
                                    suggested_labels=analysis_result.get(
                                        "suggested_labels", []
                                    ),
                                    chat_ids=notification_chat_ids,
                                )
                                logger.info(f"[{task_id}] 已发送 Critical Issue 告警")
                        except ReviewCancelledError:
                            raise
                        except Exception as e:
                            logger.warning(f"[{task_id}] 发送告警失败: {e}")

                    # 12. 发送完成通知
                    self._raise_if_cancelled(cancel_event)
                    try:
                        from backend.telegram.notifications import (
                            get_notification_sender,
                        )

                        sender = get_notification_sender()
                        if sender and notification_chat_ids:
                            self._raise_if_cancelled(cancel_event)
                            await sender.send_issue_analysis_complete(
                                repo_name=repo_full_name,
                                issue_number=issue_number,
                                category=category,
                                priority=priority,
                                issue_url=issue_info.get("html_url", ""),
                                summary=analysis_result.get("summary"),
                                chat_ids=notification_chat_ids,
                            )
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[{task_id}] 发送完成通知失败: {e}")

                    # 13. 异步触发 .sakura/ Issue 反思 / Trigger .sakura/ issue reflection async
                    self._raise_if_cancelled(cancel_event)
                    try:
                        sm_config = get_sakura_memory_config()
                        if (
                            not task_deadline.is_expired()
                            and sm_config.get("enabled", True)
                            and sm_config.get(
                                "issue_reflection", {}
                            ).get("enabled", True)
                        ):
                            self._raise_if_cancelled(cancel_event)
                            from backend.services.sakura_memory_service import (
                                get_sakura_memory_service,
                            )

                            sakura_memory_service = get_sakura_memory_service()
                            ensure_background_admission("issue_reflection")
                            self._raise_if_cancelled(cancel_event)
                            task = asyncio.create_task(
                                sakura_memory_service.reflect_issue(
                                    repo=repo,
                                    repo_full_name=repo_full_name,
                                    issue_number=issue_number,
                                    issue_info=issue_info,
                                    analysis_result=analysis_result,
                                    analysis_record=analysis_record,
                                )
                            )
                            try:
                                register_background_task(task, "issue_reflection")
                            except DatabaseResetRuntimeAdmissionClosed:
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                                raise
                            self._background_tasks.add(task)
                            task.add_done_callback(self._background_tasks.discard)
                            logger.info(f"[{task_id}] 已触发 .sakura/ Issue 反思任务")
                    except ReviewCancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            f"[{task_id}] 触发 .sakura/ Issue 反思失败（不影响分析）: {e}"
                        )

                    logger.info(
                        "[{}] Issue 分析完成: {}#{} | 轮数={}, tokens={}+{}, cost={}",
                        task_id,
                        repo_full_name,
                        issue_number,
                        analysis_result.get("tool_rounds", "?"),
                        analysis_result.get("prompt_tokens", 0),
                        analysis_result.get("completion_tokens", 0),
                        analysis_result.get("estimated_cost", 0),
                    )
                    self._raise_if_cancelled(cancel_event)
                    if execution is not None:
                        await _finish_execution("completed")

                except ReviewCancelledError as e:
                    logger.info("[{}] Issue 分析已取消: {}", task_id, e)
                    cancellation_status = None
                    try:
                        _, cancellation_status = await self._converge_cancelled_analysis(
                            db,
                            analysis_id=analysis_id,
                            issue_info=issue_info,
                            reason=str(e),
                        )
                    except Exception as cancel_exc:
                        logger.warning(
                            "[{}] 收敛 Issue 取消状态失败: {}",
                            task_id,
                            cancel_exc,
                        )
                    await _finish_execution(
                        cancellation_status
                        if cancellation_status
                        in {
                            IssueAnalysisStatus.COMPLETED.value,
                            IssueAnalysisStatus.FAILED.value,
                        }
                        else "cancelled"
                    )
                    return task_id
                except Exception as e:
                    # A cancellation signal may race a provider/DB exception.
                    # Once the lifecycle is cancelled/closed, never overwrite
                    # it with FAILED.
                    lifecycle_cancelled = cancel_event.is_set()
                    current_record = None
                    try:
                        if analysis_id is not None and not lifecycle_cancelled:
                            state_result = await db.execute(
                                select(IssueAnalysis).where(
                                    IssueAnalysis.id == analysis_id
                                )
                            )
                            current_record = state_result.scalar_one_or_none()
                            current_status = getattr(current_record, "status", None)
                            lifecycle_cancelled = current_record is None or (
                                current_status == IssueAnalysisStatus.CANCELLED.value
                                or (
                                    current_status
                                    not in {
                                        IssueAnalysisStatus.COMPLETED.value,
                                        IssueAnalysisStatus.FAILED.value,
                                    }
                                    and getattr(current_record, "issue_state", None)
                                    == "closed"
                                )
                            )
                    except Exception as state_exc:
                        logger.debug(
                            "[{}] 检查 Issue 生命周期状态失败: {}",
                            task_id,
                            state_exc,
                        )

                    if lifecycle_cancelled:
                        cancellation_status = None
                        try:
                            _, cancellation_status = await self._converge_cancelled_analysis(
                                db,
                                analysis_id=analysis_id,
                                issue_info=issue_info,
                                reason=str(e) or "Issue 分析已被取消",
                            )
                        except Exception as cancel_exc:
                            logger.warning(
                                "[{}] 异常后收敛 Issue 取消状态失败: {}",
                                task_id,
                                cancel_exc,
                            )
                        await _finish_execution(
                            cancellation_status
                            if cancellation_status
                            in {
                                IssueAnalysisStatus.COMPLETED.value,
                                IssueAnalysisStatus.FAILED.value,
                            }
                            else "cancelled"
                        )
                        return task_id

                    logger.error(f"[{task_id}] Issue 分析失败: {e}", exc_info=True)

                    # Update FAILED only for this task's active exact row.  A
                    # rowcount of zero means another lifecycle transition won;
                    # classify it as cancellation rather than failure.
                    failed_record = None
                    try:
                        failed_record = await self._mark_analysis_failed(
                            db,
                            analysis_id=analysis_id,
                            reason=str(e),
                        )
                        failed_status = getattr(failed_record, "status", None)
                        failed_is_cancelled = analysis_id is not None and (
                            failed_record is None
                            or (
                                failed_status
                                not in {
                                    IssueAnalysisStatus.COMPLETED.value,
                                    IssueAnalysisStatus.FAILED.value,
                                }
                                and (
                                    failed_status
                                    == IssueAnalysisStatus.CANCELLED.value
                                    or getattr(failed_record, "issue_state", None)
                                    == "closed"
                                )
                            )
                        )
                        if failed_is_cancelled:
                            _, cancellation_status = await self._converge_cancelled_analysis(
                                db,
                                analysis_id=analysis_id,
                                issue_info=issue_info,
                                reason=str(e) or "Issue 分析已被取消",
                            )
                            await _finish_execution(
                                cancellation_status
                                if cancellation_status
                                in {
                                    IssueAnalysisStatus.COMPLETED.value,
                                    IssueAnalysisStatus.FAILED.value,
                                }
                                else "cancelled"
                            )
                            return task_id

                        if failed_status == IssueAnalysisStatus.COMPLETED.value:
                            await _finish_execution("completed")
                            return task_id

                        if failed_record is not None:
                            await self._log_activity(
                                failed_record.id,
                                "error",
                                {
                                    "message": f"Issue 分析失败: {e!s}",
                                },
                            )

                            # 发布 SSE 事件通知前端（失败）
                            try:
                                from backend.webui.sse import publish_event

                                await publish_event(
                                    "issue:status_changed",
                                    {
                                        "issue_number": issue_info.get(
                                            "issue_number"
                                        ),
                                        "repo_name": issue_info.get("repo_name"),
                                        "status": "failed",
                                    },
                                )
                            except Exception:
                                pass
                    except Exception as failure_exc:
                        logger.warning(
                            "[{}] 更新 Issue 失败状态失败: {}",
                            task_id,
                            failure_exc,
                        )

                    if execution is not None:
                        await _finish_execution("failed", error_message=str(e))

        return task_id


_worker_instance: IssueWorker | None = None


def get_issue_worker() -> IssueWorker:
    """获取 IssueWorker 实例"""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = IssueWorker()
    return _worker_instance


async def submit_issue_analysis_task(issue_info: dict[str, Any]) -> str:
    """提交 Issue 分析任务"""
    ensure_background_admission("issue")
    task_id = str(uuid.uuid4())
    issue_info["task_id"] = task_id
    worker = get_issue_worker()
    deadline = AITaskDeadline.from_timeout(get_settings().review_timeout_seconds)
    task_key = worker._make_task_key(issue_info)
    cancel_event = worker._register_task(
        task_key,
        task_id,
    )
    task: asyncio.Task[Any] | None = None
    try:
        task = asyncio.create_task(
            worker.process_issue_analysis(
                issue_info,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        )
        worker._bind_task_handle(task_key, task_id, task)
        register_background_task(task, "issue")
    except DatabaseResetRuntimeAdmissionClosed:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_exc:
                logger.warning(
                    "[{}] 清理未登记的 Issue 任务失败: {}",
                    task_id,
                    cleanup_exc,
                )
        # The coroutine may never have started, so its ``finally`` cannot
        # unregister the pre-created entry.  Always remove it explicitly when
        # admission rejects the background handle.
        worker._unregister_task(task_key, task_id)
        raise
    except BaseException:
        # Do not leak a pre-registration if task creation itself is rejected.
        if task is None:
            worker._unregister_task(task_key, task_id)
        raise
    worker._background_tasks.add(task)
    task.add_done_callback(worker._background_tasks.discard)
    return task_id
