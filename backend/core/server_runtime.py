"""进程级 uvicorn Server 句柄与重启请求状态。

``python -m backend.main`` 以监督循环拉起应用子进程；子进程在启动时把
uvicorn ``Server`` 实例登记到这里。应用内的重启请求（Setup 完成、
管理员重启等）通过 ``Server.should_exit`` 触发优雅停机，进程随后以
``RESTART_EXIT_CODE`` 退出，由监督循环或容器重启策略重新拉起。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uvicorn import Server

# 监督循环与应用子进程约定的"请求重启"退出码。
RESTART_EXIT_CODE = 42

_server: Server | None = None
_restart_requested = False


def register_server(server: Server) -> None:
    """登记当前进程的 uvicorn Server 实例。"""

    global _server
    _server = server


def request_restart() -> bool:
    """请求优雅停机并标记重启；未登记 Server 时返回 False。

    uvicorn 收到 ``should_exit`` 后按 ``timeout_graceful_shutdown``
    优雅关闭（含 lifespan shutdown），调用方无需发送任何信号。
    """

    global _restart_requested
    server = _server
    if server is None:
        return False
    _restart_requested = True
    server.should_exit = True
    return True


def restart_requested() -> bool:
    """是否已请求重启；子进程退出时据此选择约定退出码。"""

    return _restart_requested
