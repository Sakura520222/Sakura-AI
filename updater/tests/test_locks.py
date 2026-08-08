"""locks.py — OS flock 进程唯一性（POSIX only；Windows 整模块 skip）。"""

import os

import pytest

# fcntl 仅 POSIX；Windows 在 collect 阶段即 skip 整个模块（避免 ImportError）。
fcntl = pytest.importorskip("fcntl")

from sakura_ai_updater.locks import (
    LockBusyError,
    acquire_process_lock,
    release_process_lock,
)


def test_second_acquire_fails_busy(tmp_path):
    """同一进程内两次 open 同一 lock 文件，第二个 acquire 必须失败（spec §7.5）。

    不同 open-file-description 在 Linux 上 flock 互斥（man flock.2）。
    """
    lock_path = str(tmp_path / "updater.lock")
    fd1 = acquire_process_lock(lock_path)
    try:
        with pytest.raises(LockBusyError):
            acquire_process_lock(lock_path)
    finally:
        release_process_lock(fd1)


def test_release_allows_reacquire(tmp_path):
    lock_path = str(tmp_path / "updater.lock")
    fd1 = acquire_process_lock(lock_path)
    release_process_lock(fd1)
    fd2 = acquire_process_lock(lock_path)  # 释放后可重新获取
    release_process_lock(fd2)


def test_acquire_creates_lock_file_and_parent_dir(tmp_path):
    lock_path = str(tmp_path / "nested" / "updater.lock")
    fd = acquire_process_lock(lock_path)
    try:
        assert os.path.exists(lock_path)
    finally:
        release_process_lock(fd)


def test_second_process_cannot_start(tmp_path):
    """子进程 acquire 同一 lock → LockBusyError（真实跨进程 daemon 互斥 invariant）。

    不只靠 Task 3 手动三终端验证，把"第二 daemon 进程不能启动"自动化。
    """
    import subprocess
    import sys as _sys

    lock_path = tmp_path / "updater.lock"
    fd = acquire_process_lock(str(lock_path))
    try:
        result = subprocess.run(
            [
                _sys.executable,
                "-c",
                "import sys\n"
                "from sakura_ai_updater.locks import acquire_process_lock, LockBusyError\n"
                "try:\n"
                "    acquire_process_lock(sys.argv[1])\n"
                "except LockBusyError:\n"
                "    sys.exit(3)\n"
                "sys.exit(0)\n",
                str(lock_path),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 3, (
            f"expected LockBusyError (exit 3); got {result.returncode}; "
            f"stderr={result.stderr.decode()!r}"
        )
    finally:
        release_process_lock(fd)
