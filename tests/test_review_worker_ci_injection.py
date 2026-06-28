"""ReviewWorker 外部 CI 失败上下文注入测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.workers.review_worker import ReviewWorker


@pytest.mark.asyncio
async def test_inject_external_ci_failures_prefers_after_for_incremental(monkeypatch):
    """增量审查（synchronize）时 after 是新 head，应优先取 after 读取新提交的 CI 失败。"""
    fetch = AsyncMock(return_value=[{"name": "lint", "source": "check_run"}])
    monkeypatch.setattr(
        "backend.services.ci_failure_service.CIFailureService",
        lambda: MagicMock(fetch_for_review=fetch),
    )
    worker = object.__new__(ReviewWorker)
    context = {}
    pr_info = {
        "repo_full_name": "owner/repo",
        "head_sha": "old-sha",
        "after": "new-sha",
    }

    await worker._inject_external_ci_failures(context, pr_info, "task-1")

    assert context["external_ci_failures"] == [{"name": "lint", "source": "check_run"}]
    fetch.assert_awaited_once_with("owner/repo", "new-sha")


@pytest.mark.asyncio
async def test_inject_external_ci_failures_falls_back_to_head_sha(monkeypatch):
    """首次审查（opened）无 after，回退到 head_sha。"""
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "backend.services.ci_failure_service.CIFailureService",
        lambda: MagicMock(fetch_for_review=fetch),
    )
    worker = object.__new__(ReviewWorker)
    context = {}
    pr_info = {"repo_full_name": "owner/repo", "head_sha": "sha1"}

    await worker._inject_external_ci_failures(context, pr_info, "task-1")

    assert "external_ci_failures" not in context
    fetch.assert_awaited_once_with("owner/repo", "sha1")


@pytest.mark.asyncio
async def test_inject_external_ci_failures_swallows_errors(monkeypatch):
    fetch = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(
        "backend.services.ci_failure_service.CIFailureService",
        lambda: MagicMock(fetch_for_review=fetch),
    )
    worker = object.__new__(ReviewWorker)
    context = {}
    pr_info = {"repo_full_name": "owner/repo", "head_sha": "sha1"}

    await worker._inject_external_ci_failures(context, pr_info, "task-1")

    assert "external_ci_failures" not in context
