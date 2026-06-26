"""ReviewWorker 外部 CI 失败上下文注入测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.workers.review_worker import ReviewWorker


@pytest.mark.asyncio
async def test_inject_external_ci_failures_adds_context_key(monkeypatch):
    fetch = AsyncMock(return_value=[{"name": "lint", "source": "check_run"}])
    monkeypatch.setattr(
        "backend.services.ci_failure_service.CIFailureService",
        lambda: MagicMock(fetch_for_review=fetch),
    )
    worker = object.__new__(ReviewWorker)
    context = {}
    pr_info = {
        "repo_full_name": "owner/repo",
        "head_sha": "sha1",
        "after": "sha-after",
    }

    await worker._inject_external_ci_failures(context, pr_info, "task-1")

    assert context["external_ci_failures"] == [
        {"name": "lint", "source": "check_run"}
    ]
    fetch.assert_awaited_once_with("owner/repo", "sha1")


@pytest.mark.asyncio
async def test_inject_external_ci_failures_uses_after_when_head_sha_missing(monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "backend.services.ci_failure_service.CIFailureService",
        lambda: MagicMock(fetch_for_review=fetch),
    )
    worker = object.__new__(ReviewWorker)
    context = {}
    pr_info = {"repo_full_name": "owner/repo", "after": "sha-after"}

    await worker._inject_external_ci_failures(context, pr_info, "task-1")

    assert "external_ci_failures" not in context
    fetch.assert_awaited_once_with("owner/repo", "sha-after")


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
