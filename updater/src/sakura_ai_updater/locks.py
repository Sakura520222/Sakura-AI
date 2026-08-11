"""OS-level flock 进程唯一性（spec §7.5 第一层锁）。

daemon 启动时 ``acquire_process_lock``（``LOCK_EX|LOCK_NB``），获取失败则退出——
防止 DaemonBackend 因 race 被拉起两份、两个 Python 进程各有自己的 ``asyncio.Lock``。

POSIX only（fcntl）。updater 是 Linux 宿主机组件（spec §4）；Windows 开发机不跑 updater。
"""

from __future__ import annotations

import fcntl
import os


class LockBusyError(RuntimeError):
    """另一个 updater 进程已持有锁。"""


def acquire_process_lock(path: str) -> int:
    """获取进程唯一锁（非阻塞）。返回 fd（调用方须持有以保持锁）。

    ``LOCK_EX | LOCK_NB``：拿不到立即失败（LockBusyError），不阻塞。
    fd 不能关闭——关闭即释放锁。daemon 进程生命周期内持有。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        # EAGAIN→busy（拿不到锁）；EINTR/ENOLCK 等同样 fail-closed 为 LockBusyError，先关 fd 防泄漏
        os.close(fd)
        raise LockBusyError(f"cannot lock {path}: {e}") from e
    return fd


def release_process_lock(fd: int) -> None:
    """释放锁并关闭 fd（正常退出时调用；进程死亡 OS 自动释放）。"""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
