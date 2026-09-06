"""Agent 文件写入路径的权限语义测试 / Permission semantics of agent file writes.

背景：宿主机 fs.protected_regular=2 会对 sticky 目录中"O_CREAT 打开异属主
既有文件"无条件 EACCES（root 也不豁免）。既有文件写入必须不带 O_CREAT。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.services.agent_team.tools import file_utils
from backend.services.agent_team.tools.file_utils import write_workspace_bytes


def _capture_open_flags(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """记录每次 os.open 收到的 flags / Record flags passed to os.open."""
    calls: list[int] = []
    real_open = os.open

    def wrapped(path, flags, mode=0o777, *, dir_fd=None):
        calls.append(flags)
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return real_open(path, flags, mode)

    monkeypatch.setattr(file_utils.os, "open", wrapped)
    return calls


def test_existing_file_write_omits_o_creat(tmp_path, monkeypatch):
    target = tmp_path / "main.py"
    target.write_text("old", encoding="utf-8")
    calls = _capture_open_flags(monkeypatch)

    write_workspace_bytes(target, b"new")

    assert calls, "write_workspace_bytes 必须经由 os.open"
    flags = calls[0]
    assert flags & os.O_WRONLY
    assert not flags & os.O_CREAT, "修改既有文件不得携带 O_CREAT（protected_regular 语义）"


def test_missing_file_created_with_o_creat_and_o_excl(tmp_path, monkeypatch):
    target = tmp_path / "brand_new.py"
    calls = _capture_open_flags(monkeypatch)

    write_workspace_bytes(target, b"new")

    create_flags = [flags for flags in calls if flags & os.O_CREAT]
    assert create_flags, "新建文件必须使用 O_CREAT"
    assert all(flags & os.O_EXCL for flags in create_flags)


def test_write_roundtrip_preserves_bytes(tmp_path):
    target = tmp_path / "a.txt"
    payload = "你好\r\n世界\n".encode()
    write_workspace_bytes(target, payload)
    assert target.read_bytes() == payload
    write_workspace_bytes(target, b"replace")
    assert target.read_bytes() == b"replace"


def _protected_regular_enforced() -> bool:
    try:
        return int(Path("/proc/sys/fs/protected_regular").read_text().strip()) >= 2
    except OSError:
        return False


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0 or not _protected_regular_enforced(),
    reason="需要 root + fs.protected_regular>=2 才能真实复现跨属主 sticky 拒绝",
)
def test_sticky_dir_foreign_owner_regression(tmp_path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    target = workdir / "main.py"
    target.write_text("baseline", encoding="utf-8")
    workdir.chmod(0o1775)  # 模拟 _protect_pointer_parent 的 sticky 根目录
    target.chown(65532, 65532)  # 模拟 _handoff_tree 移交后的属主
    try:
        # 旧语义（O_CREAT 打开异属主文件）：内核必须拒绝，即使 root
        with pytest.raises(PermissionError):
            os.open(target, os.O_WRONLY | os.O_CREAT)
        # 新语义：不带 O_CREAT，root 可写
        write_workspace_bytes(target, b"edited")
        assert target.read_bytes() == b"edited"
    finally:
        target.chown(0, 0)
        workdir.chmod(0o755)
