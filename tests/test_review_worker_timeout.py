"""Review worker dynamic timeout coverage."""

import asyncio

import pytest

from backend.core.config import get_settings
from backend.workers import review_worker
from backend.workers.review_worker import (
    ReviewWorker,
    _run_review_task_with_timeout,
    submit_review_task,
)


@pytest.fixture
def stub_review_worker_dependencies(monkeypatch):
    monkeypatch.setattr(review_worker, "GitHubAppClient", lambda: object())
    monkeypatch.setattr(review_worker, "PRAnalyzer", lambda: object())
    monkeypatch.setattr(review_worker, "AIReviewer", lambda: object())
    monkeypatch.setattr(review_worker, "CommentService", lambda: object())
    monkeypatch.setattr(review_worker, "_worker_instance", None)
    yield
    monkeypatch.setattr(review_worker, "_worker_instance", None)


class _TimeoutWorker:
    def __init__(self):
        self.cancelled_key = None
        self.saved_errors = []

    async def process_review_task(self, pr_info):
        await asyncio.sleep(10)
        return "done"

    def cancel_task(self, task_key):
        self.cancelled_key = task_key
        return True

    async def _save_error_record(self, pr_info, error, task_id):
        self.saved_errors.append((pr_info, error, task_id))


@pytest.mark.asyncio
async def test_review_task_timeout_uses_dynamic_setting():
    settings = get_settings()
    old_value = settings.review_timeout_seconds
    try:
        settings.review_timeout_seconds = 0.01
        pr_info = {"repo_full_name": "owner/repo", "pr_number": 1}
        task_key = ReviewWorker._make_task_key(pr_info)
        worker = _TimeoutWorker()

        with pytest.raises(RuntimeError, match="审查任务超时"):
            await _run_review_task_with_timeout(worker, pr_info, task_key)

        assert worker.cancelled_key == task_key
        assert worker.saved_errors
        assert worker.saved_errors[0][1] == "审查任务超时（0.01秒）"
        assert worker.saved_errors[0][2] == "timeout"
    finally:
        settings.review_timeout_seconds = old_value


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_submit_review_task_registers_cancel_event(monkeypatch):
    created = []

    async def fake_runner(worker, pr_info, task_key):
        del worker, pr_info
        return task_key

    class FakeTask:
        def add_done_callback(self, callback):
            callback(self)

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return FakeTask()

    monkeypatch.setattr(review_worker, "_run_review_task_with_timeout", fake_runner)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    pr_info = {"repo_full_name": "owner/repo", "repo_owner": "owner", "repo_name": "repo", "pr_number": 1}
    task_key = await submit_review_task(pr_info)

    worker = review_worker.get_worker()
    assert task_key == "owner/repo#1"
    assert task_key in worker._cancel_events
    assert created


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_review_worker_bridge_exception_does_not_fail_review(monkeypatch):
    called = []

    async def fake_handle(self, review_id):
        del self
        called.append(review_id)
        raise RuntimeError("bridge down")

    monkeypatch.setattr(
        "backend.services.agent_team.pr_review_feedback.AgentTeamPRReviewFeedbackService.handle_review_completed_with_result",
        fake_handle,
    )

    worker = ReviewWorker()
    await worker._notify_agent_team_review_completed(123, "task1")

    assert called == [123]
