"""进程级 uvicorn Server 句柄与重启请求状态。

``python -m backend.main`` 以监督循环拉起应用子进程；子进程在启动时把
uvicorn ``Server`` 实例登记到这里。应用内的重启请求（Setup 完成、
管理员重启等）通过 ``Server.should_exit`` 触发优雅停机，进程随后以
``RESTART_EXIT_CODE`` 退出，由监督循环或容器重启策略重新拉起。

监听地址/端口的有效性依赖 uvicorn 的启动顺序：``Server.startup()``
先运行 ``lifespan.startup()``（其中会从数据库加载 app_host/app_port 到
Settings 单例），之后才用 ``config.host`` / ``config.port`` 创建监听
socket。因此 lifespan 在数据库配置加载完成后调用
``apply_configured_bind``，把数据库配置回写到已登记的 Server——这是
数据库配置能够影响实际绑定的唯一窗口。uvicorn CLI 直启（Docker CMD）
从不调用 ``register_server``，容器命令行参数保持权威，不受影响。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uvicorn import Server

# 监督循环与应用子进程约定的"请求重启"退出码。
RESTART_EXIT_CODE = 42

_server: Server | None = None
_restart_requested = False
# 应用子进程启动时显式给出的 CLI 绑定参数；None 表示未显式指定，
# 此时监听值由数据库配置（回落到 Settings 默认）决定。
_cli_host: str | None = None
_cli_port: int | None = None


def register_server(
    server: Server,
    *,
    cli_host: str | None = None,
    cli_port: int | None = None,
) -> None:
    """登记当前进程的 uvicorn Server 实例及其显式 CLI 绑定参数。"""

    global _server, _cli_host, _cli_port
    _server = server
    _cli_host = cli_host
    _cli_port = cli_port


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


def _is_valid_bind_host(host: object) -> bool:
    """防御性校验：数据库脏行不得让进程绑定到非法地址。"""

    return isinstance(host, str) and bool(host.strip())


def _is_valid_bind_port(port: object) -> bool:
    """防御性校验：端口必须是 1-65535 的整数。"""

    if isinstance(port, bool) or not isinstance(port, int):
        return False
    return 1 <= port <= 65535


def apply_configured_bind(host: str, port: int) -> tuple[str | None, int | None]:
    """把数据库配置的监听地址/端口回写到已登记的 Server。

    在 lifespan 完成数据库配置加载后、socket 绑定前调用。显式 CLI
    参数优先（不回写对应项）；未登记 Server（uvicorn 直启/Docker）时
    为空操作。返回 ``(applied_host, applied_port)``，未应用的项为
    ``None``，供调用方记录日志。
    """

    server = _server
    if server is None:
        return (None, None)

    applied_host: str | None = None
    applied_port: int | None = None
    if _cli_host is None and _is_valid_bind_host(host):
        server.config.host = str(host).strip()
        applied_host = server.config.host
    if _cli_port is None and _is_valid_bind_port(port):
        server.config.port = int(port)
        applied_port = server.config.port
    return (applied_host, applied_port)
