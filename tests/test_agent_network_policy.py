from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.agent_team import network_policy
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    LocalExecutionRunner,
)
from backend.services.agent_team.tools import fetch_url_tool, web_search_tool
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _tool_context() -> ToolContext:
    return ToolContext(
        workspace=".",
        workspace_service=AgentTeamWorkspaceService(),
    )


@pytest.mark.asyncio
async def test_web_search_policy_is_read_for_each_long_lived_call(monkeypatch):
    policies = iter(
        [
            network_policy.AgentTeamNetworkPolicy.WEB_TOOLS,
            network_policy.AgentTeamNetworkPolicy.FULL_ACCESS,
            network_policy.AgentTeamNetworkPolicy.OFFLINE,
        ]
    )
    calls: list[str] = []

    class Handler:
        async def search_web(self, *, query, top_k=None):
            calls.append(query)
            return {"query": query, "results": [], "count": 0}

    async def read_policy():
        return next(policies)

    async def read_switch(_key: str):
        return True

    monkeypatch.setattr(web_search_tool, "get_agent_team_network_policy", read_policy)
    monkeypatch.setattr(web_search_tool, "get_agent_tool_switch", read_switch)
    monkeypatch.setattr(web_search_tool, "_web_search_handler", Handler())

    tool = web_search_tool.WebSearchTool()
    ctx = _tool_context()
    first = await tool.execute({"query": "one"}, ctx)
    second = await tool.execute({"query": "two"}, ctx)
    denied = await tool.execute({"query": "three"}, ctx)

    assert first.success and second.success
    assert not denied.success
    assert "offline" in denied.error
    assert calls == ["one", "two"]


@pytest.mark.asyncio
async def test_web_search_switch_disable_then_enable_recovers_without_restart(monkeypatch):
    switches = iter([False, True])

    class Handler:
        async def search_web(self, *, query, top_k=None):
            return {"query": query, "results": [], "count": 0}

    async def read_policy():
        return network_policy.AgentTeamNetworkPolicy.WEB_TOOLS

    async def read_switch(_key: str):
        return next(switches)

    monkeypatch.setattr(web_search_tool, "get_agent_team_network_policy", read_policy)
    monkeypatch.setattr(web_search_tool, "get_agent_tool_switch", read_switch)
    monkeypatch.setattr(web_search_tool, "_web_search_handler", Handler())

    tool = web_search_tool.WebSearchTool()
    ctx = _tool_context()
    disabled = await tool.execute({"query": "first"}, ctx)
    enabled = await tool.execute({"query": "second"}, ctx)

    assert not disabled.success
    assert "未启用" in disabled.error
    assert enabled.success


@pytest.mark.asyncio
async def test_fetch_url_policy_and_switch_are_fresh(monkeypatch):
    policies = iter(
        [
            network_policy.AgentTeamNetworkPolicy.WEB_TOOLS,
            network_policy.AgentTeamNetworkPolicy.OFFLINE,
        ]
    )

    class Handler:
        async def fetch_url(self, *, url):
            return {"url": url, "content": "ok", "content_length": 2}

    async def read_policy():
        return next(policies)

    async def read_switch(_key: str):
        return True

    monkeypatch.setattr(fetch_url_tool, "get_agent_team_network_policy", read_policy)
    monkeypatch.setattr(fetch_url_tool, "get_agent_tool_switch", read_switch)
    monkeypatch.setattr(fetch_url_tool, "_fetch_url_handler", Handler())

    tool = fetch_url_tool.FetchUrlTool()
    ctx = _tool_context()
    allowed = await tool.execute({"url": "https://example.com"}, ctx)
    denied = await tool.execute({"url": "https://example.com"}, ctx)

    assert allowed.success
    assert not denied.success
    assert "offline" in denied.error


@pytest.mark.asyncio
async def test_local_backend_fails_closed_for_offline_policy(tmp_path, monkeypatch):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")

    async def read_policy():
        return network_policy.AgentTeamNetworkPolicy.OFFLINE

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        read_policy,
    )
    runner = LocalExecutionRunner(workspace, service)
    request = ExecutionRequest(
        workspace_key="owner-repo",
        command="echo should-not-run",
        profile=ExecutionProfile.AGENT,
        timeout_seconds=1,
    )

    with pytest.raises(ExecutionError, match="offline"):
        await runner.execute(request)


@pytest.mark.asyncio
async def test_parameter_handlers_reload_database_values_without_ttl(monkeypatch):
    from backend.models import database
    from backend.services.ai_reviewer.tools.fetch_url_tool import FetchUrlToolHandler
    from backend.services.ai_reviewer.tools.web_search_tool import WebSearchToolHandler

    settings = SimpleNamespace(
        web_search_provider="duckduckgo",
        web_search_api_key="env-key",
        web_search_max_results=5,
        web_search_max_content_length=2000,
        web_search_timeout=30,
        fetch_url_timeout=15,
        fetch_url_max_content_length=5000,
        fetch_url_max_download_size=1024,
        fetch_url_domain_policy="off",
        fetch_url_domain_list="",
        fetch_url_force_https=False,
        fetch_url_allowed_content_types="text/html",
        fetch_url_max_redirects=5,
    )
    rows: list[SimpleNamespace] = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return Result()

    def session_factory():
        return Session()

    monkeypatch.setattr(database, "async_session", session_factory)
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.web_search_tool.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.fetch_url_tool.get_settings",
        lambda: settings,
    )
    web = WebSearchToolHandler()
    fetch = FetchUrlToolHandler()

    rows[:] = [
        SimpleNamespace(key_name="web_search_max_results", key_value="9"),
        SimpleNamespace(key_name="fetch_url_max_redirects", key_value="12"),
    ]
    await web._load_config()
    await fetch._load_config()
    assert web.max_results == 9
    assert fetch._max_redirects == 12

    rows[:] = [
        SimpleNamespace(key_name="web_search_max_results", key_value="3"),
        SimpleNamespace(key_name="fetch_url_max_redirects", key_value="2"),
    ]
    await web._load_config()
    await fetch._load_config()
    assert web.max_results == 3
    assert fetch._max_redirects == 2
