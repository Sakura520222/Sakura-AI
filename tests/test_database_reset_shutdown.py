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
from backend.webui.routes import activity_observability as activity_routes
from backend.webui.routes.sse import sse_events
from backend.webui.sse import SSEManager, sse_manager


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
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
async def test_activity_sse_releases_request_database_session_before_unsubscribe(
    monkeypatch,
):
    service = SimpleNamespace(
        require_session_access=AsyncMock(),
        authorization_version=AsyncMock(return_value="v1"),
    )
    db = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        activity_routes,
        "_access_service",
        lambda _request, _db: service,
    )

    response = await activity_routes.activity_stream(
        42,
        _ConnectedRequest(),
        user={"user_id": 1},
        db=db,
    )
    next_chunk = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)

    sse_manager.close_all()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(next_chunk, timeout=1)
    db.close.assert_awaited_once_with()
    assert await sse_manager.wait_until_closed(timeout=0.1) == 0


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
