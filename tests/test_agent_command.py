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
    async def create_task_from_manual_issue(self, *a, **kw):
        return _FakeTask()


async def _async_noop(*a, **kw):
    """通用异步 no-op，用于 monkeypatch 同步函数为 async 版本"""
    return None


async def _async_true(*a, **kw):
    """返回 True 的异步函数，用于 mock 功能开关启用"""
    return True


async def _async_false(*a, **kw):
    """返回 False 的异步函数，用于 mock 功能开关禁用"""
    return False


def _make_base_mocks(
    monkeypatch,
    telegram_service_cls=_FakeTelegramService,
    scalar_result=_FakeAnalysis(),
    permission="admin",
):
    """为 /agent 测试统一注入基础 mock"""
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: _FakeGitHubApp(permission))
    monkeypatch.setattr(webhook, "TelegramService", telegram_service_cls)
    monkeypatch.setattr(
        webhook, "get_async_session", lambda: _FakeSession(scalar_result=scalar_result)
    )
    # get_dynamic_config 是 async 函数，返回 True 表示功能已启用
    monkeypatch.setattr(webhook, "get_dynamic_config", _async_true)
    # _post_issue_comment 是 async 函数，静默忽略评论发送
    monkeypatch.setattr(webhook, "_post_issue_comment", _async_noop)
    # mock 提交上下文构建函数，避免真实 GitHub API 调用
    import backend.services.agent_team.submission_context as sc_mod

    monkeypatch.setattr(sc_mod, "load_issue_comments_for_context", _async_noop)
    # asyncio.to_thread 直接调用同步函数（测试环境下无真实事件循环阻塞）
    async def _sync_to_thread(fn, *args):
        return fn(*args)

    monkeypatch.setattr(webhook.asyncio, "to_thread", _sync_to_thread)
    # 默认 mock AgentTeamCandidateService，避免真实 GitHub API 调用
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        _FakeCandidateService,
    )


@pytest.mark.asyncio
async def test_agent_command_ignored_on_pr():
    payload = _base_payload()
    payload["issue"]["pull_request"] = {"url": "https://github.com/owner/repo/pull/42"}

    response = await webhook.handle_issue_comment_event(payload)

    assert response.status_code == 200
    assert b"ignored" in response.body


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
async def test_agent_command_skipped_when_feature_disabled(monkeypatch):
    payload = _base_payload()
    # 功能开关返回 False，其余 mock 不需要
    monkeypatch.setattr(webhook, "get_dynamic_config", _async_false)

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"agent team feature disabled" in response.body


@pytest.mark.asyncio
async def test_agent_command_denied_for_insufficient_permission(monkeypatch):
    payload = _base_payload()
    _make_base_mocks(monkeypatch, permission="read")

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"denied" in response.body


@pytest.mark.asyncio
async def test_agent_command_skipped_when_no_analysis(monkeypatch):
    payload = _base_payload()
    _make_base_mocks(monkeypatch, scalar_result=None)

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"no completed analysis" in response.body


@pytest.mark.asyncio
async def test_agent_command_skipped_when_quota_exceeded(monkeypatch):
    payload = _base_payload()
    _make_base_mocks(monkeypatch, telegram_service_cls=_FakeTelegramServiceQuotaExceeded)

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

    _make_base_mocks(monkeypatch, telegram_service_cls=CapturingService)
    # 使用模块全路径 monkeypatch，因为 AgentTeamCandidateService 是延迟导入（函数内 import）
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        CapturingCandidateService,
    )

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"accepted" in response.body
    assert captured["base_branch"] == "develop"


@pytest.mark.asyncio
async def test_agent_command_default_base_branch_is_none(monkeypatch):
    """验证不带 base: 参数时，base_branch 为 None"""
    payload = _base_payload("/agent")
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

    _make_base_mocks(monkeypatch, telegram_service_cls=CapturingService)
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        CapturingCandidateService,
    )

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"accepted" in response.body
    assert captured["base_branch"] is None


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

    _make_base_mocks(monkeypatch, telegram_service_cls=CapturingService)
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.AgentTeamCandidateService",
        FailingCandidateService,
    )

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert "已存在进行中的 Agent 任务" in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_agent_command_rejects_invalid_branch_name(monkeypatch):
    payload = _base_payload("/agent base:../bad")

    response = await webhook.handle_agent_command(payload)

    assert response.status_code == 200
    assert b"\xe6\x97\xa0\xe6\x95\x88" in response.body  # "无效" in UTF-8
