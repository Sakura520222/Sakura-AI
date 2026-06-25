"""ReviewWorker Check Run 集成测试。

验证生命周期集成点正确调用 CheckRunService。聚焦 _cancel_and_cleanup（最独立
可测的集成点）与 __init__ 持有 service；report_queued/completed/progress 的
输出正确性由 test_check_run_service.py 覆盖，集成点为直接 await 调用。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.check_run_service import CheckRunService
from backend.workers import review_worker
from backend.workers.review_worker import ReviewWorker


def _worker_with_mock_check_run():
    """绕过 __init__，构造 check_run_service / comment_service 已 mock 的 worker。"""
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.check_run_service = SimpleNamespace(
        report_queued=AsyncMock(),
        report_progress=AsyncMock(),
        report_completed=AsyncMock(),
        report_failed=AsyncMock(),
        report_cancelled=AsyncMock(),
        report_skipped=AsyncMock(),
    )
    worker.comment_service = SimpleNamespace(
        delete_placeholder_comment=AsyncMock(),
    )
    worker._update_review_status = AsyncMock()
    return worker


def _stub_worker_deps(monkeypatch):
    """stub 掉 ReviewWorker.__init__ 中的重量级依赖（保留真实 CheckRunService）。"""
    monkeypatch.setattr(review_worker, "GitHubAppClient", lambda: object())
    monkeypatch.setattr(review_worker, "PRAnalyzer", lambda: object())
    monkeypatch.setattr(review_worker, "AIReviewer", lambda: object())
    monkeypatch.setattr(review_worker, "CommentService", lambda: object())


# ---------------- __init__ 持有 service ----------------


def test_review_worker_init_has_check_run_service(monkeypatch):
    _stub_worker_deps(monkeypatch)
    worker = ReviewWorker()
    assert isinstance(worker.check_run_service, CheckRunService)


# ---------------- _cancel_and_cleanup → report_cancelled ----------------


@pytest.mark.asyncio
async def test_cancel_and_cleanup_reports_cancelled_with_head_sha():
    worker = _worker_with_mock_check_run()
    pr_info = {"repo_owner": "o", "repo_name": "r", "head_sha": "abc"}

    await worker._cancel_and_cleanup(
        "tid",
        "o/r#1",
        None,
        None,
        "reason",
        pr_info=pr_info,
        output_language="zh",
        head_sha="abc",
    )

    # review_id=None → 不更新 DB 状态
    worker._update_review_status.assert_not_called()
    worker.check_run_service.report_cancelled.assert_awaited_once()
    call = worker.check_run_service.report_cancelled.call_args
    assert call.args == ("o", "r", "abc")
    assert call.kwargs["output_language"] == "zh"


@pytest.mark.asyncio
async def test_cancel_and_cleanup_updates_status_and_reports_when_review_id():
    worker = _worker_with_mock_check_run()
    pr_info = {"repo_owner": "o", "repo_name": "r", "head_sha": "abc"}

    await worker._cancel_and_cleanup(
        "tid",
        "o/r#1",
        None,
        42,
        "reason",
        pr_info=pr_info,
        head_sha="abc",
    )

    worker._update_review_status.assert_awaited_once()
    worker.check_run_service.report_cancelled.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_and_cleanup_skips_check_run_when_no_head_sha():
    worker = _worker_with_mock_check_run()
    pr_info = {"repo_owner": "o", "repo_name": "r", "head_sha": None}

    await worker._cancel_and_cleanup(
        "tid",
        "o/r#1",
        None,
        None,
        "reason",
        pr_info=pr_info,
    )

    worker.check_run_service.report_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_cleanup_skips_check_run_when_no_pr_info():
    worker = _worker_with_mock_check_run()

    await worker._cancel_and_cleanup("tid", "o/r#1", None, None, "reason")

    worker.check_run_service.report_cancelled.assert_not_awaited()
