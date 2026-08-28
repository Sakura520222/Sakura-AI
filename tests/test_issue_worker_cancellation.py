"""Issue Worker cancellation and observability cleanup tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.workers import issue_worker as issue_worker_module
from backend.workers.issue_worker import IssueWorker


@pytest.mark.asyncio
async def test_issue_cancel_registry_is_per_task_and_repeated_cancel_is_safe():
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    task_key = "owner/repo#7"

    first = worker._register_task(task_key, "task-1")
    second = worker._register_task(task_key, "task-2")

    assert worker._cancel_events == {
        task_key: {"task-1": first, "task-2": second}
    }
    assert await worker.cancel_task(task_key) is True
    assert first.is_set() and second.is_set()
    assert await worker.cancel_task(task_key) is False

    worker._unregister_task(task_key, "task-1")
    assert worker._cancel_events[task_key] == {"task-2": second}
    worker._unregister_task(task_key, "task-2")
    assert task_key not in worker._cancel_events

    fresh = worker._register_task(task_key, "task-3")
    assert fresh is not first
    assert not fresh.is_set()


def test_issue_task_key_supports_full_name_or_owner_and_repo():
    assert IssueWorker._make_task_key(
        {"repo_full_name": "owner/repo", "issue_number": 7}
    ) == "owner/repo#7"
    assert IssueWorker._make_task_key(
        {"repo_owner": "owner", "repo_name": "repo", "issue_number": 7}
    ) == "owner/repo#7"


@pytest.mark.asyncio
async def test_issue_submission_registers_before_coroutine_runs_and_cleans_own_event(
    monkeypatch,
):
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    worker._background_tasks = set()
    release = asyncio.Event()
    started = asyncio.Event()
    observed = {}

    async def _run_issue_analysis(
        issue_info,
        *,
        deadline,
        cancel_event,
        task_id,
    ):
        observed["event"] = cancel_event
        started.set()
        await release.wait()
        return task_id

    worker._run_issue_analysis = _run_issue_analysis
    monkeypatch.setattr(issue_worker_module, "get_issue_worker", lambda: worker)
    monkeypatch.setattr(issue_worker_module, "ensure_background_admission", lambda _: None)
    monkeypatch.setattr(issue_worker_module, "register_background_task", lambda *_: None)
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    issue_info = {
        "repo_owner": "owner",
        "repo_name": "repo",
        "issue_number": 7,
    }
    task_id = await issue_worker_module.submit_issue_analysis_task(issue_info)
    task_key = "owner/repo#7"
    registered = worker._cancel_events[task_key][task_id]
    assert not started.is_set()

    assert await worker.cancel_task(task_key) is True
    assert registered.is_set()
    await asyncio.sleep(0)

    # The task handle is cancelled before its coroutine gets a chance to run;
    # this is required for lifecycle webhooks to avoid post-delete work.
    assert not started.is_set()
    assert task_key not in worker._cancel_events


@pytest.mark.asyncio
async def test_review_cancelled_error_marks_active_issue_cancelled_and_skips_save(
    monkeypatch,
):
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(
            return_value=SimpleNamespace(session_id=1, trigger_id=2)
        ),
        start_execution=AsyncMock(return_value=execution),
    )
    record = SimpleNamespace(
        id=11,
        status=issue_worker_module.IssueAnalysisStatus.ANALYZING.value,
        error_message=None,
    )

    class _Result:
        def scalar_one_or_none(self):
            return record

    class _Db:
        def add(self, value):
            value.id = record.id

        async def scalar(self, _query):
            return 0

        async def execute(self, _query):
            return _Result()

        async def commit(self):
            return None

        async def refresh(self, _value):
            return None

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    db = _Db()
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.github_app = SimpleNamespace(get_repo_client=lambda *_: None)
    worker.analyzer = SimpleNamespace(
        api_client=None,
        analyze_issue=AsyncMock(side_effect=ReviewCancelledError()),
    )
    worker._background_tasks = set()

    monkeypatch.setattr(issue_worker_module, "async_session", lambda: _Session())
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )
    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        lambda: _ready_semaphore(),
    )
    save_result = AsyncMock()
    monkeypatch.setattr(issue_worker_module.issue_service, "save_analysis_result", save_result)

    async def _ready_semaphore():
        return asyncio.Semaphore(1)

    task_id = await worker.process_issue_analysis(
        {
            "task_id": "issue-cancelled",
            "repo_owner": "owner",
            "repo_name": "repo",
            "repo_full_name": "owner/repo",
            "issue_number": 7,
        }
    )

    assert task_id == "issue-cancelled"
    assert record.status == issue_worker_module.IssueAnalysisStatus.CANCELLED.value
    assert isinstance(record.error_message, str)
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
    save_result.assert_not_awaited()
    assert worker._cancel_events == {}


@pytest.mark.asyncio
async def test_issue_cancellation_while_waiting_for_semaphore_finishes_execution(
    monkeypatch,
):
    """取消排队中的 Issue Worker 也必须终结已启动的 observability execution。"""
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(
            return_value=SimpleNamespace(session_id=1, trigger_id=2)
        ),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.analyzer = SimpleNamespace(api_client=None)

    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    blocked_semaphore = asyncio.Semaphore(0)

    async def _get_blocked_semaphore():
        return blocked_semaphore

    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        _get_blocked_semaphore,
    )

    task = asyncio.create_task(
        worker.process_issue_analysis(
            {
                "task_id": "issue-cancelled",
                "repo_owner": "owner",
                "repo_name": "repo",
                "repo_full_name": "owner/repo",
                "issue_number": 7,
            }
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    integration.start_execution.assert_awaited_once()
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
