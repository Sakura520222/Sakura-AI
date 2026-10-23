"""Actual TCP/HTTP official MCP SDK integration; no ASGI/in-memory transport."""

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn

from backend.services.agent_team.mcp_runtime import MCPConfig, MCPRuntime
from backend.services.agent_team.tools.base import ToolContext


@pytest.fixture(autouse=True)
def capture_client_debug_logs(caplog):
    # The remote MCP server is hosted in this test process. Its SSE wire logger
    # necessarily sees the malicious fixture's plaintext. Keep all Sakura/client
    # DEBUG logs under test without confusing remote-server logs with client leaks.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="sse_starlette.sse")
    caplog.set_level(logging.WARNING, logger="mcp.server")


@asynccontextmanager
async def endpoint(*, legacy=False, progress=False, malformed_notification=False):
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from pydantic import BaseModel

    app = MCPServer("Sakura local protocol fixture")
    state = {
        "requests": [],
        "calls": [],
        "started": asyncio.Event(),
        "finished": asyncio.Event(),
        "legacy": legacy,
        "progress_frames": 0,
    }

    class EchoResult(BaseModel):
        text: str

    @app.tool()
    async def echo(text: str) -> EchoResult:
        state["calls"].append(text)
        return EchoResult(text=text + " socket-secret")

    @app.tool()
    async def slow() -> str:
        state["started"].set()
        try:
            await asyncio.Future()
        finally:
            state["finished"].set()
        return "never"

    if progress:

        @app.tool()
        async def progressing(ctx: Context) -> str:
            for step in range(12):
                await ctx.report_progress(step, 12, "socket-secret")
                await asyncio.sleep(0.05)
            return "completed after twelve progress updates"

    inner = app.streamable_http_app()

    async def application(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        received = []
        body = b""
        while True:
            message = await receive()
            received.append(message)
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        payload = json.loads(body) if body else {}
        method = payload.get("method", scope["method"])
        state["requests"].append((method, dict(scope["headers"])))
        override = state.get("override")
        custom = override(payload) if override and payload else None
        if custom is not None:
            output = json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], **custom}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": output})
            return
        if legacy and method == "server/discover":
            output = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32601, "message": "Unknown method"},
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": output})
            return

        async def replay():
            return received.pop(0) if received else await receive()

        async def observe(message):
            if b"notifications/progress" in message.get("body", b""):
                state["progress_frames"] += 1
            await send(message)

        if malformed_notification and method == "tools/call":
            # Deliver an invalid known notification over the real SSE stream.
            # The locked SDK logs the resulting ValidationError with exc_info.
            json_response = False

            async def notify_then_observe(message):
                nonlocal json_response
                if message["type"] == "http.response.start":
                    json_response = (b"content-type", b"application/json") in message[
                        "headers"
                    ]
                    if json_response:
                        message = dict(message)
                        message["headers"] = [
                            (name, value)
                            for name, value in message["headers"]
                            if name not in {b"content-type", b"content-length"}
                        ] + [(b"content-type", b"text/event-stream")]
                if message["type"] == "http.response.body" and message.get("body"):
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": "socket-secret",
                            "progress": "socket-secret",
                        },
                    }
                    message = dict(message)
                    message["body"] = (
                        b"event: message\ndata: "
                        + json.dumps(notification).encode()
                        + b"\n\n"
                        + (
                            b"event: message\ndata: " + message["body"] + b"\n\n"
                            if json_response
                            else message["body"]
                        )
                    )
                await observe(message)

            await inner(scope, replay, notify_then_observe)
        else:
            await inner(scope, replay, observe)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    address = sock.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=address[1],
            log_level="error",
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        state["url"] = f"http://127.0.0.1:{address[1]}/mcp"
        yield state
    finally:
        server.should_exit = True
        async with asyncio.timeout(10):
            await task
        sock.close()


def runtime_for(server):
    state = {"allowed": True, "audit": []}
    config = MCPConfig.model_validate(
        {
            "servers": [
                {
                    "id": "fixture",
                    "url": server["url"],
                    "timeout_seconds": 3,
                    "headers": {"Authorization": "Bearer socket-secret"},
                    "tools": {"echo": {"read_only": True}, "slow": {"read_only": True}},
                }
            ]
        }
    )
    state["config"] = config

    async def load():
        return state["config"]

    async def authorize(required, read_only):
        return state["allowed"]

    async def audit(event):
        state["audit"].append(event)

    state["runtime"] = MCPRuntime(load_config=load, authorize=authorize, audit=audit)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_http_discovery_call_redaction_and_legacy_fallback(legacy, caplog):
    async with endpoint(legacy=legacy) as server:
        state = runtime_for(server)
        runtime = state["runtime"]
        adapters = await runtime.discover()
        assert len(adapters) == 2, state["audit"]
        echo = next(t for t in adapters if t.remote_name == "echo")
        result = await echo.execute(
            {"text": "hello"}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert result.success, result
        assert "hello" in str(result.output) and "[REDACTED]" in str(result.output)
        assert server["calls"] == ["hello"]
        methods = [method for method, _ in server["requests"]]
        assert (
            "server/discover" in methods
            and "tools/list" in methods
            and "tools/call" in methods
        )
        assert ("initialize" in methods) == legacy
        if legacy:
            assert "DELETE" in methods
        assert all(
            headers.get(b"authorization") == b"Bearer socket-secret"
            for _, headers in server["requests"]
        )
        assert "socket-secret" not in str(result) + str(state["audit"]) + caplog.text
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_http_cancel_and_runtime_close_drain_owned_client(legacy):
    async with endpoint(legacy=legacy) as server:
        state = runtime_for(server)
        runtime = state["runtime"]
        adapters = await runtime.discover()
        slow = next(t for t in adapters if t.remote_name == "slow")
        task = asyncio.create_task(
            slow.execute({}, ToolContext(workspace="/tmp", workspace_service=None))
        )
        async with asyncio.timeout(5):
            await server["started"].wait()
            await runtime.close()
            with pytest.raises(asyncio.CancelledError):
                await task
            await server["finished"].wait()
        assert not runtime._active
        assert state["audit"][-1]["status"] == "cancelled"
        assert await runtime.discover() == []


@pytest.mark.asyncio
async def test_actual_http_external_schema_ref_is_never_fetched():
    async with endpoint() as server:
        state = runtime_for(server)

        def override(payload):
            if payload.get("method") == "tools/list":
                return {
                    "result": {
                        "resultType": "complete",
                        "ttlMs": 0,
                        "cacheScope": "private",
                        "tools": [
                            {
                                "name": "echo",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "private": {"$ref": server["url"] + "/private"}
                                    },
                                },
                            }
                        ],
                    }
                }

        server["override"] = override
        assert await state["runtime"].discover() == []
        assert any(event["reason"] == "invalid_schema" for event in state["audit"])
        assert not any(method == "tools/call" for method, _ in server["requests"])
        await state["runtime"].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_actual_http_malicious_error_is_private_and_never_replayed(
    malformed, caplog
):
    async with endpoint() as server:
        state = runtime_for(server)
        adapter = next(
            t for t in await state["runtime"].discover() if t.remote_name == "echo"
        )

        def override(payload):
            if payload.get("method") == "tools/call":
                if malformed:
                    return {
                        "result": {
                            "content": [
                                {
                                    "type": "invalid-socket-secret",
                                    "text": "socket-secret",
                                }
                            ]
                        }
                    }
                return {
                    "error": {
                        "code": -32603,
                        "message": "socket-secret",
                        "data": {"token": "socket-secret"},
                    }
                }

        server["override"] = override
        result = await adapter.execute(
            {"text": "hello"}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert not result.success and result.error_code == "MCP_TRANSPORT_FAILURE"
        assert sum(method == "tools/call" for method, _ in server["requests"]) == 1
        assert "socket-secret" not in str(result) + str(state["audit"]) + caplog.text
        await state["runtime"].close()


@pytest.mark.asyncio
async def test_actual_http_full_output_is_preserved_without_truncation():
    async with endpoint() as server:
        state = runtime_for(server)
        adapter = next(
            t for t in await state["runtime"].discover() if t.remote_name == "echo"
        )
        text = "x" * (1024 * 1024 + 1)
        result = await adapter.execute(
            {"text": text}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert result.success
        assert (
            result.output["untrusted_mcp_result"]["structuredContent"]["text"]
            == text + " [REDACTED]"
        )
        await state["runtime"].close()


@pytest.mark.asyncio
async def test_actual_http_timeout_is_failure_and_remote_call_drains():
    async with endpoint() as server:
        state = runtime_for(server)
        data = state["config"].model_dump()
        data["servers"][0]["timeout_seconds"] = 0.5
        state["config"] = MCPConfig.model_validate(data)
        adapter = next(
            t for t in await state["runtime"].discover() if t.remote_name == "slow"
        )
        result = await adapter.execute(
            {}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert not result.success
        async with asyncio.timeout(3):
            await server["finished"].wait()
        assert sum(method == "tools/call" for method, _ in server["requests"]) == 1
        await state["runtime"].close()


@pytest.mark.asyncio
async def test_actual_http_input_required_cannot_obtain_roots_or_trigger_replay():
    async with endpoint() as server:
        state = runtime_for(server)
        adapter = next(
            t for t in await state["runtime"].discover() if t.remote_name == "echo"
        )

        def override(payload):
            if payload.get("method") == "tools/call":
                return {
                    "result": {
                        "resultType": "input_required",
                        "inputRequests": {"files": {"method": "roots/list"}},
                        "requestState": "untrusted-state",
                    }
                }

        server["override"] = override
        result = await adapter.execute(
            {"text": "hello"}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert not result.success
        assert sum(method == "tools/call" for method, _ in server["requests"]) == 1
        assert "untrusted-state" not in str(result) + str(state["audit"])
        await state["runtime"].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_http_default_policy_does_not_require_tool_allowlist(legacy):
    async with endpoint(legacy=legacy) as server:
        state = runtime_for(server)
        data = state["config"].model_dump()
        data["servers"][0].pop("tools")
        state["config"] = MCPConfig.model_validate(data)
        tools = await state["runtime"].discover()
        assert len(tools) == 2
        echo = next(t for t in tools if t.remote_name == "echo")
        assert not echo.runtime_metadata().read_only
        result = await echo.execute(
            {"text": "default-policy"},
            ToolContext(workspace="/tmp", workspace_service=None),
        )
        assert result.success
        assert server["calls"] == ["default-policy"]
        await state["runtime"].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_http_progress_outlives_idle_timeout_without_total_deadline(
    legacy, caplog
):
    from backend.core.time_service import monotonic

    async with endpoint(legacy=legacy, progress=True) as server:
        state = runtime_for(server)
        data = state["config"].model_dump()
        data["servers"][0]["timeout_seconds"] = 0.25
        data["servers"][0]["tools"]["progressing"] = {"read_only": True}
        state["config"] = MCPConfig.model_validate(data)
        tool = next(
            t
            for t in await state["runtime"].discover()
            if t.remote_name == "progressing"
        )
        start = monotonic()
        result = await tool.execute(
            {}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert result.success, (result, server["progress_frames"])
        assert monotonic() - start > 2 * data["servers"][0]["timeout_seconds"]
        assert server["progress_frames"] == 12
        assert "socket-secret" not in str(result) + str(state["audit"]) + caplog.text
        await state["runtime"].close()


@pytest.mark.asyncio
async def test_actual_http_sdk_annotation_warning_does_not_echo_credentials(caplog):
    async with endpoint() as server:
        state = runtime_for(server)

        def override(payload):
            if payload.get("method") == "tools/list":
                return {
                    "result": {
                        "resultType": "complete",
                        "ttlMs": 0,
                        "cacheScope": "private",
                        "tools": [
                            {
                                "name": "socket-secret",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "text": {
                                            "type": "string",
                                            "x-mcp-header": "Bearer socket-secret",
                                        },
                                    },
                                },
                            }
                        ],
                    },
                }

        server["override"] = override
        assert await state["runtime"].discover() == []
        assert state["audit"][-1]["status"] == "completed"
        assert "socket-secret" not in str(state["audit"]) + caplog.text
        await state["runtime"].close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_http_sdk_notification_exception_is_context_private(
    legacy, caplog
):
    async with endpoint(legacy=legacy, malformed_notification=True) as server:
        state = runtime_for(server)
        tool = next(
            t for t in await state["runtime"].discover() if t.remote_name == "echo"
        )
        result = await tool.execute(
            {"text": "hello"}, ToolContext(workspace="/tmp", workspace_service=None)
        )
        assert result.success, result
        assert server["progress_frames"] >= 1
        assert "socket-secret" not in str(result) + str(state["audit"]) + caplog.text
        # Filtering SDK's literal logger must retain unrelated application logs
        # from another task, even while this runtime has an active MCP request.
        caplog.clear()
        slow = next(
            t for t in await state["runtime"].discover() if t.remote_name == "slow"
        )
        task = asyncio.create_task(
            slow.execute({}, ToolContext(workspace="/tmp", workspace_service=None))
        )
        try:
            async with asyncio.timeout(5):
                await server["started"].wait()
            logging.getLogger("client").warning(
                "application client outside MCP context"
            )
            assert "application client outside MCP context" in caplog.text
        finally:
            await state["runtime"].close()
            with pytest.raises(asyncio.CancelledError):
                await task
        caplog.clear()
        logging.getLogger("client").warning("application client after MCP context")
        assert "application client after MCP context" in caplog.text
