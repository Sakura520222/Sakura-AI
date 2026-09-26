"""Navigation and compatibility checks for the WebUI domain migration."""

from pathlib import Path

import pytest
from fastapi import Request
from sqlalchemy import select

from backend.models.database import PRReview
from backend.webui.deps import build_user_scope_filter, get_templates
from backend.webui.routes.logs import log_detail_fragment, logs_list_fragment, logs_page
from backend.webui.routes.pr import apply_review_filters


class SettingsStub:
    payment_enabled = True


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_sidebar_groups_domains_without_leaking_admin_links(role):
    html = (
        get_templates()
        .get_template("components/sidebar.html")
        .render(active_page="pr", current_user={"role": role}, settings=SettingsStub())
    )
    assert html.count("<details") <= 8
    assert 'href="/pr/"' in html
    assert 'href="/logs/"' not in html
    assert 'href="/announcements"' not in html
    assert 'href="/settings/about"' not in html
    assert 'href="/github-app/"' in html
    assert ('href="/logs/actions/"' in html) == (role != "user")
    assert ('href="/config"' in html) == (role == "super_admin")
    assert ('href="/billing/admin/plans"' in html) == (role == "super_admin")


def test_mobile_sidebar_button_opens_sidebar_and_overlay():
    sidebar = (
        Path(__file__).parents[1] / "backend/webui/templates/components/sidebar.html"
    ).read_text(encoding="utf-8")
    assert "getElementById('sidebar-toggle')?.addEventListener('click'" in sidebar
    assert "getElementById('sidebar').classList.toggle('-translate-x-full')" in sidebar
    assert "getElementById('sidebar-overlay').classList.toggle('hidden')" in sidebar


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_contextual_tabs_respect_roles(role):
    template = get_templates().get_template("components/domain_tabs.html")
    html = template.render(
        active_page="settings", current_user={"role": role}, settings=SettingsStub()
    )
    assert ('href="/settings/"' in html) == (role == "super_admin")
    assert ('href="/config/ai"' in html) == (role == "super_admin")


@pytest.mark.asyncio
async def test_legacy_review_urls_preserve_filters_and_point_to_pr():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/logs/",
            "query_string": b"repo=demo&date_from=2026-09-01",
            "headers": [],
        }
    )
    for handler, destination in (
        (logs_page, "/pr/"),
        (logs_list_fragment, "/pr/list-fragment"),
    ):
        response = await handler(request, user={"sub": "alice"})
        assert response.status_code == 307
        assert (
            response.headers["location"]
            == destination + "?repo=demo&date_from=2026-09-01"
        )
    detail = await log_detail_fragment(request, 42, user={"sub": "alice"})
    assert detail.headers["location"] == "/pr/42?repo=demo&date_from=2026-09-01"

    htmx_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/logs/list-fragment",
            "query_string": b"repo=org-a%2Fapi&page=2",
            "headers": [(b"hx-request", b"true")],
        }
    )
    fragment = await logs_list_fragment(htmx_request, user={"sub": "alice"})
    assert fragment.status_code == 200
    assert fragment.headers["HX-Redirect"] == "/pr/?repo=org-a%2Fapi&page=2"
    detail = await log_detail_fragment(htmx_request, 42, user={"sub": "alice"})
    assert detail.headers["HX-Redirect"] == "/pr/42?repo=org-a%2Fapi&page=2"


def test_review_filters_apply_to_list_count_and_export_queries():
    filters = {
        "repo": "org-a/api",
        "search": "fix",
        "status": "completed",
        "decision": "approve",
        "date_from": "2026-09-01",
        "date_to": "2026-09-03",
    }
    scope = build_user_scope_filter({"sub": "alice", "role": "user"}, PRReview)
    assert scope is not None
    for query in (select(PRReview), select(PRReview.id)):
        filtered = apply_review_filters(query.where(scope), **filters)
        sql = str(filtered)
        params = list(filtered.compile().params.values())
        assert "pr_reviews.repo_name" in sql
        assert "pr_reviews.created_at >=" in sql
        assert "pr_reviews.created_at <" in sql
        assert "pr_reviews.repo_owner" in sql and "pr_reviews.author" in sql
        assert "org-a" in params and "api" in params
        assert "completed" in params and "approve" in params
        assert sum(value.__class__.__name__ == "datetime" for value in params) == 2


def test_invalid_date_does_not_remove_other_review_filters():
    query = apply_review_filters(select(PRReview), repo="demo", date_from="invalid")
    sql = str(query)
    assert "pr_reviews.repo_name" in sql
    assert "pr_reviews.created_at" not in sql.split("WHERE", 1)[-1]


def test_repository_selector_distinguishes_same_name_under_different_owners():
    html = (
        get_templates()
        .get_template("components/pr_filters.html")
        .render(
            search="",
            repo="org-b/api",
            status="",
            decision="",
            date_from="",
            date_to="",
            available_repos=[("org-a", "api"), ("org-b", "api")],
        )
    )
    assert 'value="org-a/api"' in html
    assert 'value="org-b/api" selected' in html
