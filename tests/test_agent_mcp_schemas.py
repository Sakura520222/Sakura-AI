"""Remote schemas retain their meaning through discovery and model advertisement."""

from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team.mcp_runtime import schema_validator
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.tools import registry
from backend.services.agent_team.tools.base import ToolContext
from tests.test_agent_mcp_http import endpoint, runtime_for

SCHEMAS = [
    pytest.param(
        {
            "type": "object",
            "properties": {"payload": True, "forbidden": False},
            "required": [],
            "additionalProperties": False,
        },
        [
            ({}, True),
            ({"payload": {"any": [1, None]}}, True),
            ({"forbidden": 1}, False),
            ({"extra": 1}, False),
        ],
        id="boolean-properties",
    ),
    pytest.param(
        {
            "type": "object",
            "$defs": {"identifier": {"type": "string", "minLength": 1}},
            "properties": {
                "left": {"$ref": "#/$defs/identifier"},
                "right": {"$ref": "#/$defs/identifier"},
                "limit": {"type": "integer", "minimum": 1, "default": 5},
            },
            "oneOf": [{"required": ["left"]}, {"required": ["right"]}],
            "additionalProperties": {"type": "string"},
        },
        [
            ({"left": "a"}, True),
            ({"right": "b", "dynamic": "value"}, True),
            ({"left": "a", "right": "b"}, False),
            ({}, False),
            ({"left": "a", "dynamic": 3}, False),
        ],
        id="optional-composition-map",
    ),
    pytest.param(
        {
            "type": "object",
            "properties": {
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                }
            },
            "additionalProperties": True,
        },
        [
            ({}, True),
            ({"labels": {"dynamic": 3}, "extension": True}, True),
            ({"labels": {"dynamic": "bad"}}, False),
        ],
        id="nested-dynamic-properties",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
@pytest.mark.parametrize("provider", [None, "openai", "glm", "zai"])
@pytest.mark.parametrize("remote_schema, examples", SCHEMAS)
async def test_fresh_definitions_preserve_remote_schema_semantics(
    legacy, provider, remote_schema, examples, monkeypatch
):
    monkeypatch.setattr(
        registry,
        "get_agent_team_network_policy",
        AsyncMock(return_value=AgentTeamNetworkPolicy.WEB_TOOLS),
    )
    monkeypatch.setattr(registry, "skills_enabled", AsyncMock(return_value=True))
    async with endpoint(legacy=legacy) as server:
        state = runtime_for(server)

        def override(payload):
            if payload.get("method") == "tools/list":
                return {
                    "result": {
                        "resultType": "complete",
                        "ttlMs": 0,
                        "cacheScope": "private",
                        "tools": [{"name": "echo", "inputSchema": remote_schema}],
                    }
                }

        server["override"] = override
        executor = registry.create_executor()
        ctx = ToolContext(
            workspace="/tmp",
            workspace_service=None,
            executor=executor,
            mcp_runtime=state["runtime"],
        )
        try:
            definitions = await registry.get_tool_definitions_fresh(
                provider=provider, ctx=ctx
            )
            by_name = {item["function"]["name"]: item for item in definitions}
            adapter = next(
                tool
                for tool in executor.all_tools()
                if getattr(tool, "remote_name", None) == "echo"
            )
            assert (
                "read_file" in by_name
            )  # A remote schema cannot abort built-in advertisement.
            advertised = by_name[adapter.name]["function"]["parameters"]
            for args, allowed in examples:
                assert schema_validator(advertised).is_valid(args) is allowed
                assert (adapter.validate_input(args, ctx) is None) is allowed
            assert advertised == remote_schema
            # Provider conversion cannot mutate the execution schema or fixture.
            assert adapter.get_schema()["function"]["parameters"] == remote_schema
            assert state["audit"][-1]["status"] == "completed"
        finally:
            await state["runtime"].close()
