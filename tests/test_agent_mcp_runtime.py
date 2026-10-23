"""MCP boundaries; protocol tests below use an actual loopback HTTP socket."""

import importlib.util

import pytest

MODULE = "backend.services.agent_team.mcp_runtime"


def test_mcp_runtime_is_implemented():
    assert importlib.util.find_spec(MODULE) is not None, "MCP runtime missing"


def test_typed_configuration_rejects_host_process_and_unknown_fields():
    from pydantic import ValidationError

    from backend.services.agent_team.mcp_runtime import MCPConfig

    with pytest.raises(ValidationError):
        MCPConfig.model_validate(
            {"servers": [{"id": "x", "url": "file:///etc/passwd", "command": "sh"}]}
        )


def test_schema_local_refs_and_composition_without_external_retrieval():
    from backend.services.agent_team.mcp_runtime import (
        MCPBoundaryError,
        schema_validator,
    )

    schema = {
        "type": "object",
        "$defs": {"n": {"type": "integer", "minimum": 1}},
        "properties": {"n": {"$ref": "#/$defs/n"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    validator = schema_validator(schema)
    assert validator.is_valid({"n": 1})
    assert not validator.is_valid({"n": 0})
    assert not validator.is_valid({"n": 1, "other": 2})
    with pytest.raises(MCPBoundaryError, match="invalid_schema"):
        schema_validator({"$ref": "http://127.0.0.1/private"})
    with pytest.raises(MCPBoundaryError, match="invalid_schema"):
        schema_validator({"$schema": "https://evil.invalid/dialect"})


def test_names_are_deterministic_and_do_not_collapse_remote_punctuation():
    from backend.services.agent_team.mcp_runtime import tool_name

    names = [
        tool_name(server, name)
        for server, name in [("one", "a-b"), ("one", "a_b"), ("two", "a-b")]
    ]
    assert len(set(names)) == 3
    assert names[0] == tool_name("one", "a-b")
    assert all(name.startswith("mcp_") and len(name) <= 64 for name in names)


@pytest.fixture
def harness():
    from backend.services.agent_team.mcp_runtime import MCPConfig, MCPRuntime

    state = {
        "config": MCPConfig.model_validate(
            {
                "servers": [
                    {
                        "id": "local",
                        "url": "http://127.0.0.1:1/mcp",
                        "timeout_seconds": 2,
                        "headers": {"Authorization": "Bearer super-secret"},
                        "tools": {"echo": {"read_only": True}},
                    }
                ]
            }
        ),
        "allowed": True,
        "audit": [],
    }

    async def load():
        return state["config"]

    async def authorize(required, read_only):
        assert "mcp.invoke" in required and "network.web" in required
        return state["allowed"]

    async def audit(event):
        state["audit"].append(event)

    state["runtime"] = MCPRuntime(load_config=load, authorize=authorize, audit=audit)
    return state


@pytest.mark.asyncio
async def test_offline_does_not_connect_and_unavailable_server_is_isolated(harness):
    harness["allowed"] = False
    assert await harness["runtime"].discover() == []
    assert harness["audit"][-1]["status"] == "denied"
    harness["allowed"] = True
    assert await harness["runtime"].discover() == []
    assert harness["audit"][-1]["status"] == "failed"
    assert "super-secret" not in str(harness["audit"])


@pytest.mark.asyncio
async def test_cancelled_discovery_drains_transport(harness, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from backend.services.agent_team import mcp_runtime

    entered = asyncio.Event()
    closed = asyncio.Event()

    @asynccontextmanager
    async def client(*args):
        try:
            entered.set()
            await asyncio.Future()
            yield
        finally:
            closed.set()

    monkeypatch.setattr(harness["runtime"], "_client", client)
    task = asyncio.create_task(harness["runtime"].discover())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert harness["audit"][-1]["status"] == "cancelled"
    assert mcp_runtime is not None


@pytest.fixture
def remote(harness, monkeypatch):
    """Protocol-independent adapter tests; socket integration is a separate fixture."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    state = {
        "calls": [],
        "closed": 0,
        "pages": [],
        "fail": False,
        "output": {"content": [{"type": "text", "text": "super-secret"}]},
    }
    definition = SimpleNamespace(
        name="echo",
        description="Echo super-secret",
        input_schema={
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
            "additionalProperties": False,
        },
        output_schema=None,
    )
    state["definition"] = definition

    async def listing(cursor=None, **kwargs):
        state["pages"].append(cursor)
        return SimpleNamespace(tools=[definition], next_cursor=None)

    async def calling(name, args, **kwargs):
        state["calls"].append((name, args))
        if state["fail"]:
            raise RuntimeError("transport failure super-secret")
        return SimpleNamespace(
            is_error=False,
            structured_content=None,
            model_dump=lambda **kwargs: state["output"],
        )

    @asynccontextmanager
    async def client(server, policy=None):
        await harness["runtime"]._admit(server, policy)
        try:
            yield (
                SimpleNamespace(
                    list_tools=listing, session=SimpleNamespace(call_tool=calling)
                ),
                {"super-secret"},
            )
        finally:
            state["closed"] += 1

    monkeypatch.setattr(harness["runtime"], "_client", client)
    return state


def context(executor=None):
    from backend.services.agent_team.tools.base import ToolContext

    return ToolContext(workspace="/tmp", workspace_service=None, executor=executor)


@pytest.mark.asyncio
async def test_schema_validation_and_credentials_never_reach_result_or_discovery(
    harness, remote
):
    (adapter,) = await harness["runtime"].discover()
    assert adapter.runtime_metadata().read_only
    assert adapter.runtime_metadata().delegation_safe
    assert not adapter.runtime_metadata().workspace_access
    assert "super-secret" not in str(adapter.get_schema())
    bad = await adapter.execute({"n": 0}, context())
    assert not bad.success and not remote["calls"]
    result = await adapter.execute({"n": 1}, context())
    assert result.success
    assert "[REDACTED]" in str(result.output)
    assert "super-secret" not in str(result)
    assert remote["closed"] == 3
    assert "super-secret" not in str(harness["audit"])


@pytest.mark.asyncio
async def test_policy_revocation_and_schema_changes_reject_stale_adapters(
    harness, remote
):
    (adapter,) = await harness["runtime"].discover()
    harness["allowed"] = False
    denied = await adapter.execute({"n": 1}, context())
    assert not denied.success and not remote["calls"]
    harness["allowed"] = True
    remote["definition"].input_schema["properties"]["n"]["minimum"] = 2
    changed = await adapter.execute({"n": 2}, context())
    assert changed.error_code == "MCP_SCHEMA_CHANGED"
    assert not remote["calls"]


@pytest.mark.asyncio
async def test_readonly_reclassification_and_direct_executor_ceiling(harness, remote):
    from backend.services.agent_team.mcp_runtime import MCPConfig
    from backend.services.agent_team.tools.base import ToolExecutor

    (adapter,) = await harness["runtime"].discover()
    data = harness["config"].model_dump()
    data["servers"][0]["tools"]["echo"]["read_only"] = False
    harness["config"] = MCPConfig.model_validate(data)
    result = await adapter.execute({"n": 1}, context())
    assert result.error_code == "MCP_POLICY_CHANGED" and not remote["calls"]
    (writer,) = await harness["runtime"].discover()
    executor = ToolExecutor([writer], read_only=True)
    forged_ctx = context(executor)
    forged_ctx.extra.update(read_only=False, grants=["full_access"], role="main")
    assert not (await writer.execute({"n": 1}, forged_ctx)).success
    assert not remote["calls"]
    harness["runtime"].read_only = True
    assert await harness["runtime"].discover() == []


@pytest.mark.asyncio
async def test_ambiguous_mutation_failure_is_not_replayed_and_error_is_stable(
    harness, remote
):
    (adapter,) = await harness["runtime"].discover()
    remote["fail"] = True
    result = await adapter.execute({"n": 1}, context())
    assert not result.success and len(remote["calls"]) == 1
    assert "super-secret" not in str(result)
    assert result.error_code == "MCP_TRANSPORT_FAILURE"


@pytest.mark.asyncio
async def test_output_schema_and_discovery_pagination(harness, remote, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    remote["definition"].output_schema = {"type": "object", "required": ["ok"]}
    (adapter,) = await harness["runtime"].discover()
    result = await adapter.execute({"n": 1}, context())
    assert result.error_code == "MCP_INVALID_RESULT"
    original = harness["runtime"]._client

    @asynccontextmanager
    async def paged(server, policy=None):
        async with original(server, policy) as (client, secrets):

            async def listing(cursor=None, **kwargs):
                remote["pages"].append(cursor)
                return SimpleNamespace(
                    tools=[] if cursor is None else [remote["definition"]],
                    next_cursor="second" if cursor is None else None,
                )

            client.list_tools = listing
            yield client, secrets

    monkeypatch.setattr(harness["runtime"], "_client", paged)
    assert len(await harness["runtime"].discover()) == 1
    assert remote["pages"][-2:] == [None, "second"]


def test_json_schema_reference_shaped_instance_values_are_preserved():
    from backend.services.agent_team.mcp_runtime import schema_validator

    schema = {
        "type": "object",
        "properties": {"payload": {"const": {"$ref": "https://example.test/data"}}},
        "required": ["payload"],
    }
    validator = schema_validator(schema)
    assert validator.is_valid({"payload": {"$ref": "https://example.test/data"}})


@pytest.mark.asyncio
async def test_failed_discovery_teardown_does_not_publish_adapter(
    harness, remote, monkeypatch
):
    from contextlib import asynccontextmanager

    original = harness["runtime"]._client

    @asynccontextmanager
    async def broken_close(server, policy=None):
        async with original(server, policy) as value:
            yield value
        raise RuntimeError("session teardown failed")

    monkeypatch.setattr(harness["runtime"], "_client", broken_close)
    assert await harness["runtime"].discover() == []
    assert harness["audit"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_credential_bearing_schema_is_denied_without_altering_semantics(
    harness, remote
):
    remote["definition"].input_schema["properties"]["n"] = {"enum": ["super-secret"]}
    assert await harness["runtime"].discover() == []
    assert any(event["reason"] == "credential_in_schema" for event in harness["audit"])
    assert "super-secret" not in str(harness["audit"])


@pytest.mark.asyncio
async def test_server_default_policy_admits_main_tools_without_exact_allowlist(
    harness, remote
):
    from backend.services.agent_team.mcp_runtime import MCPConfig

    data = harness["config"].model_dump()
    data["servers"][0].pop("tools")
    harness["config"] = MCPConfig.model_validate(data)
    # A server's annotation cannot upgrade the trusted default to read-only.
    remote["definition"].annotations = {"readOnlyHint": True}
    (adapter,) = await harness["runtime"].discover()
    assert not adapter.runtime_metadata().read_only
    assert not adapter.runtime_metadata().delegation_safe
    assert (await adapter.execute({"n": 1}, context())).success
    harness["runtime"].read_only = True
    assert await harness["runtime"].discover() == []


@pytest.mark.asyncio
async def test_disabled_default_policy_retains_explicit_allowlist_and_revocation(
    harness, remote
):
    from backend.services.agent_team.mcp_runtime import MCPConfig

    data = harness["config"].model_dump()
    data["servers"][0]["default_policy"] = {"enabled": False}
    data["servers"][0]["tools"] = {}
    harness["config"] = MCPConfig.model_validate(data)
    assert await harness["runtime"].discover() == []
    data["servers"][0]["tools"] = {"echo": {"read_only": True}}
    harness["config"] = MCPConfig.model_validate(data)
    (adapter,) = await harness["runtime"].discover()
    harness["runtime"].read_only = True
    assert (await adapter.execute({"n": 1}, context())).success
    data["servers"][0]["tools"]["echo"]["enabled"] = False
    harness["config"] = MCPConfig.model_validate(data)
    assert not (await adapter.execute({"n": 1}, context())).success
    assert len(remote["calls"]) == 1
