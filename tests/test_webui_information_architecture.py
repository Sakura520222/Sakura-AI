"""Review filter regression tests."""

from sqlalchemy import select

from backend.api.v1.reviews import apply_review_filters
from backend.models.database import PRReview
from backend.webui.deps import build_user_scope_filter


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
