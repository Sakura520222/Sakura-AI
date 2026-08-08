"""socket_util — UDS 文件 lifecycle（stat/os.remove 跨平台；bind/connect socket POSIX only）。

prepare_socket_path 不创建父目录（/run/sakura-ai bootstrap 属 3b）；用 AF_UNIX connect
probe 区分 live/stale socket（绝不 unlink live）。
"""

import os
import sys

import pytest
from sakura_ai_updater.socket_util import (
    SocketPathError,
    cleanup_owned_socket,
    prepare_socket_path,
)


def test_prepare_requires_existing_parent(tmp_path):
    """父目录不存在 → SocketPathError（3a 不越界创建 /run/sakura-ai，spec 边界）。"""
    path = str(tmp_path / "nonexistent" / "updater.sock")
    with pytest.raises(SocketPathError):
        prepare_socket_path(path)
    assert not os.path.exists(tmp_path / "nonexistent")  # 未创建


def test_prepare_refuses_non_socket_file(tmp_path):
    """非 socket 文件占用路径 → SocketPathError（绝不乱删用户文件）。"""
    path = str(tmp_path / "updater.sock")
    with open(path, "w") as f:
        f.write("important data")
    with pytest.raises(SocketPathError):
        prepare_socket_path(path)
    assert os.path.exists(path)  # 原文件保留


def test_cleanup_ignores_nonexistent(tmp_path):
    cleanup_owned_socket(str(tmp_path / "absent.sock"))  # 不报错


def test_cleanup_does_not_remove_non_socket(tmp_path):
    """cleanup 只删 socket，不误删普通文件。"""
    path = str(tmp_path / "updater.sock")
    with open(path, "w") as f:
        f.write("data")
    cleanup_owned_socket(path)
    assert os.path.exists(path)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_prepare_removes_stale_socket(tmp_path):
    """已存在的 Unix socket 但无人监听（stale，上次崩溃残留）→ 删除。"""
    import socket

    path = str(tmp_path / "updater.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()  # bind 但不 listen → connect 会 ConnectionRefused → stale
    assert os.path.exists(path)
    prepare_socket_path(path)  # 不报错
    assert not os.path.exists(path)  # stale 已删


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_prepare_refuses_live_socket(tmp_path):
    """另一个 daemon 正在监听的 socket（live）→ SocketPathError，绝不 unlink。

    防误配置：同 socket path + 不同 lock path 时，第二个 daemon 拿到自己的 flock 后
    不能 unlink 第一 daemon 正在用的 live socket。
    """
    import socket

    path = str(tmp_path / "updater.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)  # listen → connect 会成功 → live
    try:
        with pytest.raises(SocketPathError):
            prepare_socket_path(path)
        assert os.path.exists(path)  # live socket 保留，未删
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_cleanup_keeps_live_socket(tmp_path):
    """cleanup 对 live socket（另一 daemon 监听中）→ 保留不删（核心 invariant）。"""
    import socket

    path = str(tmp_path / "updater.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    try:
        cleanup_owned_socket(path)
        assert os.path.exists(path)  # live socket 保留
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_cleanup_removes_owned_socket(tmp_path):
    import socket

    path = str(tmp_path / "updater.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()
    cleanup_owned_socket(path)
    assert not os.path.exists(path)
