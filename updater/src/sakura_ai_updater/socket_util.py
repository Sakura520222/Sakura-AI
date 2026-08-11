"""UDS socket 文件 lifecycle（live/stale 真检测 + owned 清理）。

Python 3.12 的 ``asyncio.create_unix_server`` 无 ``cleanup_socket`` 参数（3.13 才加），
直接用 ``uvicorn.Server.serve()`` 也绕过 ``uvicorn.run()`` wrapper 的 socket 清理。故
updater 自己管理 socket 文件。

**不创建父目录**：``/run/sakura-ai`` 创建/bootstrap 属 Slice 3b；本模块要求父目录已存在，
否则 SocketPathError（dev/生产须先 mkdir）。

**live vs stale 真检测**：用 AF_UNIX connect probe——connect 成功说明有 daemon 正监听
（live），绝不 unlink；ConnectionRefused 说明是上次崩溃残留（stale），安全删除。避免
"任何已存在 socket 都当 stale 删"导致误配置时 unlink live socket。
"""

from __future__ import annotations

import os
import socket
import stat


class SocketPathError(RuntimeError):
    """socket 路径不可用（父目录缺失 / 非 socket 文件占用 / live socket / 探测失败）。"""


def prepare_socket_path(socket_path: str) -> None:
    """启动前确保 socket 路径可用（**不创建父目录**）。

    - 父目录不存在 → SocketPathError（3a 不越界创建 /run/sakura-ai）。
    - 路径不存在 → OK（uvicorn 将创建）。
    - 已存在但**不是** socket（普通文件/目录）→ SocketPathError（拒绝启动，不乱删）。
    - 已存在且是 Unix socket：
        - AF_UNIX connect 成功 → **live socket**（另一 daemon 正监听）→ SocketPathError
          （防误配置：同 socket path 不同 lock path 时 unlink live socket）。
        - ConnectionRefused → stale（上次崩溃残留）→ unlink。
        - 其他 OSError → SocketPathError（fail-closed，不删）。
    """
    parent = os.path.dirname(socket_path) or "."
    if not os.path.isdir(parent):
        raise SocketPathError(
            f"socket parent directory does not exist: {parent!r} "
            f"(create it before starting the daemon; /run/sakura-ai bootstrap is Slice 3b)"
        )
    if not os.path.exists(socket_path):
        return
    try:
        st = os.stat(socket_path)
    except OSError:
        if not os.path.exists(socket_path):
            return  # 竞态：文件刚消失，放行
        raise SocketPathError(f"cannot stat socket path {socket_path!r}") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketPathError(
            f"socket path {socket_path!r} exists but is not a socket; "
            f"refusing to remove a non-socket file"
        )
    # 区分 live vs stale：connect probe
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(socket_path)
    except ConnectionRefusedError:
        probe.close()
        try:
            os.remove(socket_path)
        except OSError as e:
            raise SocketPathError(f"cannot remove stale socket {socket_path!r}: {e}") from None
        return
    except OSError as e:
        probe.close()
        raise SocketPathError(f"cannot probe socket {socket_path!r}: {e}") from None
    # connect 成功 = live（另一 daemon 正监听），绝不 unlink
    probe.close()
    raise SocketPathError(
        f"socket {socket_path!r} is live (another daemon is listening); "
        f"refusing to unlink — check for misconfigured --lock-path"
    )


def cleanup_owned_socket(socket_path: str) -> None:
    """关闭后删除自己拥有的 socket（**live 感知**）。

    connect probe 区分：connect 成功 = 仍有 daemon 监听（可能是误配置下另一 daemon 的
    live socket）→ 保留不删；connect 失败/文件消失 = stale（无人监听）→ 删除。正常退出时
    uvicorn 已 shutdown 关闭监听，自己的 socket 必是 stale，删除不受影响。
    """
    if not os.path.exists(socket_path):
        return
    try:
        st = os.stat(socket_path)
    except OSError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        return  # 非 socket 不删（cleanup 不碰普通文件）
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        probe.close()
        try:
            os.remove(socket_path)
        except OSError:
            pass  # 删除尽力而为，静默
        return
    except OSError:
        probe.close()
        return  # 探测失败 → 不删（fail-closed 于删除动作）
    probe.close()  # connect 成功 = live，保留


def bind_socket_listener(
    socket_path: str,
    *,
    uid: int = 0,
    gid: int = 9472,
    mode: int = 0o660,
) -> socket.socket:
    """预绑定 UDS listener（ownership/mode 在 listen 前设置，host bootstrap）。

    - ``prepare_socket_path`` 先做 live/stale 检查（**不创建父目录**）。
    - 顺序：bind → chown(uid, gid) → chmod(mode) → listen —— uvicorn 接受连接前
      socket 文件已是 ``0o660 root:sakura-ai``（spec §11.4；Web 容器经补充 GID
      connect，不依赖 umask）。
    - 任何异常（含 BaseException）→ listener.close() + cleanup_owned_socket +
      原样 raise（不吞；bind/chown 失败不留脏 socket 文件，不泄漏 fd）。
    - 返回值由调用方持有：close() 后自行 cleanup_owned_socket。
    """
    prepare_socket_path(socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(socket_path)
        os.chown(socket_path, uid, gid)
        os.chmod(socket_path, mode)
        listener.listen(socket.SOMAXCONN)
        return listener
    except BaseException:
        listener.close()
        cleanup_owned_socket(socket_path)
        raise
