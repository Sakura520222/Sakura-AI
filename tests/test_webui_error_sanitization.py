"""Regression tests for unexpected WebUI exception disclosure."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse

from backend.api.v1 import issues as api_issues
from backend.api.v1 import scans as api_scans
from backend.core import github_app
from backend.services.agent_team import api_operations as agent_team
from backend.webui.routes import auth
from backend.workers import scan_worker

_SECRET = "mysql+asyncmy://admin:super-secret@db/sakura"


def _payload(response) -> dict:
    return json.loads(response.body)


def _assert_secret_is_hidden(response, expected_message: str) -> None:
    payload = _payload(response)
    assert payload == {"success": False, "message": expected_message}
    assert _SECRET.encode() not in response.body


def test_sensitive_auth_cookies_are_always_secure(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            webui_cookie_secure=False,
            two_factor_pending_token_expire_minutes=5,
        ),
    )
    login_response = JSONResponse({"success": True})
    pending_response = RedirectResponse("/auth/mfa")

    auth._set_webui_token_cookie(login_response, "login-token")
    auth._set_mfa_pending_cookie(pending_response, "pending-token")

    for response in (login_response, pending_response):
        cookie_header = response.headers["set-cookie"]
        assert "Secure" in cookie_header
        assert "HttpOnly" in cookie_header


@pytest.mark.asyncio
async def test_branch_list_hides_github_client_exception(monkeypatch):
    class BrokenGitHubAppClient:
        def __init__(self):
            raise RuntimeError(_SECRET)

    monkeypatch.setattr(github_app, "GitHubAppClient", BrokenGitHubAppClient)

    response = await agent_team.list_repo_branches(
        "owner", "repo", user={"role": "admin"}
    )

    _assert_secret_is_hidden(response, "获取分支列表失败，请稍后重试")


@pytest.mark.asyncio
async def test_candidate_preview_hides_service_exception(monkeypatch):
    monkeypatch.setattr(
        agent_team.AgentTeamCandidateService,
        "collect_candidates",
        AsyncMock(side_effect=RuntimeError(_SECRET)),
    )

    response = await agent_team.preview_candidates(
        db=object(),
        user={"user_id": 1},
        csrf_token="token",
        ai_filter_requirement="",
    )

    _assert_secret_is_hidden(response, "AI 筛选候选失败，请稍后重试")


@pytest.mark.asyncio
async def test_task_creation_does_not_use_removed_ai_config_validation(monkeypatch):
    monkeypatch.setattr(
        agent_team.AgentTeamCandidateService,
        "collect_candidates",
        AsyncMock(return_value=[]),
    )

    response = await agent_team.create_task_from_candidate(
        background_tasks=BackgroundTasks(),
        db=object(),
        user={"user_id": 1, "sub": "admin"},
        csrf_token="token",
    )

    assert response.status_code == 404
    assert _SECRET.encode() not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "route_kwargs"),
    [
        (
            agent_team.delete_workspace,
            {"repo_owner": "owner", "repo_name": "repo"},
        ),
        (
            agent_team.delete_worktree,
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "dir_name": "invalid-path",
            },
        ),
    ],
)
async def test_workspace_deletion_hides_resolved_path(monkeypatch, route, route_kwargs):
    class BrokenWorkspaceService:
        _WT_DIR_RE = agent_team.AgentTeamWorkspaceService._WT_DIR_RE

        def delete_workspace(self, *args):
            raise ValueError(f"path escaped: C:/secrets/{_SECRET}")

        def delete_worktree(self, *args):
            raise ValueError(f"path escaped: C:/secrets/{_SECRET}")

    monkeypatch.setattr(agent_team, "AgentTeamWorkspaceService", BrokenWorkspaceService)
    db = SimpleNamespace(scalar=AsyncMock(return_value=0))

    response = await route(
        db=db,
        user={"user_id": 1},
        csrf_token="token",
        **route_kwargs,
    )

    _assert_secret_is_hidden(response, "工作区路径无效")


@pytest.mark.asyncio
async def test_issue_reanalysis_hides_worker_exception(monkeypatch):
    analysis = SimpleNamespace(
        issue_number=7,
        repo_name="repo",
        repo_owner="owner",
        author="alice",
        title="title",
        body="body",
        issue_state="open",
    )
    analysis_result = SimpleNamespace(scalar_one_or_none=lambda: analysis)
    version_result = SimpleNamespace(scalar=lambda: 2)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[analysis_result, version_result])
    )
    submit = AsyncMock(side_effect=RuntimeError(_SECRET))
    monkeypatch.setattr(api_issues, "submit_issue_reanalysis", submit)

    response = await api_issues.reanalyze_issue(
        issue_id=1,
        db=db,
        user={"sub": "admin", "role": "admin"},
    )

    assert _payload(response) == {
        "success": False,
        "error": "提交分析任务失败，请稍后重试",
    }
    assert _SECRET.encode() not in response.body
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_trigger_hides_worker_exception(monkeypatch):
    class BrokenScanWorker:
        async def get_scan_candidates(self):
            raise RuntimeError(_SECRET)

    monkeypatch.setattr(scan_worker, "ScanWorker", BrokenScanWorker)

    # 绕过限流装饰器，直接验证端点的异常脱敏行为。
    trigger = getattr(api_scans.trigger_scan, "__wrapped__", api_scans.trigger_scan)
    response = await trigger(request=object(), user={"username": "admin"})

    assert _payload(response) == {"success": False, "error": "触发扫描失败"}
    assert _SECRET.encode() not in response.body
