"""数据库全量重置的运行时停机回归测试。"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.core.setup_service as setup_service_module
from backend.core.setup_service import SetupService
from backend.services.database_reset_runtime_service import (
    quiesce_database_reset_runtime,
)
from backend.webui import deps as webui_deps
from backend.webui.routes import activity_observability as activity_routes
from backend.webui.routes.sse import sse_events
from backend.webui.sse import SSEManager, sse_manager

import backend.main as main


async def _run_isolated_lifespan(app, monkeypatch):
    monkeypatch.setattr(main, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(main, "stop_telegram_bot", AsyncMock())
    monkeypatch.setattr(
        "backend.services.embedding_service.close_embedding_service", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.services.embedding_service.close_reranker_service", AsyncMock()
    )

    async with main.lifespan(app):
        pass


@pytest.mark.asyncio
async def test_lifespan_outbox_shutdown_waits_for_natural_dispatcher_exit(
    monkeypatch,
):
    release_task = asyncio.Event()

    async def run_outbox():
        await release_task.wait()

    dispatcher = MagicMock()
    dispatcher.stop.side_effect = release_task.set
    task = asyncio.create_task(run_outbox())
    await asyncio.sleep(0)
    app = SimpleNamespace(
        state=SimpleNamespace(
            activity_outbox_dispatcher=dispatcher,
            activity_outbox_task=task,
        )
    )

    await _run_isolated_lifespan(app, monkeypatch)

    dispatcher.stop.assert_called_once_with()
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_lifespan_outbox_shutdown_cancels_and_awaits_unresponsive_dispatcher(
    monkeypatch,
):
    shutdown_timeout = 0.02
    cancellation_received = asyncio.Event()
    task_finished = asyncio.Event()
    task_started = asyncio.Event()

    async def run_outbox():
        task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            task_finished.set()
            raise

    dispatcher = MagicMock()
    task = asyncio.create_task(run_outbox())
    await task_started.wait()
    app = SimpleNamespace(
        state=SimpleNamespace(
            activity_outbox_dispatcher=dispatcher,
            activity_outbox_task=task,
        )
    )
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(activity_outbox_shutdown_timeout_seconds=shutdown_timeout),
    )

    started_at = asyncio.get_running_loop().time()
    await _run_isolated_lifespan(app, monkeypatch)
    elapsed = asyncio.get_running_loop().time() - started_at

    dispatcher.stop.assert_called_once_with()
    assert elapsed >= shutdown_timeout / 2
    assert cancellation_received.is_set()
    assert task_finished.is_set()
    assert task.done()
    assert task.cancelled()


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _DisconnectingRequest:
    def __init__(self):
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks == 1


class _ActivitySessionContext:
    def __init__(self, contexts):
        self.entered = False
        self.exited = False
        self.exit_exception = None
        contexts.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, _value, _traceback):
        self.exited = True
        self.exit_exception = exc_type
        return False


@pytest.mark.asyncio
async def test_quiesce_waits_for_outbox_and_clears_lifespan_handles():
    stop_event = asyncio.Event()

    async def run_outbox() -> None:
        await stop_event.wait()

    dispatcher = MagicMock()
    dispatcher.stop.side_effect = stop_event.set
    task = asyncio.create_task(run_outbox())
    await asyncio.sleep(0)
    app = SimpleNamespace(
        state=SimpleNamespace(
            activity_outbox_dispatcher=dispatcher,
            activity_outbox_task=task,
        )
    )

    await quiesce_database_reset_runtime(app)

    dispatcher.stop.assert_called_once_with()
    assert task.done()
    assert not task.cancelled()
    assert app.state.activity_outbox_task is None
    assert app.state.activity_outbox_dispatcher is None


@pytest.mark.asyncio
async def test_quiesce_consumes_an_already_failed_outbox_task():
    async def fail_outbox() -> None:
        raise RuntimeError("outbox failed")

    task = asyncio.create_task(fail_outbox())
    await asyncio.sleep(0)
    app = SimpleNamespace(
        state=SimpleNamespace(
            activity_outbox_dispatcher=MagicMock(),
            activity_outbox_task=task,
        )
    )

    await quiesce_database_reset_runtime(app)

    assert app.state.activity_outbox_task is None
    assert app.state.activity_outbox_dispatcher is None


@pytest.mark.asyncio
async def test_sse_close_all_replaces_full_queue_and_unblocks_receiver():
    manager = SSEManager(queue_size=3)
    queue = manager.subscribe("test")
    for index in range(3):
        queue.put_nowait({"type": "stale", "data": {"index": index}})

    assert manager.close_all() == 1
    assert await manager.receive(queue, timeout=0.1) is None
    assert queue.empty()

    manager.unsubscribe("test", queue)


@pytest.mark.asyncio
async def test_active_sse_stream_exits_when_restart_closes_connections():
    response = await sse_events(_ConnectedRequest(), user={"user_id": 1})
    next_chunk = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)

    sse_manager.close_all()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(next_chunk, timeout=1)
    assert await sse_manager.wait_until_closed(timeout=0.1) == 0


@pytest.mark.asyncio
async def test_get_db_completes_session_close_before_propagating_cancellation(
    monkeypatch,
):
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()

    class Session:
        async def close(self):
            close_started.set()
            await release_close.wait()
            close_finished.set()

    session = Session()

    class SessionFactory:
        def __call__(self):
            return session

    monkeypatch.setattr(webui_deps.db_module, "async_session", SessionFactory())

    dependency = webui_deps.get_db()
    assert await anext(dependency) is session

    cleanup_task = asyncio.create_task(dependency.athrow(asyncio.CancelledError()))
    await close_started.wait()

    cleanup_task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_task.done()
    assert not close_finished.is_set()

    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup_task
    assert close_finished.is_set()


@pytest.mark.asyncio
async def test_get_db_completes_session_close_after_repeated_cancellation(
    monkeypatch,
):
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()
    close_task_holder = []

    class Session:
        async def close(self):
            close_task_holder.append(asyncio.current_task())
            close_started.set()
            await release_close.wait()
            close_finished.set()

    session = Session()

    class SessionFactory:
        def __call__(self):
            return session

    monkeypatch.setattr(webui_deps.db_module, "async_session", SessionFactory())

    dependency = webui_deps.get_db()
    assert await anext(dependency) is session

    cleanup_task = asyncio.create_task(dependency.athrow(asyncio.CancelledError()))
    await close_started.wait()

    try:
        for _ in range(2):
            cleanup_task.cancel()
            await asyncio.sleep(0)
            assert not cleanup_task.done()
            assert not close_finished.is_set()
    finally:
        release_close.set()
        if not cleanup_task.done():
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        await asyncio.wait_for(close_finished.wait(), timeout=1)

    with pytest.raises(asyncio.CancelledError):
        await cleanup_task
    assert close_finished.is_set()
    assert close_task_holder
    assert close_task_holder[0].done()


@pytest.mark.asyncio
async def test_activity_sse_closes_each_short_lived_session_before_stream_cleanup(
    monkeypatch,
):
    contexts = []

    class Session:
        async def close(self):
            raise AssertionError("SSE generator must not close a request session")

    class SessionContext:
        def __init__(self):
            self.session = Session()
            self.exited = False

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, _exc_type, _exc, _tb):
            self.exited = True
            return False

    def factory():
        context = SessionContext()
        contexts.append(context)
        return context

    monkeypatch.setattr(activity_routes.db_module, "async_session", factory)
    service = SimpleNamespace(
        require_session_access=AsyncMock(),
        authorization_version=AsyncMock(return_value="v1"),
    )
    monkeypatch.setattr(
        activity_routes,
        "_access_service",
        lambda _request, _db=None: service,
    )

    response = await activity_routes.activity_stream(
        42,
        _ConnectedRequest(),
        user={"user_id": 1},
    )
    assert contexts
    assert all(context.exited for context in contexts)

    next_chunk = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)

    sse_manager.close_all()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(next_chunk, timeout=1)
    assert await sse_manager.wait_until_closed(timeout=0.1) == 0


@pytest.mark.asyncio
async def test_activity_sse_heartbeat_closes_session_when_authorization_changes(
    monkeypatch,
):
    manager = SSEManager()
    monkeypatch.setattr(activity_routes, "sse_manager", manager)
    contexts = []

    monkeypatch.setattr(
        activity_routes.db_module,
        "async_session",
        lambda: _ActivitySessionContext(contexts),
    )
    service = SimpleNamespace(
        require_session_access=AsyncMock(),
        authorization_version=AsyncMock(side_effect=["v1", "v2"]),
    )
    monkeypatch.setattr(
        activity_routes,
        "_access_service",
        lambda _request, _db=None: service,
    )
    manager.receive = AsyncMock(side_effect=asyncio.TimeoutError)
    response = None
    try:
        response = await activity_routes.activity_stream(
            42,
            _ConnectedRequest(),
            user={"user_id": 1},
        )

        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

        assert service.authorization_version.await_count == 2
        assert len(contexts) == 2
        heartbeat_context = contexts[1]
        assert heartbeat_context.entered
        assert heartbeat_context.exited
        assert manager.subscriber_count == 0
    finally:
        if response is not None:
            await response.body_iterator.aclose()
        manager.close_all()
        assert await manager.wait_until_closed(timeout=0.1) == 0


@pytest.mark.asyncio
async def test_activity_sse_exits_when_client_disconnects(monkeypatch):
    manager = SSEManager()
    monkeypatch.setattr(activity_routes, "sse_manager", manager)
    contexts = []
    monkeypatch.setattr(
        activity_routes.db_module,
        "async_session",
        lambda: _ActivitySessionContext(contexts),
    )
    service = SimpleNamespace(
        require_session_access=AsyncMock(),
        authorization_version=AsyncMock(return_value="v1"),
    )
    monkeypatch.setattr(
        activity_routes,
        "_access_service",
        lambda _request, _db=None: service,
    )
    request = _DisconnectingRequest()
    response = None
    try:
        response = await activity_routes.activity_stream(
            42,
            request,
            user={"user_id": 1},
        )

        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

        assert request.checks == 1
        assert manager.subscriber_count == 0
    finally:
        if response is not None:
            await response.body_iterator.aclose()
        manager.close_all()
        assert await manager.wait_until_closed(timeout=0.1) == 0


@pytest.mark.asyncio
async def test_activity_sse_cancellation_propagates_and_unsubscribes(monkeypatch):
    manager = SSEManager()
    monkeypatch.setattr(activity_routes, "sse_manager", manager)
    contexts = []
    monkeypatch.setattr(
        activity_routes.db_module,
        "async_session",
        lambda: _ActivitySessionContext(contexts),
    )
    service = SimpleNamespace(
        require_session_access=AsyncMock(),
        authorization_version=AsyncMock(return_value="v1"),
    )
    monkeypatch.setattr(
        activity_routes,
        "_access_service",
        lambda _request, _db=None: service,
    )
    receive_started = asyncio.Event()
    receive = manager.receive

    async def blocking_receive(queue, *, timeout):
        receive_started.set()
        return await receive(queue, timeout=timeout)

    manager.receive = blocking_receive
    response = None
    pending = None
    try:
        response = await activity_routes.activity_stream(
            42,
            _ConnectedRequest(),
            user={"user_id": 1},
        )
        pending = asyncio.create_task(anext(response.body_iterator))
        await asyncio.wait_for(receive_started.wait(), timeout=1)
        assert not pending.done()

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert manager.subscriber_count == 0
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        if response is not None:
            await response.body_iterator.aclose()
        manager.close_all()
        assert await manager.wait_until_closed(timeout=0.1) == 0


def test_trigger_restart_closes_sse_before_sigterm(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(
        sse_manager,
        "close_all",
        lambda: events.append("close_sse") or 2,
    )
    monkeypatch.setattr(setup_service_module.os, "getpid", lambda: 1234)
    monkeypatch.setattr(
        setup_service_module.os,
        "kill",
        lambda pid, sig: events.append(("kill", pid, sig)),
    )

    SetupService().trigger_restart()

    assert events == ["close_sse", ("kill", 1234, signal.SIGTERM)]


def test_docker_uvicorn_has_bounded_graceful_shutdown():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert '"--timeout-graceful-shutdown", "15"' in dockerfile
