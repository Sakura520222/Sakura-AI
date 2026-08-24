"""Issue Worker cancellation and observability cleanup tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.workers import issue_worker as issue_worker_module
from backend.workers.issue_worker import IssueWorker


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
