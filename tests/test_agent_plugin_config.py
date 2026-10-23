import json
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team import plugin_config as plugins


def server(**kwargs):
    return {"id": "docs", "url": "https://example.test/mcp", **kwargs}


def payload(**kwargs):
    return {"version": 1, "mcp": {"servers": [server()]}, **kwargs}


def test_defaults_roundtrip_and_mask():
    config = plugins.parse_harness_plugins(payload())
    assert config.mcp.servers[0].timeout_seconds == 300
    config = plugins.parse_harness_plugins(
        payload(mcp={"servers": [server(headers={"Authorization": "fixture-secret"})]})
    )
    public = plugins.public_plugin_config(config)
    assert "fixture-secret" not in json.dumps(public)
    assert plugins.parse_harness_plugins(public, previous=config) == config


@pytest.mark.parametrize(
    "value",
    [
        payload(version=2),
        payload(version=True),
        payload(mcp={"servers": [server(), server()]}),
        payload(hooks=[{"id": "h", "event": "unknown", "kind": "audit"}]),
        payload(
            hooks=[
                {"id": "h", "event": "before_tool", "argv": ["echo"], "read_only": True}
            ]
        ),
        payload(
            mcp={
                "servers": [
                    server(credential_headers={"Authorization": "DATABASE_URL"})
                ]
            }
        ),
        payload(
            mcp={
                "servers": [
                    server(default_policy={"required_capabilities": ["unknown"]})
                ]
            }
        ),
        payload(mcp_repository_scopes={"missing": ["owner/repo"]}),
    ],
)
def test_invalid_configuration_has_safe_error(value):
    with pytest.raises(plugins.PluginConfigError, match="invalid_plugin_configuration"):
        plugins.parse_harness_plugins(value)


@pytest.mark.asyncio
async def test_credentials_are_namespace_restricted(monkeypatch):
    monkeypatch.setenv("SAKURA_MCP_TEST_FIXTURE", "fake-token")
    assert (
        await plugins.resolve_mcp_credential("SAKURA_MCP_TEST_FIXTURE") == "fake-token"
    )
    with pytest.raises(plugins.PluginConfigError):
        await plugins.resolve_mcp_credential("DATABASE_URL")


def test_mask_cannot_move_to_new_endpoint_or_server():
    config = plugins.parse_harness_plugins(
        payload(mcp={"servers": [server(headers={"Authorization": "fixture-secret"})]})
    )
    public = plugins.public_plugin_config(config)
    public["mcp"]["servers"][0]["url"] = "https://other.test/mcp"
    with pytest.raises(plugins.PluginConfigError):
        plugins.parse_harness_plugins(public, previous=config)


def test_empty_defaults_and_explicit_timeout():
    from backend.core.config import Settings

    settings = Settings.model_construct()
    config = plugins.parse_harness_plugins(settings.agent_team_harness_plugins)
    assert config == plugins.HarnessPluginConfig()
    assert settings.agent_team_permission_profile == "autonomous"
    assert (
        plugins.parse_harness_plugins(
            payload(mcp={"servers": [server(timeout_seconds=12.5)]})
        )
        .mcp.servers[0]
        .timeout_seconds
        == 12.5
    )


@pytest.mark.asyncio
async def test_scope_uses_trusted_task_and_no_task_is_unscoped(monkeypatch):
    value = payload(mcp_repository_scopes={"docs": ["owner/repo"]})
    monkeypatch.setattr(
        plugins, "get_dynamic_config_fresh", AsyncMock(return_value=json.dumps(value))
    )
    monkeypatch.setattr(
        plugins, "_task_repository", AsyncMock(return_value="owner/other")
    )
    assert not (await plugins.load_harness_plugins()).mcp.servers
    assert not (await plugins.load_harness_plugins(task_id=1)).mcp.servers
    plugins._task_repository.return_value = "owner/repo"
    assert (await plugins.load_harness_plugins(task_id=1)).mcp.servers
    plugins._task_repository.return_value = None
    with pytest.raises(plugins.PluginConfigError):
        await plugins.load_harness_plugins(task_id=99)
