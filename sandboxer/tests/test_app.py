from __future__ import annotations

import httpx
import pytest
from sakura_ai_sandboxer import PROTOCOL_VERSION
from sakura_ai_sandboxer.app import create_app
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.runtime import FakeRuntimeAdapter, RuntimeResult


async def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_health_and_execution_envelopes_are_versioned():
    app = create_app(
        SandboxdConfig(max_output_bytes=100),
        runtime=FakeRuntimeAdapter(RuntimeResult(stdout="ok")),
    )
    health = await _request(app, "GET", "/v1/health")
    assert health.status_code == 200
    assert health.json()["protocol_version"] == PROTOCOL_VERSION
    assert health.json()["data"]["runtime"] == "fake"

    response = await _request(
        app,
        "POST",
        "/v1/executions",
        json={
            "request_id": "request-1",
            "workspace_key": "task-1",
            "command": "printf ok",
            "profile": "agent",
            "timeout_seconds": 1,
            "network_mode": "none",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["stdout"] == "ok"


@pytest.mark.asyncio
async def test_unknown_fields_are_422_with_versioned_error():
    app = create_app(runtime=FakeRuntimeAdapter())
    response = await _request(
        app,
        "POST",
        "/v1/executions",
        json={
            "request_id": "request-1",
            "workspace_key": "task-1",
            "command": "echo ok",
            "profile": "agent",
            "timeout_seconds": 1,
            "image": "evil-image",
            "network": "host",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "INVALID_REQUEST"
    assert response.json()["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_egress_capability_is_denied_when_daemon_has_no_egress_network():
    app = create_app(
        SandboxdConfig(egress_network="none"),
        runtime=FakeRuntimeAdapter(),
    )
    response = await _request(
        app,
        "POST",
        "/v1/executions",
        json={
            "request_id": "request-egress",
            "workspace_key": "task-1",
            "command": "echo denied",
            "profile": "agent",
            "network_mode": "egress",
            "timeout_seconds": 1,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "POLICY_DENIED"


@pytest.mark.asyncio
async def test_response_envelope_budget_drops_error_detail():
    app = create_app(
        SandboxdConfig(max_output_bytes=1, max_response_bytes=100),
        runtime=FakeRuntimeAdapter(),
    )
    response = await _request(
        app,
        "POST",
        "/v1/executions",
        json={
            "request_id": "request-1",
            "workspace_key": "task-1",
            "command": "echo ok",
            "profile": "agent",
            "timeout_seconds": 1,
            "unknown": "reject",
        },
    )
    assert response.status_code == 422
    assert len(response.content) <= 100
    assert "detail" not in response.json()


@pytest.mark.asyncio
async def test_cancel_endpoint_is_idempotent_for_unknown_request():
    app = create_app(runtime=FakeRuntimeAdapter())
    first = await _request(app, "POST", "/v1/executions/missing/cancel")
    second = await _request(app, "POST", "/v1/executions/missing/cancel")
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["cancelled"] is True
    assert second.json()["data"] == {
        "request_id": "missing",
        "cancelled": True,
        "state": "cancelled",
    }
