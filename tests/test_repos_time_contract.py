"""Repository WebUI regression tests for aware activity instants."""

from datetime import UTC, datetime

import pytest

from backend.webui.routes import repos


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StatsDb:
    def __init__(self, results):
        self._results = iter(results)

    async def execute(self, _query):
        return _Rows(next(self._results))


@pytest.mark.asyncio
@pytest.mark.parametrize("activity_kind", ["pr", "issue"])
async def test_repository_last_activity_handles_one_aware_source(
    monkeypatch,
    activity_kind,
):
    instant = datetime(2026, 8, 13, 8, 30, tzinfo=UTC)
    data = [{"repos": [{"full_name": "owner/repo"}]}]
    monkeypatch.setattr(repos, "monotonic", lambda: 100.0)
    monkeypatch.setattr(repos, "_installations_cache", (data, 100.0))

    pr_counts = [("owner", "repo", 1)] if activity_kind == "pr" else []
    issue_counts = [("owner", "repo", 1)] if activity_kind == "issue" else []
    last_prs = [("owner", "repo", instant)] if activity_kind == "pr" else []
    last_issues = [("owner", "repo", instant)] if activity_kind == "issue" else []
    db = _StatsDb((pr_counts, issue_counts, last_prs, last_issues))

    result = await repos._get_installations_with_stats(db)

    assert result[0]["repos"][0]["last_activity"] == instant
    assert result[0]["repos"][0]["last_activity"].tzinfo is UTC
