"""运行时停机协调，用于数据库全量重置前的安全静默。

数据库重置会在 ``before_drop`` 回调返回后立即执行 DDL。该模块提供一个很
小的、应用级 supervisor：先关闭后台任务 admission，再停止调度器，取消并
等待已经开始的后台任务，最后清理 SSE 与 activity outbox。任何未能完成的
步骤都会抛出异常，因此调用方不会在运行时仍可能访问数据库时继续 DROP。

Worker 模块不持有 FastAPI ``app``，所以 admission supervisor 通过当前
asyncio context 暴露。应用 lifespan 或 HTTP middleware 会在启动/请求最早
阶段绑定 app supervisor；没有绑定时拒绝创建后台任务，以免产生
无法被清库流程看到的孤儿 task。
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Iterable
from typing import Any

from loguru import logger

_OUTBOX_STOP_TIMEOUT_SECONDS = 5.0
_SSE_STOP_TIMEOUT_SECONDS = 5.0
_BACKGROUND_STOP_TIMEOUT_SECONDS = 5.0
_REQUEST_DRAIN_TIMEOUT_SECONDS = 30.0


class DatabaseResetRuntimeError(RuntimeError):
    """运行时无法安全静默的基类异常。"""


class DatabaseResetRuntimeAdmissionClosed(DatabaseResetRuntimeError):
    """数据库重置已经开始，新的数据库后台工作被拒绝。"""


class DatabaseResetRuntimeBindingError(DatabaseResetRuntimeError):
    """当前 task 没有绑定 app runtime supervisor。"""


class DatabaseResetRuntimeQuiesceError(DatabaseResetRuntimeError):
    """后台任务、调度器、SSE 或 Outbox 未能完成静默。"""


_runtime_supervisor_context: contextvars.ContextVar[
    DatabaseResetRuntimeSupervisor | None
] = contextvars.ContextVar("sakura_database_reset_runtime_supervisor", default=None)


def _task_cancelled(task: Any) -> bool:
    """兼容 asyncio.Task 与测试中最小 FakeTask 协议。"""

    checker = getattr(task, "cancelled", None)
    return bool(checker()) if callable(checker) else False


def _task_done(task: Any) -> bool:
    checker = getattr(task, "done", None)
    return bool(checker()) if callable(checker) else False


async def _await_bounded(awaitable: Any, timeout: float) -> Any:
    """Await an async stop hook without waiting forever for cancellation.

    ``asyncio.wait_for(coro)`` cancels ``coro`` and, on modern Python, waits for
    that cancellation to finish. A broken stop hook can ignore cancellation and
    therefore defeat the timeout. Shielding the owned task lets us return a
    diagnostic failure promptly while the still-running hook remains visible to
    the caller's fail-closed state.
    """

    pending = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(pending), timeout=timeout)
    except TimeoutError:
        pending.cancel()

        def _consume(done: asyncio.Future[Any]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except asyncio.CancelledError, Exception:
                return

        pending.add_done_callback(_consume)
        raise


class DatabaseResetRuntimeSupervisor:
    """跟踪数据库相关后台任务并协调可重复的 quiesce。

    ``register_task`` 和 ``ensure_admission`` 都是无 ``await`` 的同步操作，因而
    在单线程 asyncio event loop 中不会在 admission 与 ``create_task`` 之间
    产生竞态。所有被跟踪的任务都在 quiesce 中明确 await；不会使用
    ``wait=False`` 穿过数据库 DDL。
    """

    def __init__(
        self,
        *,
        background_timeout: float = _BACKGROUND_STOP_TIMEOUT_SECONDS,
        request_timeout: float = _REQUEST_DRAIN_TIMEOUT_SECONDS,
    ):
        self.accepting = True
        self.quiescing = False
        self.quiesced = False
        self.background_timeout = max(float(background_timeout), 0.01)
        self.request_timeout = max(float(request_timeout), 0.01)
        self._tasks: dict[Any, str] = {}
        self._schedulers: list[Any] = []
        self._requests: dict[object, tuple[asyncio.Task[Any], str]] = {}
        self._request_changed = asyncio.Event()
        self._quiesce_lock = asyncio.Lock()

    @property
    def tasks(self) -> tuple[Any, ...]:
        """返回仍被 supervisor 引用的任务（诊断/测试用只读快照）。"""

        return tuple(self._tasks)

    @property
    def schedulers(self) -> tuple[Any, ...]:
        """返回已注册调度器的只读快照。"""

        return tuple(self._schedulers)

    @property
    def requests(self) -> tuple[tuple[asyncio.Task[Any], str], ...]:
        """返回仍持有数据库 session 的请求快照。"""

        return tuple(self._requests.values())

    def ensure_admission(self, source: str) -> None:
        """在创建新的 DB-backed 后台 task 前检查 gate。"""

        if not self.accepting or self.quiescing:
            raise DatabaseResetRuntimeAdmissionClosed(
                f"database reset runtime gate is closed (source={source})"
            )

    def register_request(self, source: str) -> object:
        """登记当前持有数据库 session 的 HTTP 请求并返回释放句柄。"""

        self.ensure_admission(source)
        task = asyncio.current_task()
        if task is None:
            raise DatabaseResetRuntimeBindingError(
                "database-backed request is not running in an asyncio task"
            )
        lease = object()
        self._requests[lease] = (task, source)
        return lease

    def release_request(self, lease: object) -> None:
        """释放 ``register_request`` 返回的请求句柄。"""

        if self._requests.pop(lease, None) is not None:
            self._request_changed.set()

    def _active_requests(
        self, *, exclude: asyncio.Task[Any] | None
    ) -> list[tuple[asyncio.Task[Any], str]]:
        active: list[tuple[asyncio.Task[Any], str]] = []
        for lease, (task, source) in tuple(self._requests.items()):
            if _task_done(task):
                self._requests.pop(lease, None)
                continue
            if task is not exclude:
                active.append((task, source))
        return active

    async def wait_for_requests(self) -> None:
        """等待 reset 请求之外的既有数据库请求释放 session。"""

        current = asyncio.current_task()
        deadline = asyncio.get_running_loop().time() + self.request_timeout
        while True:
            active = self._active_requests(exclude=current)
            if not active:
                return
            self._request_changed.clear()
            # ``release_request`` 与本方法运行在同一个 event loop；clear 后再次
            # 检查可避免在进入 wait 前丢失完成通知。
            active = self._active_requests(exclude=current)
            if not active:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                sources = ",".join(source for _task, source in active)
                raise DatabaseResetRuntimeQuiesceError(
                    "database-backed requests did not finish before timeout "
                    f"({self.request_timeout:.2f}s); sources={sources}"
                )
            try:
                await asyncio.wait_for(self._request_changed.wait(), timeout=remaining)
            except TimeoutError as exc:
                sources = ",".join(source for _task, source in active)
                raise DatabaseResetRuntimeQuiesceError(
                    "database-backed requests did not finish before timeout "
                    f"({self.request_timeout:.2f}s); sources={sources}"
                ) from exc

    def register_task(
        self, task: Any, source: str, *, allow_closed: bool = False
    ) -> Any:
        """登记一个已经创建的 task，并在 task 完成时移除引用。

        Gate 在 task 创建和登记之间关闭时，登记会失败；调用方必须取消并
        await 该 task 后再把拒绝异常传给上层，避免悬挂 coroutine。
        """

        if not allow_closed:
            self.ensure_admission(source)
        self._tasks[task] = source

        def _discard(done: asyncio.Task[Any]) -> None:
            self._tasks.pop(done, None)
            if _task_cancelled(done):
                return
            exception_reader = getattr(done, "exception", None)
            try:
                error = exception_reader() if callable(exception_reader) else None
            except asyncio.CancelledError:
                return
            if error is not None:
                # Consume the exception so asyncio does not emit a detached
                # task warning. A task that completed before quiesce no longer
                # owns a live DB session; only errors from tasks awaited during
                # this quiesce can fail closed.
                logger.warning(
                    "数据库后台任务已在 quiesce 前异常结束: source={}, error_type={}",
                    source,
                    type(error).__name__,
                )

        task.add_done_callback(_discard)
        return task

    def register_scheduler(self, scheduler: Any) -> None:
        """登记 lifespan 创建的 scheduler；重复登记是幂等的。"""

        if scheduler is None:
            return
        if all(existing is not scheduler for existing in self._schedulers):
            self._schedulers.append(scheduler)

    def begin_quiesce(self) -> bool:
        """立即关闭 admission；返回本次是否第一次进入 quiesce。"""

        if self.quiesced:
            return False
        first = not self.quiescing
        self.accepting = False
        self.quiescing = True
        return first

    async def stop_schedulers_and_tasks(self) -> None:
        """停止调度器，取消并 await 所有已登记的后台 task。"""

        self.begin_quiesce()

        scheduler_errors: list[BaseException] = []
        for scheduler in tuple(self._schedulers):
            stop = getattr(scheduler, "stop", None)
            if stop is None:
                continue
            try:
                result = stop()
                if inspect.isawaitable(result):
                    await _await_bounded(result, self.background_timeout)
            except BaseException as exc:
                scheduler_errors.append(exc)
                logger.exception(
                    "停止数据库后台调度器失败，阻止清库: scheduler_type={}",
                    type(scheduler).__name__,
                )

        # 任务在 scheduler.stop() 期间可能刚刚完成；只取消当前仍在运行的
        # task。reset request 本身从未通过 register_task 登记，因此不会被误杀。
        current = asyncio.current_task()
        tasks = [
            task
            for task in tuple(self._tasks)
            if task is not current and not _task_done(task)
        ]
        for task in tasks:
            task.cancel()

        task_errors: list[BaseException] = []
        if tasks:
            pending = asyncio.gather(*tasks, return_exceptions=True)
            try:
                results = await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=self.background_timeout,
                )
            except TimeoutError as exc:
                # Leave still-running tasks registered for a retry/diagnostic;
                # never claim quiesce succeeded after a cancellation timeout.
                raise DatabaseResetRuntimeQuiesceError(
                    "database background tasks did not stop before timeout "
                    f"({self.background_timeout:.2f}s); sources="
                    f"{self._task_sources(tasks)}"
                ) from exc
            for task, result in zip(tasks, results, strict=False):
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    task_errors.append(result)

        if scheduler_errors or task_errors:
            details = []
            if scheduler_errors:
                details.append(
                    "scheduler="
                    + ",".join(type(error).__name__ for error in scheduler_errors)
                )
            if task_errors:
                details.append(
                    "tasks=" + ",".join(type(error).__name__ for error in task_errors)
                )
            raise DatabaseResetRuntimeQuiesceError(
                "database background runtime failed to quiesce ("
                + "; ".join(details)
                + ")"
            ) from (scheduler_errors[0] if scheduler_errors else task_errors[0])

        # Completed callbacks are scheduled for the next event-loop turn. Keeping
        # only live tasks here makes diagnostics accurate even when a callback is
        # delayed by a test loop.
        for task in tasks:
            if _task_done(task):
                self._tasks.pop(task, None)

        self._schedulers.clear()

    def _task_sources(self, tasks: Iterable[asyncio.Task[Any]]) -> str:
        return ",".join(
            f"{self._tasks.get(task, 'unknown')}:{id(task)}" for task in tasks
        )

    async def quiesce(self) -> None:
        """幂等地完成 scheduler/task quiesce。

        失败时 gate 保持关闭且 ``quiesced`` 为 False；调用者可以诊断后重试，
        但在成功前绝不能执行数据库 DROP。
        """

        if self.quiesced:
            return

        async with self._quiesce_lock:
            if self.quiesced:
                return
            try:
                await self.stop_schedulers_and_tasks()
                await self.wait_for_requests()
            except asyncio.CancelledError as exc:
                self.accepting = False
                self.quiescing = True
                raise DatabaseResetRuntimeQuiesceError(
                    "database background runtime quiesce was cancelled"
                ) from exc


def install_runtime_supervisor(
    supervisor: DatabaseResetRuntimeSupervisor | None = None,
) -> DatabaseResetRuntimeSupervisor:
    """将 supervisor 绑定到当前 asyncio context，并返回它。

    不再使用 module-global active app：每个 FastAPI request/lifespan task 都
    通过 contextvars 获得自己的 app supervisor，``asyncio.create_task`` 会
    自动继承父 task 的绑定。
    """

    resolved = supervisor or DatabaseResetRuntimeSupervisor()
    _runtime_supervisor_context.set(resolved)
    return resolved


def bind_runtime_supervisor(
    supervisor: DatabaseResetRuntimeSupervisor,
) -> contextvars.Token[DatabaseResetRuntimeSupervisor | None]:
    """临时绑定 supervisor，调用方应在 finally 中 reset 返回的 token。"""

    return _runtime_supervisor_context.set(supervisor)


def reset_runtime_supervisor(
    token: contextvars.Token[DatabaseResetRuntimeSupervisor | None],
) -> None:
    """恢复 bind_runtime_supervisor 之前的 context。"""

    _runtime_supervisor_context.reset(token)


def get_runtime_supervisor(app: Any | None = None) -> DatabaseResetRuntimeSupervisor:
    """获取 app 绑定的 supervisor；无 app 时必须已绑定当前 context。"""

    if app is not None:
        state = getattr(app, "state", None)
        supervisor = getattr(state, "database_reset_runtime_supervisor", None)
        if supervisor is None:
            supervisor = DatabaseResetRuntimeSupervisor()
            if state is not None:
                state.database_reset_runtime_supervisor = supervisor
        return supervisor
    supervisor = _runtime_supervisor_context.get()
    if supervisor is None:
        raise DatabaseResetRuntimeBindingError(
            "database reset runtime supervisor is not bound; "
            "bind the app supervisor before creating database background work"
        )
    return supervisor


def ensure_background_admission(source: str) -> None:
    """Worker/scheduler 在创建 DB-backed task 前调用。"""

    get_runtime_supervisor().ensure_admission(source)


def register_background_task(
    task: Any, source: str, *, allow_closed: bool = False
) -> Any:
    """登记 worker 或 scheduler 创建的后台 task。"""

    return get_runtime_supervisor().register_task(
        task, source, allow_closed=allow_closed
    )


def create_registered_background_task(awaitable: Any, source: str) -> asyncio.Task[Any]:
    """Create and register a DB-backed background task without an admission gap."""

    try:
        ensure_background_admission(source)
    except DatabaseResetRuntimeAdmissionClosed, DatabaseResetRuntimeBindingError:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise
    task = asyncio.create_task(awaitable)
    try:
        register_background_task(task, source)
    except DatabaseResetRuntimeAdmissionClosed:
        # Admission cannot normally change between the synchronous check and
        # registration on one event loop, but retain a cleanup callback for
        # test doubles or unusual task factories.
        task.cancel()
        # Keep the cancelled task visible to quiesce so its completion can be
        # awaited even in a synthetic registration race.
        register_background_task(task, source, allow_closed=True)

        def _consume(done: asyncio.Task[Any]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except asyncio.CancelledError, Exception:
                return

        task.add_done_callback(_consume)
        raise
    return task


def register_current_background_task(source: str) -> asyncio.Task[Any] | None:
    """登记当前 APScheduler coroutine；无 current task 时返回 ``None``。"""

    task = asyncio.current_task()
    if task is None:
        return None
    try:
        return register_background_task(task, source)
    except DatabaseResetRuntimeAdmissionClosed:
        # Scheduler may have been queued just as reset closed admission. It must
        # return without touching the database rather than surface a noisy job
        # failure.
        logger.info("跳过已进入清库静默期的后台任务: source={}", source)
        return None


def _register_app_runtime_handles(
    app: Any, supervisor: DatabaseResetRuntimeSupervisor
) -> None:
    """把 lifespan 可能创建的 scheduler/task handle 补登记到 supervisor。"""

    state = getattr(app, "state", None)
    if state is None:
        return
    for name in (
        "scan_scheduler",
        "quota_reset_scheduler",
        "star_aid_scheduler",
        "update_checker",
    ):
        scheduler = getattr(state, name, None)
        if scheduler is not None:
            supervisor.register_scheduler(scheduler)
    for name in ("database_reset_runtime_schedulers",):
        for scheduler in getattr(state, name, ()) or ():
            supervisor.register_scheduler(scheduler)


async def _quiesce_sse() -> None:
    """关闭并等待全部 SSE；超时必须阻止数据库 DROP。"""

    from backend.webui.sse import sse_manager

    try:
        begin_quiesce = getattr(sse_manager, "begin_quiesce", None)
        closed_sse = (
            begin_quiesce() if callable(begin_quiesce) else sse_manager.close_all()
        )
        if not closed_sse:
            return
        remaining_sse = await sse_manager.wait_until_closed(
            timeout=_SSE_STOP_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise DatabaseResetRuntimeQuiesceError(
            "activity SSE cleanup failed; refusing database DROP"
        ) from exc
    if remaining_sse:
        raise DatabaseResetRuntimeQuiesceError(
            f"{remaining_sse} activity SSE streams did not close within "
            f"{_SSE_STOP_TIMEOUT_SECONDS:.2f}s; refusing database DROP"
        )


async def _quiesce_outbox(app: Any) -> None:
    """停止并 await Outbox dispatcher；active timeout/error fail-closed。"""

    state = app.state
    dispatcher = getattr(state, "activity_outbox_dispatcher", None)
    task = getattr(state, "activity_outbox_task", None)
    if task is None:
        if dispatcher is not None:
            try:
                result = dispatcher.stop()
                if inspect.isawaitable(result):
                    await _await_bounded(result, _OUTBOX_STOP_TIMEOUT_SECONDS)
            except Exception as exc:
                raise DatabaseResetRuntimeQuiesceError(
                    "activity Outbox dispatcher stop failed; refusing database DROP"
                ) from exc
            state.activity_outbox_dispatcher = None
        return

    already_done = task.done()
    try:
        if dispatcher is not None:
            try:
                result = dispatcher.stop()
                if inspect.isawaitable(result):
                    timeout = getattr(
                        state,
                        "activity_outbox_shutdown_timeout_seconds",
                        _OUTBOX_STOP_TIMEOUT_SECONDS,
                    )
                    await _await_bounded(result, float(timeout))
            except Exception as exc:
                if not already_done:
                    raise DatabaseResetRuntimeQuiesceError(
                        "activity Outbox dispatcher stop failed; refusing database DROP"
                    ) from exc
                logger.warning(
                    "已结束的 Outbox dispatcher stop 失败，继续清理句柄: error_type={}",
                    type(exc).__name__,
                )

        if not task.done():
            timeout = getattr(
                state,
                "activity_outbox_shutdown_timeout_seconds",
                _OUTBOX_STOP_TIMEOUT_SECONDS,
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError as exc:
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                except TimeoutError, asyncio.CancelledError:
                    pass
                raise DatabaseResetRuntimeQuiesceError(
                    "activity Outbox dispatcher did not stop before timeout "
                    f"({float(timeout):.2f}s); refusing database DROP"
                ) from exc

        try:
            await task
        except asyncio.CancelledError:
            # Dispatcher.stop() may intentionally cancel its run task. It has
            # been awaited, so no code can continue into the post-DROP window.
            if not task.cancelled() and not already_done:
                raise
        except Exception as exc:
            if not already_done:
                raise DatabaseResetRuntimeQuiesceError(
                    "activity Outbox dispatcher failed while quiescing; "
                    "refusing database DROP"
                ) from exc
            logger.warning(
                "已结束的 Outbox dispatcher 任务异常，继续清理句柄: error_type={}",
                type(exc).__name__,
            )
    finally:
        if task.done():
            state.activity_outbox_task = None
            state.activity_outbox_dispatcher = None


async def quiesce_database_reset_runtime(app: Any) -> None:
    """在 reset 或正常 lifespan shutdown 中完成完整 runtime quiesce。"""

    supervisor = get_runtime_supervisor(app)
    token = bind_runtime_supervisor(supervisor)
    try:
        _register_app_runtime_handles(app, supervisor)
        supervisor.begin_quiesce()

        # Scheduler/worker admission 与 in-flight task 必须先安全收敛；只有成功
        # 后才结束长连接和 outbox，确保 reset 失败时 gate 保持关闭且不会 DROP。
        await supervisor.quiesce()
        await _quiesce_sse()
        await _quiesce_outbox(app)
        supervisor.quiesced = True
        supervisor.quiescing = True
        supervisor.accepting = False
        logger.info("✅ 数据库重置/应用关闭前 runtime 已安全静默")
    finally:
        reset_runtime_supervisor(token)
