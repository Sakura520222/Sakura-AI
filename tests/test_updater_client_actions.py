"""Typed updater client action error boundaries."""

import pytest
from httpx import Response

from backend.services.updater_client import (
    UpdaterActionError,
    UpdaterClient,
    UpdaterProtocolError,
    UpdaterUnavailableError,
)


@pytest.mark.asyncio
async def test_actions_preserve_typed_errors(monkeypatch):
    client = UpdaterClient(socket_path="/tmp/does-not-exist.sock")

    async def unavailable(*args, **kwargs):
        raise UpdaterUnavailableError("socket unavailable")

    monkeypatch.setattr(client, "_request", unavailable)
    with pytest.raises(UpdaterUnavailableError):
        await client.check()

    async def protocol(*args, **kwargs):
        raise UpdaterProtocolError("bad envelope")

    monkeypatch.setattr(client, "_request", protocol)
    with pytest.raises(UpdaterProtocolError):
        await client.preflight("3.1.0")

    async def conflict(*args, **kwargs):
        raise UpdaterActionError(409, {"error": "update_in_progress", "job_id": "upd_existing"})

    monkeypatch.setattr(client, "_request", conflict)
    with pytest.raises(UpdaterActionError) as exc:
        await client.update("3.1.0")
    assert exc.value.status_code == 409
    assert exc.value.body["job_id"] == "upd_existing"


@pytest.mark.asyncio
async def test_action_paths_and_json_payload(monkeypatch):
    client = UpdaterClient(socket_path="/tmp/unused.sock")
    seen = []

    async def request(method, path, body=None):
        seen.append((method, path, body))
        return {"protocol_version": 1, "updater_version": "0.1.0", "data": {}}

    monkeypatch.setattr(client, "_request", request)
    await client.check()
    await client.preflight("3.1.0")
    await client.update("3.1.0")
    await client.get_job("upd_1")
    await client.get_job_logs("upd_1")
    assert seen == [
        ("POST", "/v1/check", None),
        ("POST", "/v1/preflight", {"target_version": "3.1.0"}),
        ("POST", "/v1/update", {"target_version": "3.1.0"}),
        ("GET", "/v1/jobs/upd_1", None),
        ("GET", "/v1/jobs/upd_1/logs", None),
    ]


@pytest.mark.asyncio
async def test_action_requests_use_long_timeout_but_job_polling_stays_short(monkeypatch):
    client = UpdaterClient(
        socket_path="/tmp/unused.sock",
        timeout=2.0,
        action_timeout=90.0,
    )
    seen_timeouts: list[float] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout, **kwargs):
            seen_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, method, path, json=None):
            return Response(
                200,
                json={
                    "protocol_version": 1,
                    "updater_version": "0.1.0",
                    "data": {},
                },
            )

    monkeypatch.setattr(
        "backend.services.updater_client.httpx.AsyncClient", FakeAsyncClient
    )

    await client.check()
    await client.preflight("3.1.0")
    await client.update("3.1.0")
    await client.get_job("upd_1")
    await client.get_job_logs("upd_1")

    assert seen_timeouts == [90.0, 90.0, 90.0, 2.0, 2.0]
