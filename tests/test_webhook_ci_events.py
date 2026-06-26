"""webhook check_run / workflow_job 事件处理测试。"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.webhook import (
    handle_check_run_event,
    handle_workflow_job_event,
    resolve_pr_number_for_ci,
)


def _ci_service_factory():
    """构造 mock CIFailureService 实例。"""
    instance = MagicMock()
    instance.lookup_pr_number = AsyncMock(return_value=None)
    instance.record_check_run_failure = AsyncMock()
    instance.record_workflow_job_failure = AsyncMock()
    instance.upsert_head_sha_pr_map = AsyncMock()
    instance.cleanup_for_pr = AsyncMock(return_value=0)
    return instance


@pytest.fixture
def mock_ci(monkeypatch):
    """替换 CIFailureService 与 GitHubAppClient，隔离外部依赖。"""
    instance = _ci_service_factory()
    from backend.services import ci_failure_service

    monkeypatch.setattr(ci_failure_service, "CIFailureService", lambda: instance)
    monkeypatch.setattr(
        "backend.api.webhook.GitHubAppClient",
        lambda: MagicMock(get_pr_number_for_commit=MagicMock(return_value=None)),
    )
    return instance


def _repo_payload():
    return {
        "name": "repo",
        "full_name": "owner/repo",
        "owner": {"login": "owner"},
    }


# ---------------- resolve_pr_number_for_ci（三层降级） ----------------


@pytest.mark.asyncio
async def test_resolve_prefers_payload_pull_requests(mock_ci):
    result = await resolve_pr_number_for_ci(
        "owner", "repo", "owner/repo", "sha", [{"number": 42}]
    )
    assert result == 42
    mock_ci.lookup_pr_number.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_head_sha_map(mock_ci):
    mock_ci.lookup_pr_number = AsyncMock(return_value=7)
    result = await resolve_pr_number_for_ci(
        "owner", "repo", "owner/repo", "sha", []
    )
    assert result == 7


@pytest.mark.asyncio
async def test_resolve_returns_none_when_all_tiers_fail(mock_ci):
    result = await resolve_pr_number_for_ci(
        "owner", "repo", "owner/repo", "sha", []
    )
    assert result is None


# ---------------- handle_check_run_event ----------------


@pytest.mark.asyncio
async def test_check_run_completed_routes_to_service(mock_ci):
    payload = {
        "action": "completed",
        "check_run": {
            "id": 101,
            "name": "lint",
            "head_sha": "sha1",
            "conclusion": "failure",
            "output": {"title": "t", "summary": "s"},
            "pull_requests": [{"number": 9}],
        },
        "repository": _repo_payload(),
    }

    response = await handle_check_run_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "accepted"
    mock_ci.record_check_run_failure.assert_awaited_once()
    args = mock_ci.record_check_run_failure.call_args.args
    assert args[0] == "owner"  # repo_owner
    assert args[3] == 9  # pr_number
    assert args[4] == "sha1"  # head_sha


@pytest.mark.asyncio
async def test_check_run_ignores_non_completed_action(mock_ci):
    response = await handle_check_run_event({"action": "created", "check_run": {}})

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    mock_ci.record_check_run_failure.assert_not_called()


@pytest.mark.asyncio
async def test_check_run_ignores_self_sakura_check(mock_ci):
    payload = {
        "action": "completed",
        "check_run": {
            "id": 1,
            "name": "Sakura AI Review",
            "head_sha": "s",
            "conclusion": "failure",
            "pull_requests": [{"number": 1}],
        },
        "repository": _repo_payload(),
    }

    response = await handle_check_run_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    mock_ci.record_check_run_failure.assert_not_called()


@pytest.mark.asyncio
async def test_check_run_ignores_success_conclusion(mock_ci):
    payload = {
        "action": "completed",
        "check_run": {
            "id": 1,
            "name": "lint",
            "head_sha": "s",
            "conclusion": "success",
            "pull_requests": [{"number": 1}],
        },
        "repository": _repo_payload(),
    }

    response = await handle_check_run_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    assert body["reason"] == "non-failure conclusion"
    mock_ci.record_check_run_failure.assert_not_called()


@pytest.mark.asyncio
async def test_check_run_ignored_when_pr_unresolvable(mock_ci):
    mock_ci.lookup_pr_number = AsyncMock(return_value=None)
    payload = {
        "action": "completed",
        "check_run": {
            "id": 1,
            "name": "lint",
            "head_sha": "s",
            "conclusion": "failure",
            "pull_requests": [],
        },
        "repository": _repo_payload(),
    }

    response = await handle_check_run_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    mock_ci.record_check_run_failure.assert_not_called()


# ---------------- handle_workflow_job_event ----------------


@pytest.mark.asyncio
async def test_workflow_job_completed_routes_to_service(mock_ci):
    # workflow_job payload 无 pull_requests 字段，靠 head_sha_pr_map 兜底
    mock_ci.lookup_pr_number = AsyncMock(return_value=9)
    payload = {
        "action": "completed",
        "workflow_job": {
            "id": 202,
            "name": "tests",
            "head_sha": "sha2",
            "conclusion": "failure",
            "html_url": "https://github.com/o/r/actions/jobs/202",
            "steps": [{"name": "pytest", "conclusion": "failure"}],
        },
        "repository": _repo_payload(),
    }

    response = await handle_workflow_job_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "accepted"
    mock_ci.record_workflow_job_failure.assert_awaited_once()
    args = mock_ci.record_workflow_job_failure.call_args.args
    assert args[3] == 9  # pr_number（来自映射表）
    assert args[4] == "sha2"


@pytest.mark.asyncio
async def test_workflow_job_ignores_success_conclusion(mock_ci):
    payload = {
        "action": "completed",
        "workflow_job": {
            "id": 202,
            "name": "tests",
            "head_sha": "sha2",
            "conclusion": "success",
        },
        "repository": _repo_payload(),
    }

    response = await handle_workflow_job_event(payload)

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    assert body["reason"] == "non-failure conclusion"
    mock_ci.record_workflow_job_failure.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_job_ignores_non_completed_action(mock_ci):
    response = await handle_workflow_job_event(
        {"action": "in_progress", "workflow_job": {}}
    )

    body = json.loads(response.body)
    assert body["status"] == "ignored"
    mock_ci.record_workflow_job_failure.assert_not_called()
