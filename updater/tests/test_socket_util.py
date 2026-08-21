"""socket_util — UDS 文件 lifecycle（stat/os.remove 跨平台；bind/connect socket POSIX only）。

prepare_socket_path 不创建父目录（/run/sakura-ai bootstrap 属 3b）；用 AF_UNIX connect
probe 区分 live/stale socket（绝不 unlink live）。
"""

import os
import stat
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


# =============================================================================
# bind_socket_listener（Task 2）：预绑定 + ownership/mode + 失败清理
# =============================================================================


class _FakeListener:
    """socket.socket 的 fake（记录 bind/listen/close，跨平台验证调用序列）。"""

    def __init__(self):
        self.binds = []
        self.listen_calls = 0
        self.closed = False

    def bind(self, path):
        self.binds.append(path)

    def listen(self, backlog):
        self.listen_calls += 1

    def close(self):
        self.closed = True


def _patch_socket_factory(monkeypatch, listener):
    """Monkeypatch socket 模块的 socket 构造与常量 → 返回 fake listener（跨平台）。"""
    import socket as socket_mod

    monkeypatch.setattr(socket_mod, "socket", lambda *a, **kw: listener)
    monkeypatch.setattr(socket_mod, "AF_UNIX", 1, raising=False)  # Windows 无 AF_UNIX
    monkeypatch.setattr(socket_mod, "SOMAXCONN", 128, raising=False)


def test_bind_socket_listener_sets_owner_mode_before_listen(tmp_path, monkeypatch):
    """调用序列：prepare → bind → chown(0, 9472) → chmod(0660) → listen。"""
    from sakura_ai_updater import socket_util

    path = str(tmp_path / "updater.sock")
    listener = _FakeListener()
    _patch_socket_factory(monkeypatch, listener)
    steps = []
    monkeypatch.setattr(
        socket_util.os,
        "chown",
        lambda p, u, g: steps.append(("chown", p, u, g)) or None,
        raising=False,  # Windows os 模块无 chown 属性，注入即可
    )
    monkeypatch.setattr(
        socket_util.os,
        "chmod",
        lambda p, m: steps.append(("chmod", p, m)) or None,
        raising=False,
    )
    monkeypatch.setattr(
        socket_util, "prepare_socket_path", lambda p: steps.append(("prepare", p))
    )

    result = socket_util.bind_socket_listener(path, uid=0, gid=9472, mode=0o660)

    assert result is listener  # 返回同一个已绑定 listener
    assert steps[0] == ("prepare", path)
    assert listener.binds == [path]  # bind 在 chown 之前
    assert steps[1:] == [("chown", path, 0, 9472), ("chmod", path, 0o660)]
    assert listener.listen_calls == 1  # listen 最后（可接受连接前 ownership/mode 已设）
    assert listener.closed is False


def test_bind_socket_listener_cleans_socket_when_chown_fails(tmp_path, monkeypatch):
    """chown 抛异常 → listener 关闭 + socket 文件清理 + 异常传播（不吞）。"""
    from sakura_ai_updater import socket_util

    path = str(tmp_path / "updater.sock")
    listener = _FakeListener()
    _patch_socket_factory(monkeypatch, listener)

    def _boom_chown(p, u, g):
        raise PermissionError("EACCES")

    monkeypatch.setattr(socket_util.os, "chown", _boom_chown, raising=False)
    cleaned = []
    monkeypatch.setattr(
        socket_util, "cleanup_owned_socket", lambda p: cleaned.append(p) or None
    )
    with pytest.raises(PermissionError, match="EACCES"):
        socket_util.bind_socket_listener(path, uid=0, gid=9472, mode=0o660)
    assert listener.closed is True  # listener 已关闭（不泄漏 fd）
    assert cleaned == [path]  # socket 文件已清理
    assert listener.listen_calls == 0  # 失败路径绝不 listen


def test_bind_socket_listener_cleans_socket_when_bind_fails(tmp_path, monkeypatch):
    """bind 抛异常 → listener 关闭 + 清理 + 传播（与 chown 失败同一 fail-closed 路径）。"""
    from sakura_ai_updater import socket_util

    path = str(tmp_path / "updater.sock")

    class _BindBoom(_FakeListener):
        def bind(self, p):
            raise OSError("address in use")

    listener = _BindBoom()
    _patch_socket_factory(monkeypatch, listener)
    cleaned = []
    monkeypatch.setattr(
        socket_util, "cleanup_owned_socket", lambda p: cleaned.append(p) or None
    )
    with pytest.raises(OSError, match="address in use"):
        socket_util.bind_socket_listener(path)
    assert listener.closed is True
    assert cleaned == [path]


def test_bind_socket_listener_requires_existing_parent(tmp_path, monkeypatch):
    """父目录不存在 → SocketPathError（pre-bind 不越界创建 /run/sakura-ai）。

    prepare 失败发生在 listener 创建之前：从不 bind/close（无 socket 需要清理）。
    """
    from sakura_ai_updater import socket_util

    path = str(tmp_path / "nonexistent" / "updater.sock")
    listener = _FakeListener()
    _patch_socket_factory(monkeypatch, listener)
    with pytest.raises(SocketPathError):
        socket_util.bind_socket_listener(path)
    assert not os.path.exists(tmp_path / "nonexistent")  # 未创建
    assert listener.binds == []  # prepare 失败 → listener 从未 bind


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_bind_socket_listener_real_uds_sets_owner_mode(tmp_path):
    """真实 UDS：bind/listen 成功后 stat 验证 mode（chown 用当前 uid/gid，非 root 可跑）。"""
    from sakura_ai_updater.socket_util import bind_socket_listener

    path = str(tmp_path / "updater.sock")
    listener = bind_socket_listener(path, uid=os.getuid(), gid=os.getgid(), mode=0o660)
    try:
        st = os.stat(path)
        assert stat.S_ISSOCK(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o660
        assert st.st_uid == os.getuid()
        assert st.st_gid == os.getgid()
    finally:
        listener.close()
        cleanup_owned_socket(path)
