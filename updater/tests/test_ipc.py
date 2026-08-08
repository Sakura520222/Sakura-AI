"""ipc.py — envelope + /v1/status。

TestClient 测 HTTP 逻辑（跨平台）；UDS 端到端集成测试 POSIX only。
版本字段只在 envelope 顶层，data 不重复（spec §7.2）。
"""

import asyncio
import os
import sys

import pytest
from sakura_ai_updater import PROTOCOL_VERSION
from sakura_ai_updater.ipc import create_app
from sakura_ai_updater.state import JobState, UpdateStateStore, save_state
from starlette.testclient import TestClient


def test_status_returns_envelope_with_idle_state(tmp_path):
    app = create_app(str(tmp_path / "update-state.json"))
    client = TestClient(app)
    r = client.get("/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["updater_version"]  # 顶层，非空
    assert body["data"]["state"] == "idle"
    assert body["data"]["has_active_job"] is False
    assert body["data"]["active_job_id"] is None
    # data 不重复版本字段（envelope 顶层独有）
    assert "protocol_version" not in body["data"]
    assert "updater_version" not in body["data"]


def test_status_reflects_active_job(tmp_path):
    """create_app 后写 state → /v1/status 反映（钉住"每次读最新 state，非启动快照"）。"""
    state_path = str(tmp_path / "update-state.json")
    app = create_app(state_path)
    client = TestClient(app)
    r0 = client.get("/v1/status")
    assert r0.json()["data"]["state"] == "idle"  # 尚未写文件
    save_state(
        state_path,
        UpdateStateStore(
            active_job_id="upd_001",
            current_job=JobState(
                job_id="upd_001", deployment="image", state="downloading"
            ),
        ),
    )
    r = client.get("/v1/status")
    body = r.json()
    assert body["data"]["has_active_job"] is True
    assert body["data"]["active_job_id"] == "upd_001"
    assert body["data"]["deployment"] == "image"
    assert body["data"]["state"] == "downloading"


def test_health_returns_envelope(tmp_path):
    app = create_app(str(tmp_path / "update-state.json"))
    client = TestClient(app)
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_serve_over_real_uds(tmp_path):
    """端到端 UDS：起 uvicorn 监听 unix socket，httpx UDS transport 连。"""
    import httpx
    import uvicorn

    socket_path = str(tmp_path / "updater.sock")
    state_path = str(tmp_path / "update-state.json")
    app = create_app(state_path)
    config = uvicorn.Config(app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://updater", timeout=2.0
        ) as client:
            r = await client.get("/v1/status")
            assert r.status_code == 200
            assert r.json()["protocol_version"] == PROTOCOL_VERSION
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
