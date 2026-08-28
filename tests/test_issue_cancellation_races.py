"""Regression tests for Issue worker cancellation and persistence races."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeAdmissionClosed,
)
from backend.services.issue_service import IssueService
from backend.workers import issue_worker as issue_worker_module
from backend.workers.issue_worker import IssueWorker


class _Result:
    def __init__(self, record=None, *, rowcount=None):
        self._record = record
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._record


class _RecordingSession:
    def __init__(self, record=None, *, update_rowcount=1):
        self.record = record
        self.update_rowcount = update_rowcount
        self.selects = []
        self.updates = []
        self.commits = 0

    async def execute(self, statement):
        if getattr(statement, "is_update", False):
            self.updates.append(statement)
            return _Result(rowcount=self.update_rowcount)
        self.selects.append(statement)
        return _Result(self.record)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _record):
        return None


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


def _issue_info(task_id="issue-task", *, owner="owner", repo="repo"):
    return {
        "task_id": task_id,
        "repo_owner": owner,
        "repo_name": repo,
        "repo_full_name": f"{owner}/{repo}",
        "issue_number": 7,
        "title": "Issue",
        "body": "Body",
        "state": "open",
    }


@pytest.mark.asyncio
async def test_cancel_task_cancels_and_awaits_semaphore_wait(monkeypatch):
    """A task blocked on admission must finish before lifecycle cleanup runs."""
    execution = SimpleNamespace(merged=False, finish=AsyncMock())
    integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=2)),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.analyzer = SimpleNamespace(api_client=None)
    worker._background_tasks = set()
    blocked = asyncio.Semaphore(0)

    async def get_blocked_semaphore():
        return blocked

    monkeypatch.setattr(issue_worker_module, "_get_issue_semaphore", get_blocked_semaphore)
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    task = asyncio.create_task(worker.process_issue_analysis(_issue_info()))
    task_key = "owner/repo#7"
    for _ in range(10):
        await asyncio.sleep(0)
        if task_key in worker._task_handles:
            break

    assert not task.done()
    assert await worker.cancel_task(task_key) is True
    assert task.done()
    assert task.cancelled()
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
    assert worker._cancel_events == {}
    assert await worker.cancel_task(task_key) is False


@pytest.mark.asyncio
async def test_admission_rejection_unregisters_pre_registered_issue_task(monkeypatch):
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    worker._task_handles = {}
    worker._task_analysis_records = {}

    def reject_registration(_task, _source):
        raise DatabaseResetRuntimeAdmissionClosed("reset is closing")

    monkeypatch.setattr(issue_worker_module, "ensure_background_admission", lambda _: None)
    monkeypatch.setattr(issue_worker_module, "register_background_task", reject_registration)
    monkeypatch.setattr(issue_worker_module, "get_issue_worker", lambda: worker)
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        await issue_worker_module.submit_issue_analysis_task(_issue_info())

    assert worker._cancel_events == {}
    assert worker._task_handles == {}
    assert worker._task_analysis_records == {}


@pytest.mark.asyncio
async def test_save_result_uses_exact_analysis_id_for_concurrent_same_issue_tasks():
    service = IssueService()
    records = [
        SimpleNamespace(id=101, status="analyzing", issue_state="open"),
        SimpleNamespace(id=202, status="analyzing", issue_state="open"),
    ]
    sessions = [_RecordingSession(record) for record in records]

    for record, session in zip(records, sessions):
        result = await service.save_analysis_result(
            {"summary": f"result-{record.id}"},
            _issue_info(),
            session,
            analysis_id=record.id,
        )
        assert result is record
        assert len(session.updates) == 1
        compiled = session.updates[0].compile()
        assert compiled.params["id_1"] == record.id

    assert [session.updates[0].compile().params["id_1"] for session in sessions] == [
        101,
        202,
    ]


@pytest.mark.asyncio
async def test_short_repo_compatibility_query_is_owner_scoped():
    record = SimpleNamespace(id=303, status="analyzing", issue_state="open")
    session = _RecordingSession(record)

    await IssueService().save_analysis_result(
        {"summary": "owner-a result"},
        _issue_info(owner="owner-a"),
        session,
    )

    assert len(session.selects) == 1
    compiled = session.selects[0].compile()
    assert "repo_owner" in str(session.selects[0])
    assert "owner-a" in compiled.params.values()


@pytest.mark.asyncio
async def test_save_rowcount_zero_converges_worker_to_cancelled(monkeypatch):
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=2)),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.github_app = SimpleNamespace(get_repo_client=lambda *_args: None)
    worker.analyzer = SimpleNamespace(
        api_client=None,
        analyze_issue=AsyncMock(return_value={"summary": "stale"}),
    )
    worker._background_tasks = set()

    record_holder = {}

    class WorkerSession(_RecordingSession):
        async def execute(self, statement):
            if getattr(statement, "is_update", False):
                self.updates.append(statement)
                return _Result(rowcount=1)
            if "issue_analyses.id" in str(statement):
                return _Result(record_holder.get("record"))
            return _Result(None)

        def add(self, record):
            record.id = 404
            record_holder["record"] = record

        async def scalar(self, _statement):
            return 0

    session = WorkerSession()
    monkeypatch.setattr(issue_worker_module, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        lambda: _ready_semaphore(),
    )
    save_result = AsyncMock(return_value=None)
    monkeypatch.setattr(issue_worker_module.issue_service, "save_analysis_result", save_result)

    async def _ready_semaphore():
        return asyncio.Semaphore(1)

    task_id = await worker.process_issue_analysis(_issue_info())

    record = record_holder["record"]
    assert task_id == "issue-task"
    assert record.status == issue_worker_module.IssueAnalysisStatus.CANCELLED.value
    assert save_result.await_args.kwargs["analysis_id"] == 404
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
    assert worker._cancel_events == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_terminal_analysis_does_not_emit_cancelled_or_change_execution(
    monkeypatch, terminal_status
):
    """A stale cancellation must preserve an already terminal analysis row."""
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=2)),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.github_app = SimpleNamespace(get_repo_client=lambda *_args: None)
    worker.analyzer = SimpleNamespace(
        api_client=None,
        analyze_issue=AsyncMock(side_effect=ReviewCancelledError()),
    )
    worker._background_tasks = set()

    record_holder = {}

    class TerminalSession(_RecordingSession):
        def add(self, record):
            record.id = 505
            record_holder["record"] = record

        async def execute(self, statement):
            if getattr(statement, "is_update", False):
                self.updates.append(statement)
                return _Result(rowcount=1)
            self.selects.append(statement)
            return _Result(record_holder.get("record"))

        async def scalar(self, _statement):
            return 0

        async def commit(self):
            self.commits += 1
            # Simulate a sibling completing/failing this exact row after the
            # worker's ANALYZING transition but before cancellation cleanup.
            if self.commits >= 2:
                record_holder["record"].status = terminal_status

    session = TerminalSession()
    monkeypatch.setattr(issue_worker_module, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        lambda: _ready_semaphore(),
    )
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    async def _ready_semaphore():
        return asyncio.Semaphore(1)

    publish_event = AsyncMock()
    monkeypatch.setattr("backend.webui.sse.publish_event", publish_event)

    task_id = await worker.process_issue_analysis(_issue_info())

    assert task_id == "issue-task"
    assert record_holder["record"].status == terminal_status
    execution.finish.assert_awaited_once_with(terminal_status, error_message=None)
    assert all(
        call.args[1].get("status") != "cancelled"
        for call in publish_event.await_args_list
    )
    assert session.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_task_cancel_refreshes_terminal_execution_status(
    monkeypatch, terminal_status
):
    """Forced cancellation refreshes the exact row before finishing execution."""
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=2)),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.github_app = SimpleNamespace(get_repo_client=lambda *_args: None)
    analysis_started = asyncio.Event()
    never = asyncio.Event()

    async def wait_for_cancellation(**_kwargs):
        analysis_started.set()
        await never.wait()

    worker.analyzer = SimpleNamespace(
        api_client=None,
        analyze_issue=wait_for_cancellation,
    )
    worker._background_tasks = set()

    record_holder = {}

    class TerminalSession(_RecordingSession):
        def add(self, record):
            record.id = 808
            record_holder["record"] = record

        async def execute(self, statement):
            if getattr(statement, "is_update", False):
                self.updates.append(statement)
                return _Result(rowcount=1)
            self.selects.append(statement)
            return _Result(record_holder.get("record"))

        async def scalar(self, _statement):
            return 0

        async def commit(self):
            self.commits += 1
            # Simulate an independent transaction winning after ANALYZING was
            # committed but before the worker receives task.cancel().
            if self.commits >= 2:
                record_holder["record"].status = terminal_status

    sessions = []

    def session_factory():
        session = TerminalSession()
        sessions.append(session)
        return _SessionContext(session)

    monkeypatch.setattr(issue_worker_module, "async_session", session_factory)
    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        lambda: _ready_semaphore(),
    )
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )
    publish_event = AsyncMock()
    monkeypatch.setattr("backend.webui.sse.publish_event", publish_event)

    async def _ready_semaphore():
        return asyncio.Semaphore(1)

    task = asyncio.create_task(worker.process_issue_analysis(_issue_info()))
    await asyncio.wait_for(analysis_started.wait(), timeout=1)

    assert await worker.cancel_task("owner/repo#7") is True
    assert task.done()
    assert task.cancelled()
    assert record_holder["record"].status == terminal_status
    execution.finish.assert_awaited_once_with(terminal_status, error_message=None)
    assert all(
        call.args[1].get("status") != "cancelled"
        for call in publish_event.await_args_list
    )
    assert all(not session.updates for session in sessions)
    assert worker._cancel_events == {}


@pytest.mark.asyncio
async def test_active_and_already_cancelled_analysis_emit_cancelled_once(monkeypatch):
    """Active and close-pre-cancelled rows both converge without duplicate SSE."""
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancelled_analysis_notifications = set()
    publish_event = AsyncMock()
    monkeypatch.setattr("backend.webui.sse.publish_event", publish_event)
    activity = AsyncMock()
    worker._log_activity = activity

    active_record = SimpleNamespace(id=606, status="analyzing", issue_state="open")
    active_session = _RecordingSession(active_record, update_rowcount=1)
    first = await worker._converge_cancelled_analysis(
        active_session,
        analysis_id=606,
        issue_info=_issue_info(),
        reason="cancelled",
    )
    second = await worker._converge_cancelled_analysis(
        active_session,
        analysis_id=606,
        issue_info=_issue_info(),
        reason="cancelled again",
    )

    assert first == (active_record, "cancelled")
    assert second == (active_record, "cancelled")
    assert active_record.status == "cancelled"
    assert publish_event.await_count == 1
    activity.assert_awaited_once_with(606, "cancelled", {"message": "cancelled"})

    publish_event.reset_mock()
    activity.reset_mock()
    already_cancelled = SimpleNamespace(id=707, status="cancelled", issue_state="closed")
    cancelled_session = _RecordingSession(already_cancelled)
    await worker._converge_cancelled_analysis(
        cancelled_session,
        analysis_id=707,
        issue_info=_issue_info(),
        reason="close webhook already cancelled",
    )
    await worker._converge_cancelled_analysis(
        cancelled_session,
        analysis_id=707,
        issue_info=_issue_info(),
        reason="duplicate close webhook",
    )

    assert publish_event.await_count == 1
    activity.assert_not_awaited()
