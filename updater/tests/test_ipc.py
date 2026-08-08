"""ipc.py — envelope + /v1/status。

TestClient 测 HTTP 逻辑（跨平台）；UDS 端到端集成测试 POSIX only（external
listener pre-bind，Config 不含 uds=）。版本字段只在 envelope 顶层，data 不重复
（spec §7.2）。
"""

import asyncio
import os
import sys

import pytest
from sakura_ai_updater import PROTOCOL_VERSION
from sakura_ai_updater.backends.daemon import DEFAULT_GID
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
async def test_serve_over_prebound_uds(tmp_path):
    """端到端 UDS：host pre-bind listener → uvicorn serve(sockets=[listener])，httpx 连。

    验证 Task 2 transport 架构：FastAPI app 保持纯 HTTP（Config 不含 uds=），UDS
    listener 由 socket_util 预绑定（chown/chmod）后交给 uvicorn。
    """
    import httpx
    import uvicorn
    from sakura_ai_updater.socket_util import bind_socket_listener

    socket_path = str(tmp_path / "updater.sock")
    state_path = str(tmp_path / "update-state.json")
    app = create_app(state_path)
    listener = bind_socket_listener(
        socket_path, uid=os.getuid(), gid=os.getgid(), mode=0o660
    )
    config = uvicorn.Config(app, log_level="warning")
    assert not hasattr(config, "uds") or config.uds is None  # Config 不含 uds=
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
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
        listener.close()


def test_serve_prebinds_listener_before_uvicorn(monkeypatch, tmp_path):
    """serve()：create_app 不加 socket 参数；Config 无 uds=；serve 收到 sockets=[listener]。

    pre-bound listener 在 uvicorn 前已 bind/listen（ownership/mode 先于连接）。
    locks（fcntl）与 uvicorn 均被替换（serve 内 import 经 sys.modules 生效），跨平台。
    """
    import sys
    import types

    import sakura_ai_updater.__main__ as main_mod

    class _StubListener:
        """pre-bound listener 的 stub：记录 close（finally 清理契约）。"""

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    socket_path = str(tmp_path / "updater.sock")
    state_path = str(tmp_path / "update-state.json")
    lock_path = str(tmp_path / "updater.lock")

    seen = {}

    def _fake_acquire(path):
        seen["lock_fd"] = ("fd", path)
        return seen["lock_fd"]

    def _fake_release(fd):
        seen.setdefault("released", fd)

    # serve() 内 `from sakura_ai_updater.locks import ...`（locks 顶层 import fcntl，
    # Windows 无 fcntl）→ 注入 fake locks 模块，跨平台运行
    fake_locks = types.ModuleType("sakura_ai_updater.locks")
    fake_locks.LockBusyError = type("LockBusyError", (RuntimeError,), {})
    fake_locks.acquire_process_lock = _fake_acquire
    fake_locks.release_process_lock = _fake_release
    monkeypatch.setitem(sys.modules, "sakura_ai_updater.locks", fake_locks)

    def _fake_create_app(state_path_arg):
        seen["state_path"] = state_path_arg
        return create_app(state_path_arg)

    def _fake_bind(path, **kwargs):
        seen["bind"] = (path, kwargs)
        seen["listener"] = _StubListener()  # pre-bind 行为由 test_socket_util 覆盖
        return seen["listener"]

    def _fake_config(app, **kwargs):
        seen["config_kwargs"] = kwargs
        return object()  # _FakeServer 只记录 config，不依赖真实 Config

    class _FakeServer:
        def __init__(self, config):
            seen["config"] = config

        async def serve(self, sockets=None):
            seen["sockets"] = sockets

    def _fake_run(coro):
        seen["ran"] = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)  # 真实 await，FakeServer.serve 记录 sockets
        finally:
            loop.close()

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = _fake_config
    fake_uvicorn.Server = _FakeServer

    monkeypatch.setattr(main_mod, "create_app", _fake_create_app)
    monkeypatch.setattr(main_mod, "bind_socket_listener", _fake_bind)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(main_mod.asyncio, "run", _fake_run)

    main_mod.serve(socket_path, state_path, lock_path)

    assert seen["state_path"] == state_path  # create_app 唯一参数是 state_path
    assert "uds" not in seen["config_kwargs"]  # Config 不含 uds=
    assert seen["sockets"] == [seen["listener"]]  # serve 收到 pre-bound listener
    assert seen["ran"] is True
    assert seen["listener"] is not None
    assert seen["listener"].closed is True  # finally 关闭 pre-bound listener
    assert seen["released"] == seen["lock_fd"]  # finally 释放 flock
    assert not os.path.exists(socket_path)  # finally 已 cleanup socket


def test_serve_parser_has_socket_gid_and_uid(monkeypatch, tmp_path):
    """--serve parser 提供 --socket-gid（int，默认 DEFAULT_GID=9472）和 --socket-uid（默认 0）。"""
    import sakura_ai_updater.__main__ as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "serve", lambda *a, **kw: calls.append((a, kw)))
    state_dir = str(tmp_path)
    main_mod.main(
        ["--serve", "--socket-path", "x.sock", "--state-dir", state_dir,
         "--socket-gid", "9999", "--socket-uid", "1000"]
    )
    assert calls[0][1]["socket_gid"] == 9999
    assert calls[0][1]["socket_uid"] == 1000
    main_mod.main(["--serve", "--socket-path", "x.sock", "--state-dir", state_dir])
    assert calls[1][1]["socket_gid"] == DEFAULT_GID  # 默认 9472
    assert calls[1][1]["socket_uid"] == 0  # 默认 root
