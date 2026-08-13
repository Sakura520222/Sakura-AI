"""Regression tests for dashboard application-calendar aggregation."""

import json
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

    async def execute(self, _query):
        self._calls += 1
        if self._calls == 1:
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
    monkeypatch.setattr(
        api_dashboard,
        "fetch_review_trend",
        lambda *args, **kwargs: _review_trend(args[2]),
    )
    monkeypatch.setattr(
        web_dashboard,
        "fetch_review_trend",
        lambda *args, **kwargs: _review_trend(args[2]),
    )
    api_dashboard._chart_cache.clear()
    web_dashboard._chart_cache.clear()


async def _empty_token_trend():
    return [0] * 31


async def _review_trend(labels):
    completed = [0] * len(labels)
    failed = [0] * len(labels)
    index = labels.index("08-10")
    completed[index] = 2
    failed[index] = 1
    return completed, failed


@pytest.mark.asyncio
async def test_api_dashboard_uses_aggregated_review_trend(frozen_dashboard):
    response = await api_dashboard.get_chart_data(
        _ChartDb(), {"role": "super_admin", "user_id": 1, "sub": "admin"}
    )
    payload = json.loads(response.body)
    labels = payload["data"]["trend"]["labels"]
    index = labels.index("08-10")
    assert payload["data"]["trend"]["completed"][index] == 2
    assert payload["data"]["trend"]["failed"][index] == 1


@pytest.mark.asyncio
async def test_webui_dashboard_uses_aggregated_review_trend(frozen_dashboard):
    result = await web_dashboard.get_chart_data(
        _ChartDb(), {"role": "super_admin", "user_id": 1, "sub": "admin"}
    )
    labels = result["trend"]["labels"]
    index = labels.index("08-10")
    assert result["trend"]["completed"][index] == 2
    assert result["trend"]["failed"][index] == 1
