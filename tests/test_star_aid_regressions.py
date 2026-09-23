"""Star Aid regression tests for completion gaps."""

import subprocess
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment
from sqlalchemy.dialects import mysql
from starlette.datastructures import QueryParams

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

    summary_service.apply_summary_failure(repo, exc, datetime.now(UTC))

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

        async def get(self, *args, headers=None, params=None, timeout=None):
            assert args
            assert headers is not None
            assert timeout is not None
            page = params.get("page", 1)
            calls.append(page)
            return FakeResponse(page)

    monkeypatch.setattr(github_service.httpx, "AsyncClient", FakeClient)

    result = await github_service.list_user_public_repositories("token")

    assert result.success is True
    assert result.complete is True
    assert [r["full_name"] for r in result.repositories] == ["owner/one", "owner/two"]
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
    """安装/更新触发的 setup callback（无 code/state）回仪表盘，不报授权错误。"""
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
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_auth_callback_setup_install_with_code_redirects_to_dashboard(
    monkeypatch,
):
    """安装审查 App 时 GitHub 附带 user authorization 回调（setup_action=install，带 code 无 state）：
    应回仪表盘，且不把 code 当成仓库互助授权消费。"""
    from unittest.mock import MagicMock

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("exchange_authorization_code 不应在 setup 回调中被调用")

    monkeypatch.setattr(
        star_aid_route.gh_service, "exchange_authorization_code", _fail_if_called
    )

    resp = await star_aid_route.auth_callback(
        request=MagicMock(),
        setup_action="install",
        code="fake-auth-code",
        state=None,
        error=None,
        error_description=None,
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


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

        async def commit(self):
            pass

    async def fake_token(*a, **k):
        return None, None

    monkeypatch.setattr(svc.gh, "get_effective_access_token", fake_token)

    async def fake_resolve(session, owner_user_id=None):
        return "zh-CN"

    monkeypatch.setattr(svc, "_resolve_summary_language", fake_resolve)

    result = await svc.refresh_repository_summary(FakeSession(), 1, force=True)

    assert result["status"] == "failed"
    assert result["error"] == "empty_summary"
    assert calls["n"] == 2  # 重试了一次


@pytest.mark.asyncio
async def test_resolve_summary_language_uses_owner_preference(monkeypatch):
    """star_aid_summary_language 配置空时，按仓库 owner 的偏好语言生成摘要。"""
    from backend.services import star_aid_summary_service as svc

    async def fake_cfg(key):
        return None  # star_aid_summary_language 未配置

    monkeypatch.setattr(svc, "get_dynamic_config", fake_cfg)

    class FakeResult:
        def first(self):
            return ("en",)  # owner 的 WebUIConfig.language

    class FakeSession:
        async def execute(self, *args, **kwargs):
            return FakeResult()

    lang = await svc._resolve_summary_language(FakeSession(), owner_user_id=42)

    assert lang == "en"


@pytest.mark.asyncio
async def test_resolve_summary_language_config_overrides_owner_preference(monkeypatch):
    """star_aid_summary_language 配置非空时，覆盖 owner 偏好语言。"""
    from backend.services import star_aid_summary_service as svc

    async def fake_cfg(key):
        return "zh-CN"  # 全局强制中文

    monkeypatch.setattr(svc, "get_dynamic_config", fake_cfg)

    class FakeSession:
        async def execute(self, *args, **kwargs):
            raise AssertionError("不应查 owner 偏好（配置已覆盖）")

    lang = await svc._resolve_summary_language(FakeSession(), owner_user_id=42)

    assert lang == "zh-CN"
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
    client.call_with_retry = AsyncMock(return_value=resp)

    monkeypatch.setattr(svc, "_get_summary_client", lambda: (client, ""))

    result = await svc.generate_summary(
        full_name="owner/repo",
        description="desc",
        topics=[],
        primary_language="Python",
        readme_excerpt="readme",
        lang="zh-CN",
        max_tokens=16000,
    )

    # content 为空时返回空，触发上层重试/失败，绝不把思考过程当摘要
    assert result == ""


@pytest.mark.asyncio
async def test_leave_plan_rejects_banned_member(monkeypatch):
    """被封禁成员不能通过 leave 清除封禁状态绕过封禁（P1）。"""
    from unittest.mock import MagicMock

    from backend.models.star_aid_models import (
        MEMBER_STATUS_BANNED,
        StarAidMember,
    )

    banned = StarAidMember(user_id=7, github_username="x", status=MEMBER_STATUS_BANNED)

    async def fake_get_member(session, user_id):
        return banned

    monkeypatch.setattr(star_aid_service, "get_member", fake_get_member)

    result = await star_aid_service.leave_plan(MagicMock(), 7)

    assert result == {"success": False, "message": "banned"}
    assert banned.status == MEMBER_STATUS_BANNED  # 封禁状态未被清除


@pytest.mark.asyncio
async def test_select_repositories_rejects_non_active_member(monkeypatch):
    """非 active 成员（已退出/被封禁）不得修改展示仓库写入公开池。"""
    from unittest.mock import AsyncMock, MagicMock

    from backend.models.star_aid_models import MEMBER_STATUS_LEFT, StarAidMember

    left = StarAidMember(user_id=7, github_username="x", status=MEMBER_STATUS_LEFT)

    async def fake_get_member(session, user_id):
        return left

    monkeypatch.setattr(star_aid_service, "get_member", fake_get_member)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=AssertionError("非 active 不应查仓库"))

    count = await star_aid_service.select_repositories(session, 7, ["a/b"])

    assert count == 0


@pytest.mark.asyncio
async def test_join_plan_requires_valid_token(monkeypatch):
    """token 失效时不得加入互助池（避免只收 star 不贡献）。"""
    from unittest.mock import AsyncMock, MagicMock

    async def fake_enabled():
        return True

    async def fake_get_member(session, user_id):
        return None

    async def fake_token(session, user_id):
        return None, MagicMock(reauth_required=True)

    monkeypatch.setattr(star_aid_service, "is_feature_enabled", fake_enabled)
    monkeypatch.setattr(star_aid_service, "get_member", fake_get_member)
    monkeypatch.setattr(star_aid_service.gh, "get_effective_access_token", fake_token)

    result = await star_aid_service.join_plan(AsyncMock(), 7, "gh-user", ["a/b"])

    assert result == {"success": False, "message": "reauth_required"}


@pytest.mark.asyncio
async def test_upsert_action_log_preserves_existing_created_star():
    """已记录的 created_star=True 不被后续 already_done 的默认 False 覆盖。"""
    from backend.models.star_aid_models import StarAidActionLog

    existing = StarAidActionLog(
        actor_user_id=1,
        target_repository_id=2,
        action="manual_star",
        trigger="manual",
        status="success",
        created_star=True,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return existing

    class FakeSession:
        async def execute(self, *args, **kwargs):
            return FakeResult()

        async def flush(self):
            pass

    await star_aid_service._upsert_action_log(
        FakeSession(),
        actor_user_id=1,
        target_repository_id=2,
        action="manual_star",
        trigger="manual",
        status="already_done",
        created_star=False,  # 再次点击默认 False
    )

    assert existing.created_star is True  # 保留历史 True，退出时仍能识别为本功能创建


def test_admin_repository_query_normalizes_untrusted_values():
    query = star_aid_service._normalize_admin_repository_query(
        q="  Sakura-AI  ",
        status="unexpected",
        sort="DROP TABLE",
        order="sideways",
        page=-8,
        page_size=9999,
    )

    assert query == {
        "q": "Sakura-AI",
        "status": "all",
        "sort": "stars",
        "order": "desc",
        "page": 1,
        "page_size": 100,
    }


def test_public_repository_search_attribute_does_not_create_event_handlers():
    class ArticleParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.attributes = None

        def handle_starttag(self, tag, attrs):
            if tag == "article" and self.attributes is None:
                self.attributes = dict(attrs)

    source = Path("backend/webui/templates/star_aid/index.html").read_text()
    article = next(line for line in source.splitlines() if "<article x-show=" in line)
    rendered = Environment(autoescape=True).from_string(article).render(
        repo={
            "full_name": "owner/repo",
            "description": "description onmouseover=alert(1) tail",
            "primary_language": "Python",
        }
    )
    parser = ArticleParser()
    parser.feed(rendered)

    assert set(parser.attributes) == {"x-show", "class"}
    assert "onmouseover=alert(1)" in parser.attributes["x-show"]
    assert ".includes(publicSearch.toLowerCase())" in parser.attributes["x-show"]


def test_admin_repository_search_treats_like_wildcards_as_literals():
    for term, expected in (("_", r"%\_%"), ("%", r"%\%%"), (r"a\b", r"%a\\b%")):
        query = star_aid_service._normalize_admin_repository_query(q=term)
        condition = star_aid_service._admin_repository_filters(query)[0]
        compiled = condition.compile(dialect=mysql.dialect())
        assert list(compiled.params.values()) == [expected] * 3
        assert compiled.string.count("ESCAPE") == 3


def test_admin_pagination_links_keep_both_filter_states():
    source = Path("backend/webui/templates/star_aid/index.html").read_text()
    admin_markup = source.split('{% if state.is_admin %}', 1)[1].split('{% endif %}\n    </section>', 1)[0]
    state = SimpleNamespace(
        admin_repository_page={
            "items": [], "q": "my_repo", "status": "disabled", "sort": "name",
            "order": "asc", "page": 2, "page_size": 10, "pages": 3, "total": 25,
        },
        admin_member_page={
            "q": "ali%", "status": "active", "page": 2, "page_size": 20,
            "pages": 3, "all_total": 45,
        },
        admin_repositories=[], admin_members=[], is_admin=True,
    )
    rendered = Environment(autoescape=True).from_string(admin_markup).render(
        state=state, request=SimpleNamespace(query_params=QueryParams({})),
        _=lambda key, **kwargs: key, csrf_token="test",
    )

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.links.append(dict(attrs))

    parser = LinkParser()
    parser.feed(rendered)
    pagination = [link for link in parser.links if "page=" in link.get("href", "")]
    assert len(pagination) == 4
    for link in pagination:
        params = QueryParams(link["href"].split("?", 1)[1])
        assert params["member_q"] == "ali%"
        assert params["member_status"] == "active"
        assert params["q"] == "my_repo"
        assert params["status"] == "disabled"
        assert params["sort"] == "name"
        assert params["order"] == "asc"


def test_member_filter_submission_keeps_repository_state():
    source = Path("backend/webui/templates/star_aid/index.html").read_text()
    admin_markup = source.split('{% if state.is_admin %}', 1)[1].split('{% endif %}\n    </section>', 1)[0]
    state = SimpleNamespace(
        admin_repository_page={
            "items": [], "q": "my_repo", "status": "disabled", "sort": "name",
            "order": "asc", "page": 3, "page_size": 10, "pages": 3, "total": 25,
        },
        admin_member_page={
            "q": "ali", "status": "active", "page": 2, "page_size": 20,
            "pages": 2, "all_total": 30,
        },
        admin_repositories=[], admin_members=[], is_admin=True,
    )
    rendered = Environment(autoescape=True).from_string(admin_markup).render(
        state=state, request=SimpleNamespace(query_params=QueryParams({})),
        _=lambda key, **kwargs: key, csrf_token="test",
    )

    class InputParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_member_form = False
            self.inputs = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "form" and attributes.get("method") == "get":
                self.in_member_form = True
            if tag == "input" and self.in_member_form:
                self.inputs[attributes.get("name")] = attributes.get("value")

        def handle_endtag(self, tag):
            if tag == "form":
                self.in_member_form = False

    parser = InputParser()
    parser.feed(rendered)
    assert {key: parser.inputs.get(key) for key in ("q", "status", "sort", "order", "page", "page_size")} == {
        "q": "my_repo", "status": "disabled", "sort": "name", "order": "asc", "page": "3", "page_size": "10",
    }


def test_admin_filter_navigation_preserves_other_panel_params():
    script = Path("backend/webui/templates/star_aid/index.html").read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    program = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8');
const mode = process.argv[1];
const listeners = {};
const location = { href: 'https://example.test/star-aid/?member_q=alice&member_status=active&member_page=3&member_page_size=50&q=old&status=all&page=4' };
global.window = { location };
const currentResults = { replaceWith: () => {} };
const currentPanel = { querySelector: selector => ({
    '#star-aid-admin-repository-results': currentResults,
    'summary': { textContent: '' },
    '[name="page"]': { value: '1' },
})[selector] };
const nextPanel = { querySelector: selector => ({
    '#star-aid-admin-repository-results': {},
    'summary': { textContent: 'repositories' },
    '[name="page"]': { value: '1' },
})[selector] };
global.document = {
    addEventListener: (name, callback) => { listeners[name] = callback; },
    querySelector: selector => selector === '#star-aid-admin-repositories' ? currentPanel : null,
};
global.history = { replaceState: (_state, _title, url) => { location.href = url; } };
global.fetch = async (url) => {
    requested.push(url);
    return { ok: true, text: async () => '<div></div>' };
};
global.DOMParser = class { parseFromString() { return { querySelector: () => nextPanel }; } };
const requested = [];
vm.runInThisContext(script);
const form = {
    action: 'https://example.test/star-aid/',
    elements: [['page', '1'], ['q', 'new'], ['status', 'disabled'], ['sort', 'stars'], ['order', 'desc'], ['page_size', '20']],
    querySelector: () => ({ value: '1' }),
};
global.FormData = class { constructor(form) { return form.elements; } };
listeners.submit({ target: { closest: selector => selector === '#star-aid-admin-repository-filters' ? form : null }, preventDefault() {} });
setTimeout(() => {
    const url = new URL(requested[0]);
    for (const [key, value] of Object.entries({ member_q: 'alice', member_status: 'active', member_page: '3', member_page_size: '50', q: 'new' })) {
        assert.equal(url.searchParams.get(key), value);
    }
    const hidden = Object.fromEntries(['q', 'status', 'sort', 'order', 'page', 'page_size'].map(key => [key, { value: key === 'q' ? 'old' : '' }]));
    const memberForm = { querySelector: selector => hidden[/\[name="([^"]+)"\]/.exec(selector)[1]] };
    listeners.submit({ target: { closest: selector => selector === '#star-aid-admin-member-filters' ? memberForm : null } });
    assert.equal(hidden.q.value, 'new');
    assert.equal(hidden.status.value, 'disabled');
    const pageLink = { href: 'https://example.test/star-aid/?q=new&status=disabled&page=2' };
    listeners.click({ target: { closest: selector => selector === '[data-admin-repository-page]' ? pageLink : null }, preventDefault() {} });
    const pageUrl = new URL(requested[1]);
    assert.equal(pageUrl.searchParams.get('member_q'), 'alice');
    assert.equal(pageUrl.searchParams.get('member_page'), '3');
    console.log('ok');
}, 0);
"""
    result = subprocess.run(
        ["node", "-e", program], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_admin_repository_filters_cover_search_and_each_status():
    for status, expected_fragment in (
        ("displayed", "is_displayed"),
        ("not_displayed", "is_displayed"),
        ("disabled", "disabled_by_admin"),
    ):
        query = star_aid_service._normalize_admin_repository_query(
            q="owner/repo", status=status
        )
        filters = star_aid_service._admin_repository_filters(query)
        rendered = " ".join(str(condition) for condition in filters)

        assert "full_name" in rendered
        assert "owner_login" in rendered
        assert "repo_name" in rendered
        assert expected_fragment in rendered


@pytest.mark.asyncio
async def test_admin_repository_page_applies_sort_and_pagination():
    from types import SimpleNamespace

    repo = SimpleNamespace(
        id=8,
        full_name="owner/repo",
        owner_login="owner",
        repo_name="repo",
        html_url="https://github.com/owner/repo",
        description=None,
        topics_json=None,
        primary_language="Python",
        stargazers_count=42,
        pushed_at=datetime(2026, 9, 1, tzinfo=UTC),
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ai_summary=None,
        ai_summary_status="pending",
        ai_summary_language=None,
        is_displayed=True,
        disabled_by_admin=False,
    )

    class ScalarRows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class Result:
        def __init__(self, *, total=None, rows=None):
            self._total = total
            self._rows = rows or []

        def scalar_one(self):
            return self._total

        def scalars(self):
            return ScalarRows(self._rows)

    class FakeSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            if len(self.statements) == 1:
                return Result(total=41)
            return Result(rows=[repo])

    session = FakeSession()
    result = await star_aid_service.get_admin_repository_page(
        session,
        q="owner",
        status="displayed",
        sort="name",
        order="asc",
        page=3,
        page_size=20,
    )

    assert result["total"] == 41
    assert result["pages"] == 3
    assert result["page"] == 3
    assert result["items"][0]["full_name"] == "owner/repo"
    rendered = str(session.statements[1])
    assert "ORDER BY star_aid_repositories.full_name ASC" in rendered
    assert "LIMIT" in rendered and "OFFSET" in rendered


def test_star_aid_template_refreshes_only_the_repository_list():
    """筛选只替换仓库列表 DOM，不触发整页导航。"""
    template = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/star_aid/index.html"
    ).read_text(encoding="utf-8")

    assert 'name="page_size"' in template
    assert 'data-admin-repository-search' in template
    assert template.count('data-admin-repository-filter class=') == 4
    assert "setTimeout(() => refreshFromForm(form), 350)" in template
    assert "currentResults.replaceWith(nextResults)" in template
    assert "currentPanel.replaceWith(nextPanel)" not in template
    assert 'id="star-aid-admin-repository-results"' in template
    assert 'id="star-aid-admin-repositories"' in template
    assert 'id="star-aid-admin-repositories" class="rounded-lg border border-gray-200 dark:border-gray-700" open' not in template
    assert 'name="member_q"' in template
    assert 'name="member_status"' in template
    assert 'name="member_page_size"' in template
    assert "star_aid.bulk_actions_later" in template
    assert "data-admin-repository-page" in template
    assert "{{ _('common.search') }}" not in template
    assert ">Sakura AI</p>" not in template


def test_star_aid_template_posts_selected_repository_names_to_expected_field():
    """编辑展示仓库时，复选框字段必须匹配路由的 repo_full_names 参数。"""
    template = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/star_aid/index.html"
    ).read_text(encoding="utf-8")

    assert 'name="repo_full_names"' in template
    assert 'name="repositories"' not in template


@pytest.mark.asyncio
async def test_admin_member_page_filters_counts_and_clamps_page():
    from types import SimpleNamespace

    member = SimpleNamespace(
        user_id=9, github_username="alice", status="active",
        daily_star_used=2, daily_star_limit=20,
    )

    class Result:
        def __init__(self, total=None, rows=None):
            self.total = total
            self.rows = rows or []

        def scalar_one(self):
            return self.total

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return [Result(total=21), Result(total=43), Result(rows=[member])][
                len(self.statements) - 1
            ]

    session = Session()
    page = await star_aid_service.get_admin_member_page(
        session, member_q="ali%_", member_status="active",
        member_page=99, member_page_size=20,
    )
    assert page["total"] == 21
    assert page["all_total"] == 43
    assert page["page"] == 2 and page["pages"] == 2
    assert page["items"][0]["github_username"] == "alice"
    assert "LIKE" in str(session.statements[0])
    assert "LIMIT" in str(session.statements[2])
    assert "OFFSET" in str(session.statements[2])


def test_star_aid_template_is_parseable():
    from jinja2 import Environment

    template = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/star_aid/index.html"
    ).read_text(encoding="utf-8")
    Environment().parse(template)


def test_admin_page_renders_empty_filtered_member_and_repository_panels():
    from types import SimpleNamespace

    from jinja2 import DictLoader, Environment, StrictUndefined

    template = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/star_aid/index.html"
    ).read_text(encoding="utf-8")
    environment = Environment(
        loader=DictLoader({
            "base.html": "{% block content %}{% endblock %}{% block extra_scripts %}{% endblock %}",
            "star_aid/index.html": template,
        }),
        undefined=StrictUndefined,
        autoescape=True,
    )
    state = {
        "feature_enabled": True,
        "member": None,
        "credential_status": "authorized",
        "daily_star_used": 0,
        "daily_star_limit": 20,
        "available_repos": [],
        "displayed_repos": [],
        "public_repos": [],
        "is_admin": True,
        "admin_members": [],
        "admin_member_page": {
            "items": [], "q": "", "status": "all", "total": 0,
            "all_total": 0, "page": 1, "pages": 1, "page_size": 20,
        },
        "admin_repositories": [],
        "admin_repository_page": {
            "items": [], "q": "", "status": "all", "sort": "stars",
            "order": "desc", "total": 0, "page": 1, "pages": 1,
            "page_size": 20,
        },
    }
    output = environment.get_template("star_aid/index.html").render(
        state=state,
        request=SimpleNamespace(query_params={}),
        csrf_token="test",
        _=lambda key, **_kwargs: key,
    )
    assert 'name="member_status"' in output
    assert 'id="star-aid-admin-repository-results"' in output
    assert 'id="star-aid-admin-repositories"' in output
