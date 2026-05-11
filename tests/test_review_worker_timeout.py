"""Review worker dynamic timeout coverage."""

import asyncio

import pytest

from backend.core.config import get_settings
from backend.workers.review_worker import ReviewWorker, _run_review_task_with_timeout


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
