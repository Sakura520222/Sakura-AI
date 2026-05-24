"""Unit tests for /agent command in webhook handler."""

import pytest

from backend.api import webhook


def _base_payload(body="/agent"):
    return {
        "action": "created",
        "comment": {
            "body": body,
            "user": {"login": "collaborator"},
        },
        "issue": {
            "number": 42,
            "title": "Test Issue",
            "body": "Issue body",
            "user": {"login": "issue_author"},
            "state": "open",
        },
        "repository": {
            "name": "repo",
            "full_name": "owner/repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 1},
    }


class _FakeAnalysis:
    id = 1
    issue_number = 42
    repo_name = "repo"
    repo_owner = "owner"
    author = "tester"
    title = "Test Issue"
    category = "feature"
    priority = "medium"
    summary = "Test summary"
    feasibility = ""
    suggested_title = None
    suggested_labels = None
    suggested_assignees = None
    related_prs = None
    duplicate_of = None
    status = "completed"
    error_message = None
    prompt_tokens = 0
    completion_tokens = 0
    estimated_cost = 0
    comment_posted = False
    comment_url = None
    analysis_detail = None
    created_at = None
    completed_at = None


class _FakeTask:
    id = 99


class _FakeSession:
    def __init__(self, scalar_result=None):
        self._scalar_result = scalar_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def scalar(self, stmt):
        return self._scalar_result


class _FakeTelegramService:
    def __init__(self, session):
        self.session = session

    async def check_and_consume_agent_quota(self, *a, **kw):
        return True, ""


class _FakeTelegramServiceQuotaExceeded:
    def __init__(self, session):
        self.session = session

    async def check_and_consume_agent_quota(self, *a, **kw):
        return False, "Agent quota exceeded"


class _FakeGitHubApp:
    def __init__(self, permission="admin"):
        self._permission = permission

    def check_collaborator_permission(self, *a, **kw):
        return self._permission

    def get_repo_client(self, *a, **kw):
        return None


class _FakeCandidateService:
    def __init__(self, task=None, error=None):
        self._task = task
        self._error = error

    async def create_task_from_manual_issue(self, *a, **kw):
        if self._error:
            raise self._error
        return self._task


@pytest.mark.asyncio
async def test_agent_command_ignored_on_pr():
    payload = _base_payload()
    payload["issue"]["pull_request"] = {"url": "https://github.com/owner/repo/pull/42"}

    response = await webhook.handle_issue_comment_event(payload)

    assert response.status_code == 200
    body = response.body
    assert b"ignored" in body


@pytest.mark.asyncio
async def test_agent_command_ignores_bot_self_comment():
    payload = _base_payload()
    from backend.core.config import get_settings

    settings = get_settings()
    old = settings.bot_username
    try:
        settings.bot_username = "collaborator"
        response = await webhook.handle_agent_command(payload)
        assert response.status_code == 200
        assert b"bot self-comment" in response.body
    finally:
        settings.bot_username = old


@pytest.mark.asyncio
async def test_agent_command_denied_for_insufficient_permission(monkeypatch):
    payload = _base_payload()
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: _FakeGitHubApp(permission="read"))

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"denied" in response.body


@pytest.mark.asyncio
async def test_agent_command_skipped_when_no_analysis(monkeypatch):
    payload = _base_payload()
    monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGitHubApp)
    monkeypatch.setattr(webhook, "TelegramService", _FakeTelegramService)
    monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession(scalar_result=None))

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"no completed analysis" in response.body


@pytest.mark.asyncio
async def test_agent_command_skipped_when_quota_exceeded(monkeypatch):
    payload = _base_payload()
    monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGitHubApp)
    monkeypatch.setattr(webhook, "TelegramService", _FakeTelegramServiceQuotaExceeded)
    monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession(scalar_result=_FakeAnalysis()))

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"quota exceeded" in response.body


@pytest.mark.asyncio
async def test_agent_command_parses_base_branch(monkeypatch):
    payload = _base_payload("/agent base:develop")
    captured = {}

    class CapturingService:
        def __init__(self, session):
            pass

        async def check_and_consume_agent_quota(self, *a, **kw):
            return True, ""

    class CapturingCandidateService:
        async def create_task_from_manual_issue(
            self, db, repo_full_name, issue_number, started_by,
            ai_config_snapshot=None, base_branch=None, overrides=None,
        ):
            captured["base_branch"] = base_branch
            return _FakeTask()

    monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGitHubApp)
    monkeypatch.setattr(webhook, "TelegramService", CapturingService)
    monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession(scalar_result=_FakeAnalysis()))
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        CapturingCandidateService,
    )

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"accepted" in response.body
    assert captured["base_branch"] == "develop"


@pytest.mark.asyncio
async def test_agent_command_rejected_when_task_already_exists(monkeypatch):
    payload = _base_payload()

    class CapturingService:
        def __init__(self, session):
            pass

        async def check_and_consume_agent_quota(self, *a, **kw):
            return True, ""

    class FailingCandidateService:
        async def create_task_from_manual_issue(self, *a, **kw):
            raise ValueError("已存在进行中的 Agent 任务")

    monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGitHubApp)
    monkeypatch.setattr(webhook, "TelegramService", CapturingService)
    monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession(scalar_result=_FakeAnalysis()))
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        FailingCandidateService,
    )

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert "已存在进行中的 Agent 任务" in response.body.decode("utf-8")
