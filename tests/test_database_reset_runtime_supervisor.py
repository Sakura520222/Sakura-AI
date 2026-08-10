"""数据库重置 runtime supervisor 回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeAdmissionClosed,
    DatabaseResetRuntimeQuiesceError,
    DatabaseResetRuntimeSupervisor,
    bind_runtime_supervisor,
    ensure_background_admission,
    get_runtime_supervisor,
    quiesce_database_reset_runtime,
    reset_runtime_supervisor,
)
from backend.webui.sse import SSEManager


@pytest.fixture
def fresh_supervisor():
    supervisor = DatabaseResetRuntimeSupervisor(background_timeout=0.05)
    token = bind_runtime_supervisor(supervisor)
    try:
        yield supervisor
    finally:
        reset_runtime_supervisor(token)


def _app(supervisor):
    return SimpleNamespace(
        state=SimpleNamespace(database_reset_runtime_supervisor=supervisor)
    )


@pytest.mark.asyncio
async def test_quiesce_closes_admission_and_awaits_review_and_issue_tasks(
    fresh_supervisor,
):
    release = asyncio.Event()
    finished: list[str] = []

    async def running(name: str):
        try:
            await release.wait()
        finally:
            finished.append(name)

    review = asyncio.create_task(running("review"))
    issue = asyncio.create_task(running("issue"))
    fresh_supervisor.register_task(review, "review")
    fresh_supervisor.register_task(issue, "issue")
    await asyncio.sleep(0)

    await quiesce_database_reset_runtime(_app(fresh_supervisor))

    assert review.done() and issue.done()
    assert sorted(finished) == ["issue", "review"]
    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        ensure_background_admission("review")


@pytest.mark.asyncio
async def test_scheduler_is_stopped_with_wait_true_and_repeated_quiesce_is_idempotent(
    fresh_supervisor,
):
    scheduler = MagicMock()
    fresh_supervisor.register_scheduler(scheduler)
    app = _app(fresh_supervisor)

    await quiesce_database_reset_runtime(app)
    await quiesce_database_reset_runtime(app)

    scheduler.stop.assert_called_once_with()
    assert fresh_supervisor.quiesced


@pytest.mark.asyncio
async def test_quiesce_fails_closed_when_registered_task_errors(fresh_supervisor):
    async def fail_on_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("worker failed while stopping") from exc

    task = asyncio.create_task(fail_on_cancel())
    fresh_supervisor.register_task(task, "issue")
    await asyncio.sleep(0)

    with pytest.raises(DatabaseResetRuntimeQuiesceError):
        await quiesce_database_reset_runtime(_app(fresh_supervisor))

    assert not fresh_supervisor.accepting
    assert not fresh_supervisor.quiesced


@pytest.mark.asyncio
async def test_completed_worker_error_is_consumed_without_locking_future_reset(
    fresh_supervisor,
):
    async def fail():
        raise RuntimeError("already failed")

    task = asyncio.create_task(fail())
    fresh_supervisor.register_task(task, "review")
    await asyncio.sleep(0)
    await quiesce_database_reset_runtime(_app(fresh_supervisor))
    assert fresh_supervisor.quiesced


@pytest.mark.asyncio
async def test_unresponsive_worker_timeout_returns_fail_closed_promptly(fresh_supervisor):
    stop = asyncio.Event()

    async def unresponsive():
        while not stop.is_set():
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                # Broken worker simulation: cancellation does not complete the
                # task until an external supervisor fixes the underlying issue.
                continue

    task = asyncio.create_task(unresponsive())
    fresh_supervisor.register_task(task, "review")
    await asyncio.sleep(0)

    with pytest.raises(DatabaseResetRuntimeQuiesceError):
        await quiesce_database_reset_runtime(_app(fresh_supervisor))

    stop.set()
    task.cancel()
    await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_gate_closes_before_a_scheduler_startup_race(fresh_supervisor):
    fresh_supervisor.begin_quiesce()
    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        ensure_background_admission("quota_reset_scheduler")


@pytest.mark.asyncio
async def test_sse_late_subscribe_is_closed_after_quiesce_begins():
    manager = SSEManager()
    queue = manager.subscribe("activity")
    assert manager.subscriber_count == 1

    manager.begin_quiesce()
    late_queue = manager.subscribe("activity")

    assert manager.subscriber_count == 1
    assert await manager.receive(late_queue, timeout=0.1) is None
    manager.unsubscribe("activity", queue)
    assert manager.subscriber_count == 0


@pytest.mark.asyncio
async def test_two_app_contexts_keep_background_admission_isolated():
    supervisor_a = DatabaseResetRuntimeSupervisor()
    supervisor_b = DatabaseResetRuntimeSupervisor()
    app_a = _app(supervisor_a)
    app_b = _app(supervisor_b)
    ready = asyncio.Barrier(2)

    async def observe(app, expected):
        token = bind_runtime_supervisor(get_runtime_supervisor(app))
        try:
            await ready.wait()
            assert get_runtime_supervisor() is expected
            if expected is supervisor_b:
                with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
                    ensure_background_admission("multi_app_probe")
            else:
                ensure_background_admission("multi_app_probe")
        finally:
            reset_runtime_supervisor(token)

    supervisor_b.begin_quiesce()
    await asyncio.gather(observe(app_a, supervisor_a), observe(app_b, supervisor_b))


@pytest.mark.asyncio
async def test_main_request_middleware_binds_the_app_supervisor():
    from backend import main

    supervisor = DatabaseResetRuntimeSupervisor()
    app = _app(supervisor)
    request = SimpleNamespace(app=app)
    observed = []

    async def call_next(_request):
        observed.append(get_runtime_supervisor())
        child = asyncio.create_task(asyncio.sleep(0))
        await child
        observed.append(get_runtime_supervisor())
        return "ok"

    token = bind_runtime_supervisor(DatabaseResetRuntimeSupervisor())
    try:
        result = await main.bind_database_reset_runtime(request, call_next)
    finally:
        reset_runtime_supervisor(token)

    assert result == "ok"
    assert observed == [supervisor, supervisor]


@pytest.mark.asyncio
async def test_review_and_issue_submission_are_rejected_after_gate_closes(
    fresh_supervisor,
    monkeypatch,
):
    import backend.workers.issue_worker as issue_module
    import backend.workers.review_worker as review_module

    fresh_supervisor.begin_quiesce()
    monkeypatch.setattr(review_module, "get_worker", MagicMock())
    monkeypatch.setattr(issue_module, "get_issue_worker", MagicMock())

    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        await review_module.submit_review_task(
            {"repo_full_name": "owner/repo", "pr_number": 1}
        )
    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        await issue_module.submit_issue_analysis_task({"repo_full_name": "owner/repo"})

    review_module.get_worker.assert_not_called()
    issue_module.get_issue_worker.assert_not_called()


@pytest.mark.asyncio
async def test_outbox_timeout_is_fail_closed_and_handle_is_retained(fresh_supervisor):
    task_started = asyncio.Event()
    stop = asyncio.Event()

    async def unresponsive():
        task_started.set()
        while True:
            try:
                if stop.is_set():
                    return
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                # Simulate a broken dispatcher that ignores cancellation.
                if stop.is_set():
                    return

    task = asyncio.create_task(unresponsive())
    await task_started.wait()
    app = _app(fresh_supervisor)
    app.state.activity_outbox_task = task
    app.state.activity_outbox_dispatcher = MagicMock()
    app.state.activity_outbox_shutdown_timeout_seconds = 0.01

    with pytest.raises(DatabaseResetRuntimeQuiesceError):
        await quiesce_database_reset_runtime(app)

    assert not fresh_supervisor.quiesced
    assert not fresh_supervisor.accepting
    stop.set()
    task.cancel()
    # The fixture intentionally models an unresponsive task; clean it up after
    # proving fail-closed behavior.
    await asyncio.wait_for(task, timeout=0.1)
