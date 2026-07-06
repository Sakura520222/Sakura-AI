"""Star Aid regression tests for completion gaps."""

from datetime import datetime, timezone

import pytest

from backend.core.github_app import GitHubAppClient
from backend.services import star_aid_github_service as github_service
from backend.services import star_aid_service
from backend.services import star_aid_summary_service as summary_service
from backend.webui.routes import star_aid as star_aid_route


def test_readme_excerpt_applies_ai_input_budget():
    """README 传给 AI 时按预算截取（模型 context 限制，非展示截断）。"""
    long_readme = "A" * 7000

    excerpt = summary_service.prepare_readme_for_prompt(long_readme, budget=6000)

    assert len(excerpt) == 6000
    assert excerpt == "A" * 6000


def test_readme_excerpt_zero_budget_keeps_full_text():
    long_readme = "B" * 7000

    excerpt = summary_service.prepare_readme_for_prompt(long_readme, budget=0)

    assert len(excerpt) == 7000


def test_sanitized_error_keeps_full_message():
    repo = type("Repo", (), {})()
    repo.ai_summary_status = "pending"
    repo.ai_summary_error = None
    repo.ai_summary_updated_at = None
    exc = RuntimeError("x" * 800)

    summary_service.apply_summary_failure(repo, exc, datetime.now(timezone.utc))

    assert repo.ai_summary_status == "failed"
    assert repo.ai_summary_error == str(exc)


def test_join_plan_interval_uses_configured_minimum(monkeypatch):
    seen = {}

    def fake_randint(lo, hi):
        seen["lo"] = lo
        seen["hi"] = hi
        return lo

    monkeypatch.setattr(star_aid_service.random, "randint", fake_randint)

    delay = star_aid_service.random_schedule_delay_minutes(15, 180)

    assert delay == 15
    assert seen == {"lo": 15, "hi": 180}


def test_repository_can_be_displayed_only_when_public_and_not_archived():
    public_repo = type("Repo", (), {"is_public": True, "is_archived": False})()
    private_repo = type("Repo", (), {"is_public": False, "is_archived": False})()
    archived_repo = type("Repo", (), {"is_public": True, "is_archived": True})()

    assert star_aid_service.repository_can_be_displayed(public_repo)
    assert not star_aid_service.repository_can_be_displayed(private_repo)
    assert not star_aid_service.repository_can_be_displayed(archived_repo)


@pytest.mark.asyncio
async def test_list_user_public_repositories_reads_all_pages(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, page):
            self.status_code = 200
            self._page = page

        def json(self):
            if self._page == 1:
                return [{"full_name": "owner/one"}]
            if self._page == 2:
                return [{"full_name": "owner/two"}]
            return []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            assert len(args) == 3
            return None

        async def get(self, *args, headers=None, params=None, timeout=None):
            assert args
            assert headers is not None
            assert timeout is not None
            page = params.get("page", 1)
            calls.append(page)
            return FakeResponse(page)

    monkeypatch.setattr(github_service.httpx, "AsyncClient", FakeClient)

    repos = await github_service.list_user_public_repositories("token")

    assert [r["full_name"] for r in repos] == ["owner/one", "owner/two"]
    assert calls == [1, 2, 3]


def test_github_app_client_exposes_user_token_helpers():
    client = GitHubAppClient()

    assert hasattr(client, "exchange_user_code")
    assert hasattr(client, "refresh_user_token")
    assert hasattr(client, "get_user_client")
    assert hasattr(client, "get_user_access_token")


def test_repo_daily_limit_zero_blocks_all_auto_targets():
    assert star_aid_service.repo_daily_limit_allows(0, current_count=0) is False
    assert star_aid_service.repo_daily_limit_allows(1, current_count=0) is True
    assert star_aid_service.repo_daily_limit_allows(1, current_count=1) is False


def test_repository_can_receive_star_requires_displayable_and_enabled():
    ok = type(
        "Repo",
        (),
        {
            "is_public": True,
            "is_archived": False,
            "is_displayed": True,
            "disabled_by_admin": False,
        },
    )()
    disabled = type(
        "Repo",
        (),
        {
            "is_public": True,
            "is_archived": False,
            "is_displayed": True,
            "disabled_by_admin": True,
        },
    )()
    hidden = type(
        "Repo",
        (),
        {
            "is_public": True,
            "is_archived": False,
            "is_displayed": False,
            "disabled_by_admin": False,
        },
    )()

    assert star_aid_service.repository_can_receive_star(ok)
    assert not star_aid_service.repository_can_receive_star(disabled)
    assert not star_aid_service.repository_can_receive_star(hidden)


@pytest.mark.asyncio
async def test_auth_callback_silently_handles_github_app_setup_action():
    """GitHub App 安装/更新触发的 setup callback（无 code/state）不应报授权错误。"""
    from unittest.mock import MagicMock

    resp = await star_aid_route.auth_callback(
        request=MagicMock(),
        setup_action="update",
        code=None,
        state=None,
        error=None,
        error_description=None,
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("/star-aid/")
    assert "_toast_type=error" not in location


@pytest.mark.asyncio
async def test_empty_ai_summary_marks_failed_and_retries(monkeypatch):
    """AI 返回空摘要时重试一次；仍空则标记 failed，返回 empty_summary。"""
    from backend.services import star_aid_summary_service as svc

    calls = {"n": 0}

    async def fake_generate(**kwargs):
        calls["n"] += 1
        return ""

    monkeypatch.setattr(svc, "generate_summary", fake_generate)

    repo = type(
        "Repo",
        (),
        {
            "id": 1,
            "full_name": "owner/repo",
            "owner_user_id": 1,
            "description": None,
            "topics_json": None,
            "primary_language": None,
            "readme_sha": None,
            "ai_summary": None,
            "ai_summary_status": "pending",
            "ai_summary_language": None,
            "ai_summary_error": None,
            "ai_summary_updated_at": None,
        },
    )()

    class FakeSession:
        async def execute(self, *a, **k):
            class R:
                def scalar_one_or_none(self):
                    return repo

            return R()

        async def flush(self):
            pass

    async def fake_token(*a, **k):
        return None, None

    monkeypatch.setattr(svc.gh, "get_effective_access_token", fake_token)

    result = await svc.refresh_repository_summary(FakeSession(), 1, force=True)

    assert result["status"] == "failed"
    assert result["error"] == "empty_summary"
    assert calls["n"] == 2  # 重试了一次


@pytest.mark.asyncio
async def test_generate_summary_ignores_reasoning_content_when_content_empty(monkeypatch):
    """思考模型 content 为空时不应回退用 reasoning_content（那是思考过程，不是摘要）。"""
    from unittest.mock import AsyncMock, MagicMock

    from backend.services import star_aid_summary_service as svc

    message = MagicMock()
    message.content = None
    message.reasoning_content = "1. Analyze the Request: ...（思考过程）"
    choice = MagicMock()
    choice.message = message
    resp = MagicMock()
    resp.choices = [choice]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)

    monkeypatch.setattr(svc, "_get_summary_client", lambda: (client, "model"))

    result = await svc.generate_summary(
        full_name="owner/repo",
        description="desc",
        topics=[],
        primary_language="Python",
        readme_excerpt="readme",
        lang="zh-CN",
    )

    # content 为空时返回空，触发上层重试/失败，绝不把思考过程当摘要
    assert result == ""
