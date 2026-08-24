"""UpdaterClient — backend → updater UDS client + envelope shape 校验。

shape 校验（纯函数）+ 连不上 → None（跨平台）；连得上 / malformed JSON（POSIX only，起 UDS server）。
"""

import asyncio
import os
import sys

import pytest


def test_is_valid_v1_envelope_shapes():
    """envelope shape 校验（纯函数，跨平台，spec §7.2）。"""
    from backend.services.updater_client import is_valid_v1_envelope

    assert (
        is_valid_v1_envelope(
            {"protocol_version": 1, "updater_version": "0.1.0", "data": {}}
        )
        is True
    )
    # protocol_version 不匹配
    assert (
        is_valid_v1_envelope(
            {"protocol_version": 2, "updater_version": "0.1.0", "data": {}}
        )
        is False
    )
    # 缺 updater_version
    assert is_valid_v1_envelope({"protocol_version": 1, "data": {}}) is False
    # updater_version 非 str
    assert (
        is_valid_v1_envelope({"protocol_version": 1, "updater_version": 1, "data": {}})
        is False
    )
    # data 非 dict
    assert (
        is_valid_v1_envelope(
            {"protocol_version": 1, "updater_version": "x", "data": "not dict"}
        )
        is False
    )
    # 非 dict
    assert is_valid_v1_envelope("not dict") is False
    assert is_valid_v1_envelope(None) is False


@pytest.mark.asyncio
async def test_get_status_returns_none_when_unreachable():
    """连不存在的 socket → None（跨平台；/version/info 据此标 disconnected）。"""
    from backend.services.updater_client import UpdaterClient

    client = UpdaterClient(
        socket_path="/tmp/sakura-updater-not-exist.sock", timeout=1.0
    )
    result = await client.get_status()
    assert result is None


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_get_status_returns_envelope_when_connected(tmp_path):
    import uvicorn

    from backend.services.updater_client import UpdaterClient

    socket_path = str(tmp_path / "updater.sock")

    async def mini_app(scope, receive, send):
        """最小 ASGI app，回固定 envelope（不依赖 updater 包；版本只在顶层）。"""
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'{"protocol_version":1,"updater_version":"0.1.0",'
                    b'"data":{"state":"idle","has_active_job":false,'
                    b'"active_job_id":null,"deployment":null}}'
                ),
            }
        )

    config = uvicorn.Config(mini_app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        client = UpdaterClient(socket_path=socket_path, timeout=2.0)
        result = await client.get_status()
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert result is not None
    assert result["protocol_version"] == 1
    assert result["updater_version"] == "0.1.0"
    assert result["data"]["state"] == "idle"


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_get_status_returns_none_for_malformed_json(tmp_path):
    """HTTP 200 + 半截 JSON → ValueError 捕获 → None（不当 connected，防 /version/info 500）。"""
    import uvicorn

    from backend.services.updater_client import UpdaterClient

    socket_path = str(tmp_path / "updater.sock")

    async def mini_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            # 半截 JSON：resp.json() 会抛 ValueError（JSONDecodeError）
            {"type": "http.response.body", "body": b'{"protocol_version":1,'}
        )

    config = uvicorn.Config(mini_app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        client = UpdaterClient(socket_path=socket_path, timeout=2.0)
        result = await client.get_status()
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert result is None
