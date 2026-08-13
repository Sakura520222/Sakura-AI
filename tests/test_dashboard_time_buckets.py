"""Regression tests for dashboard application-calendar row bucketing."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.api.v1 import dashboard as api_dashboard
from backend.core.time_service import TimeService
from backend.webui.routes import dashboard as web_dashboard


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ChartDb:
    def __init__(self):
        self._calls = 0
        self.trend_rows = [
            SimpleNamespace(
                created_at=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
                status="completed",
            ),
            SimpleNamespace(
                created_at=datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
                status="completed",
            ),
            SimpleNamespace(
                created_at=datetime(2026, 8, 10, 3, 0, tzinfo=UTC),
                status="failed",
            ),
        ]

    async def execute(self, _query):
        self._calls += 1
        if self._calls == 1:
            return _Rows(self.trend_rows)
        if self._calls == 2:
            return _Rows([SimpleNamespace(decision="approve", cnt=3)])
        return _Rows([SimpleNamespace(repo_name="owner/repo", cnt=3)])


@pytest.fixture
def frozen_dashboard(monkeypatch):
    service = TimeService("UTC")
    monkeypatch.setattr(api_dashboard, "get_time_service", lambda: service)
    monkeypatch.setattr(web_dashboard, "get_time_service", lambda: service)
    monkeypatch.setattr(
        api_dashboard,
        "fetch_token_trend",
        lambda *args, **kwargs: _empty_token_trend(),
    )
    monkeypatch.setattr(
        web_dashboard,
        "fetch_token_trend",
        lambda *args, **kwargs: _empty_token_trend(),
    )
    api_dashboard._chart_cache.clear()
    web_dashboard._chart_cache.clear()


async def _empty_token_trend():
    return [0] * 31


@pytest.mark.asyncio
async def test_api_dashboard_counts_each_actual_trend_row(frozen_dashboard):
    response = await api_dashboard.get_chart_data(
        _ChartDb(), {"role": "super_admin", "user_id": 1, "sub": "admin"}
    )
    payload = json.loads(response.body)
    labels = payload["data"]["trend"]["labels"]
    index = labels.index("08-10")
    assert payload["data"]["trend"]["completed"][index] == 2
    assert payload["data"]["trend"]["failed"][index] == 1


@pytest.mark.asyncio
async def test_webui_dashboard_counts_each_actual_trend_row(frozen_dashboard):
    result = await web_dashboard.get_chart_data(
        _ChartDb(), {"role": "super_admin", "user_id": 1, "sub": "admin"}
    )
    labels = result["trend"]["labels"]
    index = labels.index("08-10")
    assert result["trend"]["completed"][index] == 2
    assert result["trend"]["failed"][index] == 1
