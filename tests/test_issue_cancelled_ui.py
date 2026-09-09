import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from backend.webui.i18n import make_translation_func

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "backend" / "webui" / "templates"


@pytest.fixture(scope="module")
def template_env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_ROOT)), autoescape=True)
    env.globals["_"] = make_translation_func("en")
    env.globals["settings"] = SimpleNamespace(payment_enabled=False)
    return env


def _analysis(status):
    return SimpleNamespace(
        id=1,
        issue_number=536,
        title="Status rendering",
        repo_owner="owner",
        repo_name="repo",
        author=None,
        created_at=None,
        category=None,
        priority=None,
        status=status,
        summary=None,
        feasibility=None,
        prompt_tokens=0,
        completion_tokens=0,
        estimated_cost=0,
        duplicate_of=None,
        error_message=None,
    )


def _render_issue_list(template_env, status):
    analysis = _analysis(status)
    return template_env.get_template("components/issue_list_fragment.html").render(
        analyses=[analysis],
        search="",
        repo_name="",
        category="",
        priority="",
        status="",
        total=1,
        page=1,
        total_pages=1,
    )


def _render_detail(template_env, status):
    return template_env.get_template("issue_detail.html").render(
        analysis=_analysis(status),
        suggested_labels=[],
        suggested_assignees=[],
        related_prs=[],
        current_user={"role": "admin", "sub": "tester"},
        user_prefs={"language": "en"},
        lang="en",
        app_timezone="UTC",
        csrf_token="",
    )


def _render_detail_fragment(template_env, status):
    return template_env.get_template(
        "components/issue_detail_fragment.html"
    ).render(analysis=_analysis(status))


def _status_block(html, end_marker):
    block = html.split("<!-- 状态 -->", 1)[1].split(end_marker, 1)[0]
    return re.sub(r"<[^>]+>", " ", block)


def _detail_status_block(html):
    block = html.split("<!-- 标题区 -->", 1)[1].split("<!-- AI 分析结果 -->", 1)[0]
    return re.sub(r"<[^>]+>", " ", block)


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("pending", "Pending"),
        ("cancelled", "Cancelled"),
        ("awaiting_review", "awaiting_review"),
        (None, "Unknown"),
    ),
)
def test_issue_statuses_render_pending_cancelled_and_unknown_distinctly(
    template_env, status, expected
):
    list_html = _render_issue_list(template_env, status)
    detail_html = _render_detail(template_env, status)
    fragment_html = _render_detail_fragment(template_env, status)

    list_status = _status_block(list_html, "<!-- Issue 信息 -->")
    detail_status = _detail_status_block(detail_html)
    fragment_status = re.search(
        r'<span class="px-2\.5 py-1 rounded-full text-xs font-medium[^>]*>(.*?)</span>',
        fragment_html,
        re.DOTALL,
    )

    assert fragment_status is not None
    fragment_text = re.sub(r"<[^>]+>", " ", fragment_status.group(1))
    for rendered_status in (list_status, detail_status, fragment_text):
        assert expected in rendered_status
        if status == "awaiting_review":
            assert "Pending" not in rendered_status


def test_issue_filters_keep_cancelled_option_and_selection(template_env):
    html = template_env.get_template("components/issue_filters.html").render(
        search="",
        category="",
        priority="",
        status="cancelled",
    )

    assert re.search(r'<option value="cancelled"\s+selected>', html)
    assert "Cancelled" in html
