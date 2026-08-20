"""Regression tests for unexpected WebUI exception disclosure."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse

from backend.core import github_app
from backend.webui import i18n
from backend.webui.routes import agent_team, auth, issues, repos, scans, setup
from backend.workers import issue_worker, scan_worker

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
    setup_response = RedirectResponse("/setup")

    auth._set_webui_token_cookie(login_response, "login-token")
    auth._set_mfa_pending_cookie(pending_response, "pending-token")
    setup._set_setup_verified_cookie(setup_response, "setup-token")

    for response in (login_response, pending_response, setup_response):
        cookie_header = response.headers["set-cookie"]
        assert "Secure" in cookie_header
        assert "HttpOnly" in cookie_header


def test_language_cookie_replaces_unsupported_value():
    response = RedirectResponse("/auth/login")

    i18n.set_language_cookie(response, "en\r\nX-Injected: true")

    cookie_header = response.headers["set-cookie"]
    assert f"{i18n.LANG_COOKIE}={i18n.DEFAULT_LANGUAGE}" in cookie_header
    assert "X-Injected" not in cookie_header


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
async def test_workspace_deletion_hides_resolved_path(
    monkeypatch, route, route_kwargs
):
    class BrokenWorkspaceService:
        _WT_DIR_RE = agent_team.AgentTeamWorkspaceService._WT_DIR_RE

        def delete_workspace(self, *args):
            raise ValueError(f"path escaped: C:/secrets/{_SECRET}")

        def delete_worktree(self, *args):
            raise ValueError(f"path escaped: C:/secrets/{_SECRET}")

    monkeypatch.setattr(
        agent_team, "AgentTeamWorkspaceService", BrokenWorkspaceService
    )
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
    )
    analysis_result = SimpleNamespace(scalar_one_or_none=lambda: analysis)
    version_result = SimpleNamespace(scalar=lambda: 2)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[analysis_result, version_result])
    )
    monkeypatch.setattr(
        issue_worker,
        "submit_issue_analysis_task",
        AsyncMock(side_effect=RuntimeError(_SECRET)),
    )

    response = await issues.reanalyze_issue(
        request=object(),
        issue_id=1,
        db=db,
        user={"sub": "admin", "role": "admin"},
    )

    _assert_secret_is_hidden(response, "提交重新分析任务失败，请稍后重试")


@pytest.mark.asyncio
async def test_batch_issue_index_hides_repository_exception(monkeypatch):
    monkeypatch.setattr(
        "backend.core.config.get_settings",
        lambda: SimpleNamespace(enable_semantic_issue_linking=True),
    )
    monkeypatch.setattr(
        repos,
        "_get_installations_with_stats",
        AsyncMock(side_effect=RuntimeError(_SECRET)),
    )

    response = await repos.batch_index_issues(
        request=object(),
        db=object(),
        user={"user_id": 1},
        csrf_token="token",
    )

    _assert_secret_is_hidden(response, "获取仓库列表失败，请稍后重试")


@pytest.mark.asyncio
async def test_scan_trigger_hides_worker_exception(monkeypatch):
    class BrokenScanWorker:
        async def get_scan_candidates(self):
            raise RuntimeError(_SECRET)

    monkeypatch.setattr(scan_worker, "ScanWorker", BrokenScanWorker)

    response = await scans.trigger_scan(
        request=object(),
        user={"username": "admin"},
        _csrf="token",
    )

    _assert_secret_is_hidden(response, "触发扫描失败，请稍后重试")
