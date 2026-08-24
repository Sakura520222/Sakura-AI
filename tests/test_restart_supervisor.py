"""应用重启机制（监督循环 + 优雅停机退出码契约）的回归测试。"""

from __future__ import annotations

import argparse
import signal
from types import SimpleNamespace

import pytest

import backend.main as main_module
from backend.core import server_runtime
from backend.core.setup_service import SetupService
from backend.webui.sse import sse_manager


@pytest.fixture(autouse=True)
def _reset_server_runtime(monkeypatch):
    monkeypatch.setattr(server_runtime, "_server", None)
    monkeypatch.setattr(server_runtime, "_restart_requested", False)


def _make_args(**overrides) -> argparse.Namespace:
    values = {
        "serve": False,
        "no_reload": True,
        "host": "127.0.0.1",
        "port": None,
        "log_level": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _FakeProc:
    def __init__(self, pid: int, exit_code: int | None = None):
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.exit_code

    def kill(self) -> None:
        self.killed = True
        self.exit_code = 0


def test_request_restart_without_server_returns_false():
    assert server_runtime.request_restart() is False
    assert server_runtime.restart_requested() is False


def test_request_restart_sets_graceful_exit_flag():
    server = SimpleNamespace(should_exit=False)
    server_runtime.register_server(server)

    assert server_runtime.request_restart() is True

    assert server.should_exit is True
    assert server_runtime.restart_requested() is True


def test_trigger_restart_requests_graceful_shutdown(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(sse_manager, "close_all", lambda: events.append("close_sse"))
    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(server_runtime, "_server", server)
    import backend.core.setup_service as setup_service_module

    monkeypatch.setattr(
        setup_service_module.os,
        "kill",
        lambda pid, sig: events.append(("kill", pid, sig)),
    )

    SetupService().trigger_restart()

    assert events == ["close_sse"]
    assert server.should_exit is True


def test_trigger_restart_falls_back_to_sigterm_without_server(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(sse_manager, "close_all", lambda: events.append("close_sse"))
    import backend.core.setup_service as setup_service_module

    monkeypatch.setattr(setup_service_module.os, "getpid", lambda: 1234)
    monkeypatch.setattr(
        setup_service_module.os,
        "kill",
        lambda pid, sig: events.append(("kill", pid, sig)),
    )

    SetupService().trigger_restart()

    assert events == ["close_sse", ("kill", 1234, signal.SIGTERM)]


def test_supervisor_respawns_on_restart_exit_code():
    procs = [
        _FakeProc(101, exit_code=server_runtime.RESTART_EXIT_CODE),
        _FakeProc(102, exit_code=0),
    ]

    return_code = main_module._run_supervisor(
        _make_args(), spawn=lambda _args: procs.pop(0), poll_interval=0
    )

    assert return_code == 0
    assert procs == []


def test_supervisor_exits_on_child_crash():
    procs = [_FakeProc(101, exit_code=1)]

    return_code = main_module._run_supervisor(
        _make_args(), spawn=lambda _args: procs.pop(0), poll_interval=0
    )

    assert return_code == 1
    assert procs == []


def test_supervisor_stops_child_on_keyboard_interrupt():
    class _InterruptingProc(_FakeProc):
        def poll(self) -> int | None:
            raise KeyboardInterrupt

    proc = _InterruptingProc(101, exit_code=None)

    return_code = main_module._run_supervisor(
        _make_args(), spawn=lambda _args: proc, poll_interval=0
    )

    assert return_code == 130
    assert proc.terminated is True
