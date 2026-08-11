"""DaemonBackend lifecycle、readiness、PID identity 单测。

All tests are cross-platform (run on Windows): /proc reads, UDS health probe,
os.geteuid, subprocess.Popen, and os.kill are monkeypatched — tests never touch
real /proc, UDS, root privileges, or child processes. DaemonBackend 全部外部依赖
（/proc、UDS、root、Popen）都经模块级 helper 间接，测试逐个 monkeypatch。
"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sakura_ai_updater.backends import daemon as daemon_mod
from sakura_ai_updater.backends.daemon import (
    DEFAULT_BINARY_NAME,
    DEFAULT_GID,
    DEFAULT_GROUP,
    DEFAULT_RUN_DIR,
    DEFAULT_SOCKET_PATH,
    DEFAULT_STARTUP_TIMEOUT,
    DaemonBackend,
    GIDConflictError,
    PrivilegeError,
    UnsafeDeploymentPathError,
    UpdaterNotInstalledError,
    UpdaterStartError,
    _is_same_process,
    _matches_identity,
    _read_proc_cmdline,
    _read_proc_starttime,
)

# =============================================================================
# helpers
# =============================================================================

STAT_LINE = (
    "1234 (python3.14) S 0 1234 1234 0 -1 4194304 100 0 0 0 1 0 0 0 "
    "20 0 1 0 555666 100000 0 0 0 0 0 0 0 0 0"
)

SIGTERM = getattr(signal, "SIGTERM", 15)
SIGKILL = getattr(signal, "SIGKILL", 9)


def _patch_proc_open(monkeypatch, pid: int, *, stat: str | None = None, cmdline: bytes | None = None):
    """Serve fake /proc/{pid}/stat and /proc/{pid}/cmdline via builtins.open.

    命中 ``/proc/<pid>/stat`` 或 ``/proc/<pid>/cmdline`` 时：有数据则返回 mock，
    未提供数据则显式抛 ``FileNotFoundError``——绝不 fall through 到真实 /proc，
    避免 Linux CI runner 上低 PID（如 1234 被 ``(sd-pam)`` 占用）读到真实进程。
    其余路径委托给真实 open（tests 自身文件读写不受影响）。
    """

    real_open = open
    stat_path = f"/proc/{pid}/stat"
    cmdline_path = f"/proc/{pid}/cmdline"

    def _fake_open(file, *args, **kwargs):
        file_str = str(file)
        if file_str == stat_path:
            if stat is None:
                raise FileNotFoundError(2, "No such file or directory", file_str)
            return io.StringIO(stat)
        if file_str == cmdline_path:
            if cmdline is None:
                raise FileNotFoundError(2, "No such file or directory", file_str)
            return io.BytesIO(cmdline)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)


def _make_backend(tmp_path: Path, **kwargs) -> DaemonBackend:
    """构造指向临时目录的 DaemonBackend（默认 dev 路径可用）。"""
    defaults = {
        "state_dir": str(tmp_path / "state"),
        "socket_path": str(tmp_path / "updater.sock"),
        "binary_path": str(tmp_path / "sakura-ai-updater"),
        "startup_timeout": 0.2,
        "stop_timeout": 0.2,
        "poll_interval": 0.01,
    }
    defaults.update(kwargs)
    return DaemonBackend(**defaults)


class FakePopen:
    """Minimal Popen stub: pid/poll/terminate/wait/kill 契约（计划 Step 4 要求）。"""

    def __init__(self, pid: int = 9999, poll_returncode: int | None = None):
        self.pid = pid
        self._poll_returncode = poll_returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._poll_returncode

    def terminate(self) -> None:
        self.terminated = True
        if self._poll_returncode is None:
            self._poll_returncode = -15

    def kill(self) -> None:
        self.killed = True
        self._poll_returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._poll_returncode is not None:
            return self._poll_returncode
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


def _patch_popen(monkeypatch, child: FakePopen, calls: list | None = None):
    """Patch subprocess.Popen to return `child` (optionally recording call args)."""

    def _fake_popen(*args, **kwargs):
        if calls is not None:
            calls.append(args)
        return child

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen)


def _patch_euid(monkeypatch, uid: int):
    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)


def _patch_root_owned_lstat(monkeypatch):
    """所有平台都将被测 inode 模拟为 root-owned；Windows 补充 POSIX mode。"""
    real_lstat = daemon_mod.os.lstat

    def fake_lstat(path):
        result = real_lstat(path)
        if stat.S_ISDIR(result.st_mode):
            mode = stat.S_IFDIR | 0o755
        else:
            mode = stat.S_IFREG | 0o700
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(daemon_mod.os, "lstat", fake_lstat)


def _patch_target_owned_lstat(monkeypatch, target: Path, uid: int):
    """只将指定目标 inode 显式模拟为给定 owner，保留真实 mode。"""
    real_lstat = daemon_mod.os.lstat

    def fake_lstat(path):
        result = real_lstat(path)
        if os.fspath(path) != str(target):
            return result
        mode = result.st_mode if result.st_mode & 0o111 else stat.S_IFREG | 0o700
        return SimpleNamespace(st_mode=mode, st_uid=uid)

    monkeypatch.setattr(daemon_mod.os, "lstat", fake_lstat)


def _patch_trusted_path_tree(
    monkeypatch,
    *,
    file_modes: dict[Path, int],
    overrides: dict[Path, tuple[int, int]] | None = None,
):
    """把临时目录映射为 Linux root-owned 安全路径，可精确注入不安全 inode。"""

    real_lstat = daemon_mod.os.lstat
    normalized_modes = {os.path.abspath(path): mode for path, mode in file_modes.items()}
    normalized_overrides = {
        os.path.abspath(path): value for path, value in (overrides or {}).items()
    }

    def fake_lstat(path):
        absolute = os.path.abspath(path)
        if absolute in normalized_overrides:
            mode, uid = normalized_overrides[absolute]
            return SimpleNamespace(st_mode=mode, st_uid=uid)
        result = real_lstat(path)
        if stat.S_ISDIR(result.st_mode):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        mode = normalized_modes.get(absolute, 0o700)
        return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0)

    monkeypatch.setattr(daemon_mod.os, "lstat", fake_lstat)


def _patch_same_process_primitives(monkeypatch, *, alive=True, starttime="555666", argv=()):
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: alive)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: starttime)
    monkeypatch.setattr(daemon_mod, "_read_proc_cmdline", lambda pid: argv)


# =============================================================================
# 常量
# =============================================================================


def test_module_constants_match_spec():
    assert DEFAULT_BINARY_NAME == "sakura-ai-updater"
    assert DEFAULT_SOCKET_PATH == "/run/sakura-ai/updater.sock"
    assert DEFAULT_RUN_DIR == "/run/sakura-ai"
    assert DEFAULT_GID == 9472
    assert DEFAULT_GROUP == "sakura-ai"
    assert DEFAULT_STARTUP_TIMEOUT == 5.0


def test_module_exports_error_types():
    assert issubclass(UpdaterNotInstalledError, RuntimeError)
    assert issubclass(UpdaterStartError, RuntimeError)
    assert issubclass(GIDConflictError, RuntimeError)
    assert issubclass(PrivilegeError, RuntimeError)
    assert issubclass(UnsafeDeploymentPathError, RuntimeError)


# =============================================================================
# /proc 读取（NUL argv / proc stat field 22）
# =============================================================================


def test_read_proc_cmdline_parses_null_separated_argv(monkeypatch):
    cmdline = b"python3\x00-m\x00sakura_ai_updater\x00--serve\x00"
    _patch_proc_open(monkeypatch, 1234, cmdline=cmdline)
    assert _read_proc_cmdline(1234) == (
        "python3",
        "-m",
        "sakura_ai_updater",
        "--serve",
    )


def test_read_proc_cmdline_returns_empty_tuple_on_error(monkeypatch):
    _patch_proc_open(monkeypatch, 1234)  # /proc/1234/cmdline → FileNotFoundError
    assert _read_proc_cmdline(1234) == ()


def test_read_proc_starttime_extracts_field_22(monkeypatch):
    # comm 无括号：去掉 pid/comm 后 fields[19] 即 field 22 starttime
    _patch_proc_open(monkeypatch, 1234, stat=STAT_LINE)
    assert _read_proc_starttime(1234) == "555666"


def test_read_proc_starttime_handles_comm_with_spaces_and_parens(monkeypatch):
    # comm 可含空格/括号（如 "cpu (2) heavy"）；按最后一个 ')' 截断后取 fields[19]
    stat = (
        "1234 (cpu (2) heavy) S 0 1234 1234 0 -1 4194304 100 0 0 0 1 0 0 0 "
        "20 0 1 0 888999 100000 0 0 0 0 0 0 0 0 0"
    )
    _patch_proc_open(monkeypatch, 1234, stat=stat)
    assert _read_proc_starttime(1234) == "888999"


def test_read_proc_starttime_returns_none_on_error(monkeypatch):
    # patch pid 1234 但不提供 stat → /proc/1234/stat 显式 FileNotFoundError（不依赖真实 /proc）
    _patch_proc_open(monkeypatch, 1234)
    assert _read_proc_starttime(1234) is None


# =============================================================================
# _matches_identity
# =============================================================================


def test_matches_identity_dev_module_exact_adjacent_args():
    # identity 'sakura_ai_updater' 只匹配精确相邻的 ("-m", "sakura_ai_updater")（argv[1:3]）
    assert _matches_identity(("python", "-m", "sakura_ai_updater", "--serve"), "sakura_ai_updater")
    assert _matches_identity(("python", "-m", "sakura_ai_updater"), "sakura_ai_updater")
    assert _matches_identity(("python", "-m", "sakura_ai_updater", "x"), "sakura_ai_updater")
    assert not _matches_identity(("python", "sakura_ai_updater"), "sakura_ai_updater")
    assert not _matches_identity(("python", "-m", "other_module"), "sakura_ai_updater")
    assert not _matches_identity(("python", "-m", "sakura_ai_updater_x"), "sakura_ai_updater")
    assert not _matches_identity((), "sakura_ai_updater")


def test_matches_identity_binary_basename_only():
    # identity 'sakura-ai-updater' 只匹配 argv[0] 的 basename
    assert _matches_identity(("/srv/.deploy/updater/sakura-ai-updater",), "sakura-ai-updater")
    assert not _matches_identity(("/usr/bin/sakura-ai-updater.bak",), "sakura-ai-updater")
    assert not _matches_identity(("python", "-m", "sakura_ai_updater"), "sakura-ai-updater")
    assert not _matches_identity((), "sakura-ai-updater")


def test_matches_identity_unknown_identity_never_matches():
    assert not _matches_identity(("whatever",), "unknown-identity")


# =============================================================================
# _is_same_process（alive + starttime + identity 三重校验）
# =============================================================================


def test_is_same_process_true_when_all_checks_pass(monkeypatch):
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("python", "-m", "sakura_ai_updater"),
    )
    assert _is_same_process(1234, "555666", "sakura_ai_updater") is True


def test_is_same_process_false_when_pid_dead(monkeypatch):
    _patch_same_process_primitives(monkeypatch, alive=False)
    assert _is_same_process(1234, "555666", "sakura_ai_updater") is False


def test_is_same_process_false_when_starttime_mismatch(monkeypatch):
    _patch_same_process_primitives(monkeypatch, starttime="999999")
    assert _is_same_process(1234, "555666", "sakura_ai_updater") is False


def test_is_same_process_false_when_identity_mismatch(monkeypatch):
    _patch_same_process_primitives(monkeypatch, argv=("other", "-m", "different"))
    assert _is_same_process(1234, "555666", "sakura_ai_updater") is False


def test_is_same_process_false_when_expected_fields_empty(monkeypatch):
    _patch_same_process_primitives(monkeypatch)
    assert _is_same_process(1234, "", "sakura_ai_updater") is False
    assert _is_same_process(1234, "555666", "") is False


# =============================================================================
# PID meta 原子写 / fail-closed 读 / is_running
# =============================================================================


def test_write_pid_meta_persists_three_fields(tmp_path):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura-ai-updater")
    data = json.loads(Path(backend._pid_meta_path).read_text(encoding="utf-8"))
    assert data == {"pid": 1234, "starttime": "555666", "identity": "sakura-ai-updater"}


def test_write_pid_meta_rejects_empty_fields(tmp_path):
    backend = _make_backend(tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        backend._write_pid_meta(1234, "", "sakura-ai-updater")
    with pytest.raises(ValueError, match="non-empty"):
        backend._write_pid_meta(1234, "555666", "")
    assert not os.path.exists(backend._pid_meta_path)  # 拒绝后不留残留文件


def test_write_pid_meta_is_atomic_replace(tmp_path, monkeypatch):
    """写入必须是 temp + fsync + os.replace（原子）：monkeypatch os.replace 验证调用链。"""
    backend = _make_backend(tmp_path)
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(daemon_mod.os, "replace", lambda src, dst: calls.append((src, dst)) or real_replace(src, dst))
    monkeypatch.setattr(daemon_mod.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    backend._write_pid_meta(1, "111", "sakura_ai_updater")
    srcs = [c[0] for c in calls if isinstance(c, tuple) and c[0] != "fsync"]
    assert srcs and os.path.basename(srcs[0]).startswith(".")  # temp 文件（带 . 前缀）
    assert Path(backend._pid_meta_path).exists()


def test_read_pid_meta_none_when_file_absent(tmp_path):
    assert _make_backend(tmp_path)._read_pid_meta() is None


def test_read_pid_meta_fail_closed_on_corrupt_or_wrong_shape(tmp_path):
    backend = _make_backend(tmp_path)
    os.makedirs(backend.state_dir, exist_ok=True)
    meta_path = Path(backend._pid_meta_path)
    for content in (
        "{not json",
        "[1, 2, 3]",
        "{}",
        '{"pid": 1234}',
        '{"pid": "not-int", "starttime": "1", "identity": "sakura-ai-updater"}',
        '{"pid": 1234, "starttime": 555, "identity": "sakura-ai-updater"}',
        '{"pid": 1234, "starttime": "1", "identity": 42}',
        '{"pid": true, "starttime": "1", "identity": "sakura_ai_updater"}',  # bool 是 int 子类
    ):
        meta_path.write_text(content, encoding="utf-8")
        assert backend._read_pid_meta() is None, f"should fail closed for: {content}"


def test_is_running_false_without_meta(tmp_path):
    assert _make_backend(tmp_path).is_running() is False


def test_is_running_false_for_dead_process(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(monkeypatch, alive=False)
    assert backend.is_running() is False


def test_is_running_true_for_dev_module_identity(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("python", "-m", "sakura_ai_updater", "--serve"),
    )
    assert backend.is_running() is True


def test_is_running_true_for_binary_identity(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura-ai-updater")
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("/srv/.deploy/updater/sakura-ai-updater", "--serve"),
    )
    assert backend.is_running() is True


def test_is_running_false_when_cmdline_identity_mismatch(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("/usr/bin/unrelated",),
    )
    assert backend.is_running() is False


def test_is_running_false_when_starttime_mismatch(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(monkeypatch, alive=True, starttime="777777")
    assert backend.is_running() is False


# =============================================================================
# _resolve_executable（binary 优先 / dev override / 不存在）
# =============================================================================


def test_resolve_executable_uses_binary_when_executable(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    argv, identity = backend._resolve_executable()
    assert argv == [str(binary)]
    assert identity == "sakura-ai-updater"


def test_resolve_executable_rejects_symlink_even_when_target_is_executable(tmp_path, monkeypatch):
    target = tmp_path / "real-updater"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link = tmp_path / "sakura-ai-updater"
    link.symlink_to(target)
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = _make_backend(tmp_path, binary_path=str(link))
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()


def test_resolve_executable_rejects_group_or_other_writable_binary(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o775)
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()


def test_resolve_executable_rejects_non_root_owner_in_production(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_target_owned_lstat(monkeypatch, binary, 1000)
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()


def test_resolve_executable_dev_fallback_ignores_unsafe_existing_binary(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_target_owned_lstat(monkeypatch, binary, 1000)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    backend = _make_backend(tmp_path, binary_path=str(binary))
    argv, identity = backend._resolve_executable()
    assert argv == [sys.executable, "-m", "sakura_ai_updater"]
    assert identity == "sakura_ai_updater"


def test_safe_executable_requires_regular_root_owned_private_executable(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    _patch_root_owned_lstat(monkeypatch)
    assert daemon_mod._is_safe_executable(str(binary), require_root_owner=True)
    assert daemon_mod._is_safe_executable(str(binary), require_root_owner=False)


def test_resolve_executable_binary_wins_over_dev_override(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    backend = _make_backend(tmp_path, binary_path=str(binary))
    argv, identity = backend._resolve_executable()
    assert argv == [str(binary)]
    assert identity == "sakura-ai-updater"


def test_resolve_executable_rejects_non_executable_binary(tmp_path, monkeypatch):
    """binary 无 execute permission → 不作为 production executable。

    Windows 无 execute 位概念（os.access 恒 True），故 monkeypatch os.access 模拟
    X_OK 失败，保证跨平台语义一致。
    """
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)
    monkeypatch.setattr(daemon_mod.os, "access", lambda path, mode: False)
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()


def test_resolve_executable_dev_override_without_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    backend = _make_backend(tmp_path)
    argv, identity = backend._resolve_executable()
    assert argv == [sys.executable, "-m", "sakura_ai_updater"]
    assert identity == "sakura_ai_updater"


def test_resolve_executable_raises_when_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = _make_backend(tmp_path)
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()


# =============================================================================
# start()：readiness gate、meta 时机、清理、root
# =============================================================================


def test_start_is_idempotent_when_already_running(tmp_path, monkeypatch):
    """已运行 → 幂等返回，不再 spawn 新 child。"""
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("python", "-m", "sakura_ai_updater"),
    )
    popen_calls = []
    _patch_popen(monkeypatch, FakePopen(), calls=popen_calls)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)
    backend.start()
    assert popen_calls == []


def test_start_requires_root_for_production_binary(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    _patch_euid(monkeypatch, uid=1000)  # 非 root
    with pytest.raises(PrivilegeError, match="root"):
        backend.start()
    assert not os.path.exists(backend._pid_meta_path)


def test_start_allows_root_for_production_binary(tmp_path, monkeypatch):
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    _patch_euid(monkeypatch, uid=0)  # root
    child = FakePopen(pid=4242)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)
    backend.start()
    data = json.loads(Path(backend._pid_meta_path).read_text(encoding="utf-8"))
    assert data == {"pid": 4242, "starttime": "555666", "identity": "sakura-ai-updater"}


def _production_paths(tmp_path: Path) -> tuple[Path, Path, Path, DaemonBackend]:
    binary = tmp_path / ".deploy" / "updater" / "sakura-ai-updater"
    compose = tmp_path / "docker" / "docker-compose.prod.yml"
    deployment_env = tmp_path / ".deploy" / "deployment.env"
    for path in (binary, compose, deployment_env):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test\n", encoding="utf-8")
    backend = _make_backend(
        tmp_path,
        binary_path=str(binary),
        compose_file=str(compose),
        deployment_env=str(deployment_env),
    )
    return binary, compose, deployment_env, backend


def test_validate_production_paths_accepts_trusted_root_tree(tmp_path, monkeypatch):
    binary, compose, deployment_env, backend = _production_paths(tmp_path)
    _patch_trusted_path_tree(
        monkeypatch,
        file_modes={binary: 0o700, compose: 0o644, deployment_env: 0o600},
    )

    backend._validate_production_paths()

    assert backend.binary_path == os.path.abspath(binary)
    assert backend.compose_file == os.path.abspath(compose)
    assert backend.deployment_env == os.path.abspath(deployment_env)


@pytest.mark.parametrize(
    ("target_name", "unsafe_mode", "unsafe_uid", "message"),
    [
        ("compose", stat.S_IFREG | 0o644, 1000, "owned by root"),
        ("compose", stat.S_IFREG | 0o666, 0, "group/other writable"),
        ("compose", stat.S_IFLNK | 0o777, 0, "regular file"),
        ("compose_parent", stat.S_IFDIR | 0o777, 0, "parent"),
        ("deployment_env", stat.S_IFREG | 0o644, 0, "mode 0600"),
    ],
)
def test_validate_production_paths_rejects_untrusted_inputs(
    tmp_path,
    monkeypatch,
    target_name,
    unsafe_mode,
    unsafe_uid,
    message,
):
    binary, compose, deployment_env, backend = _production_paths(tmp_path)
    targets = {
        "compose": compose,
        "compose_parent": compose.parent,
        "deployment_env": deployment_env,
    }
    _patch_trusted_path_tree(
        monkeypatch,
        file_modes={binary: 0o700, compose: 0o644, deployment_env: 0o600},
        overrides={targets[target_name]: (unsafe_mode, unsafe_uid)},
    )

    with pytest.raises(UnsafeDeploymentPathError, match=message):
        backend._validate_production_paths()


def test_start_dev_mode_does_not_require_trusted_production_paths(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    monkeypatch.setattr(
        backend,
        "_validate_production_paths",
        lambda: pytest.fail("dev start must not validate production deployment paths"),
    )
    child = FakePopen(pid=4242)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)

    backend.start()


def test_start_fails_when_child_exits_immediately(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")  # dev 模式（本测试不关心 resolver）
    child = FakePopen(pid=4242, poll_returncode=1)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    with pytest.raises(UpdaterStartError):
        backend.start()
    assert not os.path.exists(backend._pid_meta_path)  # 失败不写 meta


def test_start_does_not_write_meta_before_ready(tmp_path, monkeypatch):
    """health 一直失败 → 超时；meta 绝不先写；child 经 Popen 实例清理。"""
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    child = FakePopen(pid=4242)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: False)
    with pytest.raises(UpdaterStartError):
        backend.start()
    assert not os.path.exists(backend._pid_meta_path)
    assert child.terminated is True  # terminate → bounded wait 清理路径


def test_start_fails_when_starttime_unavailable(tmp_path, monkeypatch):
    """starttime 一直不可用（/proc 尚未填充）→ 超时抛 UpdaterStartError。"""
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    child = FakePopen(pid=4242)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: None)
    with pytest.raises(UpdaterStartError):
        backend.start()
    assert not os.path.exists(backend._pid_meta_path)
    assert child.terminated is True


def test_start_writes_meta_only_after_health_ready(tmp_path, monkeypatch):
    """health 先 False 后 True → ready 前无 meta，ready 后才原子写三字段。"""
    backend = _make_backend(tmp_path, poll_interval=0.005)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    child = FakePopen(pid=4242)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    health_calls = []
    monkeypatch.setattr(
        backend, "_health_ready",
        lambda *a: health_calls.append(True) or (len(health_calls) >= 2),
    )
    backend.start()
    assert len(health_calls) >= 2  # 至少探测两次，证明是"就绪后才写"
    data = json.loads(Path(backend._pid_meta_path).read_text(encoding="utf-8"))
    assert data == {"pid": 4242, "starttime": "555666", "identity": "sakura_ai_updater"}
    assert child.terminated is False  # 成功路径不清理 child


def test_start_cleans_child_and_reports_log_path_on_failure(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    child = FakePopen(pid=4242, poll_returncode=7)
    _patch_popen(monkeypatch, child)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    with pytest.raises(UpdaterStartError) as excinfo:
        backend.start()
    assert "updater.log" in str(excinfo.value)  # 错误带 log path
    assert not os.path.exists(backend._pid_meta_path)


def test_start_raises_updater_start_error_on_popen_oserror(tmp_path, monkeypatch):
    """Popen 抛 OSError（EACCES/EMFILE）→ UpdaterStartError 含 log path，不裸传播。"""
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")

    def _boom_popen(*a, **kw):
        raise PermissionError("simulated EACCES")

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _boom_popen)
    with pytest.raises(UpdaterStartError, match="cannot spawn updater") as excinfo:
        backend.start()
    assert "updater.log" in str(excinfo.value)


# =============================================================================
# _health_ready：probe timeout 与 startup_timeout 耦合
# =============================================================================


def test_health_ready_timeout_capped_by_startup_timeout(tmp_path, monkeypatch):
    """_health_ready 的 probe timeout 取 min(2.0, startup_timeout)，不与总超时脱钩。"""
    backend = _make_backend(tmp_path, startup_timeout=0.5)
    captured = []

    class FakeSocket:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            captured.append(t)

        def connect(self, path):
            raise OSError("connection refused")

        def sendall(self, data):
            pass

        def recv(self, size):
            return b""

    monkeypatch.setattr(daemon_mod.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(daemon_mod.socket, "SOCK_STREAM", 1, raising=False)
    monkeypatch.setattr(daemon_mod.socket, "socket", lambda *a, **kw: FakeSocket())
    backend._health_ready("/fake/path")
    assert captured == [0.5]  # min(2.0, 0.5) = 0.5


# =============================================================================
# _cleanup_child：terminate 后 bounded wait 超时 → SIGKILL
# =============================================================================


def test_cleanup_child_sigkills_when_terminate_waits_timeout(tmp_path, monkeypatch):
    """_cleanup_child：terminate 后 bounded wait 超时 → SIGKILL，断言 child.killed。"""
    backend = _make_backend(tmp_path, stop_timeout=0.05)
    child = FakePopen(pid=4242)

    # wait 始终超时（child 不响应 terminate），迫使 _cleanup_child 走 kill 分支
    def _always_timeout(timeout=None):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    monkeypatch.setattr(child, "wait", _always_timeout)
    backend._cleanup_child(child)
    assert child.terminated is True
    assert child.killed is True


# =============================================================================
# stop()：全流程 PID reuse 防御
# =============================================================================


def _make_stop_backend(tmp_path, monkeypatch, *, identity="sakura_ai_updater", pid=1234):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(pid, "555666", identity)
    kills = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda p, s: kills.append((p, s)) or None)
    return backend, kills


def test_stop_without_meta_is_noop(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    kills = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda p, s: kills.append((p, s)) or None)
    backend.stop()
    assert kills == []


def test_stop_with_bad_meta_is_noop(tmp_path, monkeypatch):
    """坏 meta 无法指导信号 → 幂等清理（删除损坏文件），不发信号。"""
    backend = _make_backend(tmp_path)
    os.makedirs(backend.state_dir, exist_ok=True)
    Path(backend._pid_meta_path).write_text("{corrupt", encoding="utf-8")
    kills = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda p, s: kills.append((p, s)) or None)
    backend.stop()
    assert kills == []
    assert not os.path.exists(backend._pid_meta_path)


def test_stop_never_signals_initial_identity_mismatch(tmp_path, monkeypatch):
    """初始 _is_same_process=False（身份已变化）→ 原进程已退出，绝不发任何信号。"""
    backend, kills = _make_stop_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: False)
    backend.stop()
    assert kills == []
    assert not os.path.exists(backend._pid_meta_path)  # meta 已清


def test_stop_sigterm_then_no_sigkill_when_pid_reused(tmp_path, monkeypatch):
    """初次一致 → SIGTERM；随后 identity 变化（PID reuse）→ 不再发任何信号（含 SIGKILL）。"""
    backend, kills = _make_stop_backend(tmp_path, monkeypatch)
    same = iter([True, False])
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: next(same))
    backend.stop()
    assert kills == [(1234, SIGTERM)]
    assert not os.path.exists(backend._pid_meta_path)


def test_stop_sigkills_only_when_same_process_survives_timeout(tmp_path, monkeypatch):
    """SIGTERM 后超时仍为同一进程 → 才 SIGKILL。"""
    backend, kills = _make_stop_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "_SIGKILL", SIGKILL)  # Windows 无 SIGKILL，注入
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)  # 始终同进程
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)  # SIGTERM 无效，进程仍存活
    backend.stop()
    assert SIGTERM in [s for _, s in kills]
    assert SIGKILL in [s for _, s in kills]
    assert kills[-1] == (1234, SIGKILL)  # 最后一发必是 SIGKILL
    assert not os.path.exists(backend._pid_meta_path)


def test_stop_process_lookup_error_cleans_meta_no_further_signal(tmp_path, monkeypatch):
    """SIGTERM 时 os.kill 抛 ProcessLookupError → 视为已退出，清 meta，不再发信号。"""
    backend, kills = _make_stop_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)

    def _kill_not_found(p, s):
        raise ProcessLookupError(f"No process {p}")

    monkeypatch.setattr(daemon_mod.os, "kill", _kill_not_found)
    backend.stop()
    assert kills == []  # 无信号成功发出
    assert not os.path.exists(backend._pid_meta_path)


def test_stop_raises_clear_error_on_os_kill_permission_error(tmp_path, monkeypatch):
    """os.kill 抛 PermissionError → 清 meta 后转抛 UpdaterStartError（CLI 单行 ERROR 契约）。"""
    backend, _kills = _make_stop_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)

    def _kill_perm_error(p, s):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(daemon_mod.os, "kill", _kill_perm_error)
    with pytest.raises(UpdaterStartError, match="cannot stop"):
        backend.stop()
    assert not os.path.exists(backend._pid_meta_path)


# =============================================================================
# status()
# =============================================================================


def test_status_reports_not_running_without_meta(tmp_path):
    backend = _make_backend(tmp_path)
    status = backend.status()
    assert status["running"] is False
    assert status["pid"] is None
    assert status["socket_path"] == str(tmp_path / "updater.sock")
    assert status["state_dir"] == str(tmp_path / "state")
    assert status["log_path"] == str(tmp_path / "state" / "updater.log")


def test_status_reports_running_with_pid(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    backend._write_pid_meta(1234, "555666", "sakura_ai_updater")
    _patch_same_process_primitives(
        monkeypatch, alive=True, starttime="555666",
        argv=("python", "-m", "sakura_ai_updater"),
    )
    status = backend.status()
    assert status["running"] is True
    assert status["pid"] == 1234


# =============================================================================
# install()（Task 2 完整实现；Task 1 仅确认 CLI 结构存在、root gate 生效）
# =============================================================================


def test_install_is_available_and_requires_root(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    _patch_euid(monkeypatch, uid=1000)
    with pytest.raises(PrivilegeError, match="install"):
        backend.install()


# =============================================================================
# bootstrap（Task 2）：_require_root / ensure_group / ensure_run_dir / install
# =============================================================================


def _completed(returncode, stdout="", stderr=""):
    """构造 subprocess.CompletedProcess（getent/groupadd fake 返回值）。"""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_getent(
    monkeypatch,
    *,
    gid_rc=1,
    gid_out="",
    gid_err="",
    name_rc=1,
    name_out="",
    name_err="",
    groupadd_rc=0,
):
    """Monkeypatch subprocess.run 模拟 getent 双向查询与 groupadd（记录调用序列）。"""

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "getent" and cmd[2] == str(DEFAULT_GID):
            return _completed(gid_rc, gid_out, gid_err)
        if cmd[0] == "getent":
            return _completed(name_rc, name_out, name_err)
        if groupadd_rc != 0:
            raise subprocess.CalledProcessError(groupadd_rc, cmd)
        return _completed(0)

    monkeypatch.setattr(daemon_mod.subprocess, "run", _fake_run)
    return calls


def test_require_root_raises_for_non_root(tmp_path, monkeypatch):
    """非 root 执行需特权动作 → PrivilegeError（含 action 与 sudo 提示）。"""
    backend = _make_backend(tmp_path)
    _patch_euid(monkeypatch, uid=1000)
    with pytest.raises(PrivilegeError, match="requires root privileges"):
        backend._require_root("install")
    _patch_euid(monkeypatch, uid=0)  # root 放行
    backend._require_root("install")


def test_ensure_group_creates_when_name_and_gid_absent(tmp_path, monkeypatch):
    """name 与 GID 都不存在 → groupadd -g 9472 sakura-ai（顺序精确）。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch)
    backend.ensure_group()
    assert calls == [
        ["getent", "group", str(DEFAULT_GID)],
        ["getent", "group", DEFAULT_GROUP],
        ["groupadd", "-g", str(DEFAULT_GID), DEFAULT_GROUP],
    ]


def test_ensure_group_accepts_getent_rc2_as_not_found(tmp_path, monkeypatch):
    """Debian getent missing-key rc=2 → both lookups are absent and groupadd runs."""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, gid_rc=2, name_rc=2)

    backend.ensure_group()

    assert calls == [
        ["getent", "group", str(DEFAULT_GID)],
        ["getent", "group", DEFAULT_GROUP],
        ["groupadd", "-g", str(DEFAULT_GID), DEFAULT_GROUP],
    ]


def test_ensure_group_is_idempotent_when_name_and_gid_match(tmp_path, monkeypatch):
    """name=sakura-ai 且 GID=9472 → 幂等成功，不调 groupadd。"""
    backend = _make_backend(tmp_path)
    out = f"{DEFAULT_GROUP}:x:{DEFAULT_GID}\n"
    calls = _patch_getent(monkeypatch, gid_rc=0, gid_out=out, name_rc=0, name_out=out)
    backend.ensure_group()
    assert calls == [
        ["getent", "group", str(DEFAULT_GID)],
        ["getent", "group", DEFAULT_GROUP],
    ]
    assert not any(c[0] == "groupadd" for c in calls)


def test_ensure_group_rejects_gid_owned_by_other_name(tmp_path, monkeypatch):
    """GID 9472 已属于其他 name → GIDConflictError，不 groupadd。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, gid_rc=0, gid_out="www-data:x:9472\n")
    with pytest.raises(GIDConflictError):
        backend.ensure_group()
    assert calls == [["getent", "group", str(DEFAULT_GID)]]  # 冲突即停，不再查 name


def test_ensure_group_rejects_name_with_other_gid(tmp_path, monkeypatch):
    """name=sakura-ai 存在但 GID != 9472 → GIDConflictError。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, name_rc=0, name_out="sakura-ai:x:9999\n")
    with pytest.raises(GIDConflictError):
        backend.ensure_group()
    assert calls[0] == ["getent", "group", str(DEFAULT_GID)]  # gid 未命中才查 name
    assert not any(c[0] == "groupadd" for c in calls)


def test_ensure_group_propagates_getent_error(tmp_path, monkeypatch):
    """getent 非 0/1/2 退出（NSS 故障）→ 明确 bootstrap error，不静默、不 groupadd。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, gid_rc=3, gid_err="nsswitch: no such provider")
    with pytest.raises(RuntimeError, match="cannot query group"):
        backend.ensure_group()
    assert not any(c[0] == "groupadd" for c in calls)


def test_ensure_group_propagates_getent_name_error(tmp_path, monkeypatch):
    """name 查询 getent 非 0/1/2（NSS 故障）→ RuntimeError。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, name_rc=3, name_err="nss failure")
    with pytest.raises(RuntimeError, match="cannot query group"):
        backend.ensure_group()
    assert not any(c[0] == "groupadd" for c in calls)


def test_ensure_group_propagates_groupadd_failure(tmp_path, monkeypatch):
    """groupadd 失败（CalledProcessError）→ 转抛 RuntimeError 含 'groupadd failed'。"""
    backend = _make_backend(tmp_path)
    calls = _patch_getent(monkeypatch, groupadd_rc=1)
    with pytest.raises(RuntimeError, match="groupadd failed"):
        backend.ensure_group()
    assert calls[-1][0] == "groupadd"


def test_ensure_run_dir_uses_os_chown_root_and_expected_gid(tmp_path, monkeypatch):
    """ensure_run_dir 用 os.chown(path, 0, 9472) + os.chmod(0770)，不调 subprocess。"""
    backend = _make_backend(tmp_path, run_dir=str(tmp_path / "run"))
    chown_calls, chmod_calls = [], []
    monkeypatch.setattr(
        daemon_mod.os, "chown", lambda p, u, g: chown_calls.append((p, u, g)) or None,
        raising=False,  # Windows os 模块无 chown 属性，注入即可
    )
    monkeypatch.setattr(
        daemon_mod.os, "chmod", lambda p, m: chmod_calls.append((p, m)) or None,
        raising=False,
    )
    subprocess_calls = []
    monkeypatch.setattr(
        daemon_mod.subprocess, "run", lambda *a, **k: subprocess_calls.append(a) or _completed(0)
    )
    backend.ensure_run_dir()
    assert chown_calls == [(backend.run_dir, 0, DEFAULT_GID)]
    assert chmod_calls == [(backend.run_dir, 0o770)]
    assert subprocess_calls == []  # 不调 subprocess chown
    assert os.path.isdir(backend.run_dir)  # makedirs 已创建


def test_install_runs_bootstrap_sequence(tmp_path, monkeypatch):
    """install：root gate → ensure_group → ensure_run_dir → makedirs(state_dir)。

    不下载 binary（Slice 3c 负责 binary acquisition）。
    """
    backend = _make_backend(tmp_path, run_dir=str(tmp_path / "run"))
    _patch_euid(monkeypatch, uid=0)  # root 放行
    calls = []
    monkeypatch.setattr(backend, "ensure_group", lambda: calls.append("ensure_group"))
    monkeypatch.setattr(backend, "ensure_run_dir", lambda: calls.append("ensure_run_dir"))
    backend.install()
    assert calls == ["ensure_group", "ensure_run_dir"]
    assert os.path.isdir(backend.state_dir)
    assert not os.path.exists(backend.binary_path)  # 不下载 binary


# =============================================================================
# backend CLI（__main__.py）
# =============================================================================


def _run_main(monkeypatch, capsys, argv):
    import sakura_ai_updater.__main__ as main_mod

    created = {}

    def _factory(*args, **kwargs):
        backend = DaemonBackend(*args, **kwargs)
        created["backend"] = backend
        return backend

    monkeypatch.setattr(main_mod, "create_backend", _factory, raising=False)
    code = main_mod.main(argv)
    return code, created.get("backend"), capsys.readouterr()


def test_cli_backend_status_outputs_json(monkeypatch, capsys, tmp_path):
    code, _backend, captured = _run_main(
        monkeypatch, capsys,
        ["backend", "status", "--state-dir", str(tmp_path / "state")],
    )
    assert code in (None, 0)
    payload = json.loads(captured.out)
    assert payload["running"] is False
    assert "pid" in payload and "socket_path" in payload and "state_dir" in payload


def test_cli_backend_is_running_exit_code(monkeypatch, capsys, tmp_path):
    """is-running 用退出码语义：not running → exit 1，stdout 无输出。"""
    import sakura_ai_updater.__main__ as main_mod

    monkeypatch.setattr(
        main_mod, "create_backend",
        lambda *a, **kw: _make_backend(tmp_path),
    )
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["backend", "is-running", "--state-dir", str(tmp_path / "state")])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""


def test_cli_backend_known_error_single_line_stderr(monkeypatch, capsys, tmp_path):
    """已知 backend 异常 → 单行 ERROR stderr + SystemExit(1)，无 traceback。"""
    import sakura_ai_updater.__main__ as main_mod

    def _boom(*a, **kw):
        raise UpdaterNotInstalledError("updater executable not installed: x")

    monkeypatch.setattr(main_mod, "create_backend", _boom)
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["backend", "start", "--state-dir", str(tmp_path / "state")])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("ERROR:") == 1
    assert "Traceback" not in captured.err
    assert "updater executable not installed" in captured.err


def test_cli_unknown_backend_action_is_error(monkeypatch, capsys, tmp_path):
    import sakura_ai_updater.__main__ as main_mod

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["backend", "frobnicate"])
    assert excinfo.value.code != 0


# =============================================================================
# B1. start() passes --serve and path arguments to child
# =============================================================================


def test_start_dev_mode_passes_serve_args(tmp_path, monkeypatch):
    """dev 模式（SAKURA_UPDATER_DEV=1）：Popen argv 含 --serve/--socket-path/--state-dir/--lock-path/--socket-uid/--socket-gid。

    dev 模式 socket uid/gid 取当前用户（非 root 无法 chown 到 root:9472）。
    """
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    child = FakePopen(pid=4242)
    popen_calls = []
    _patch_popen(monkeypatch, child, calls=popen_calls)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)
    backend.start()
    assert len(popen_calls) == 1
    argv = popen_calls[0][0]
    assert "--serve" in argv
    assert "--socket-path" in argv
    socket_idx = argv.index("--socket-path")
    assert argv[socket_idx + 1] == backend.socket_path
    assert "--state-dir" in argv
    state_idx = argv.index("--state-dir")
    assert argv[state_idx + 1] == backend.state_dir
    assert "--lock-path" in argv
    lock_idx = argv.index("--lock-path")
    assert argv[lock_idx + 1] == os.path.join(backend.state_dir, "updater.lock")
    assert "--socket-uid" in argv
    assert "--socket-gid" in argv


def test_start_binary_mode_passes_serve_args(tmp_path, monkeypatch):
    """binary 模式：Popen argv[0] 是 binary path 且含 --serve 等参数。

    生产模式 socket uid=0、gid=9472（root:sakura-ai）。
    """
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    backend = _make_backend(tmp_path, binary_path=str(binary))
    _patch_euid(monkeypatch, uid=0)
    child = FakePopen(pid=4242)
    popen_calls = []
    popen_kwargs = {}

    def fake_popen(*args, **kwargs):
        popen_calls.append(args)
        popen_kwargs.update(kwargs)
        return child

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)
    backend.start()
    assert len(popen_calls) == 1
    argv = popen_calls[0][0]
    assert argv[0] == str(binary)
    assert "--serve" in argv
    assert "--socket-path" in argv
    assert "--state-dir" in argv
    assert "--lock-path" in argv
    assert "--socket-uid" in argv
    uid_idx = argv.index("--socket-uid")
    assert argv[uid_idx + 1] == "0"
    assert "--socket-gid" in argv
    gid_idx = argv.index("--socket-gid")
    assert argv[gid_idx + 1] == str(daemon_mod.DEFAULT_GID)
    assert popen_kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_start_dev_mode_does_not_force_pyinstaller_reset(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    monkeypatch.setenv("SAKURA_UPDATER_DEV", "1")
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    child = FakePopen(pid=4242)
    popen_kwargs = {}

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return child

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_mod, "_read_proc_starttime", lambda pid: "555666")
    monkeypatch.setattr(daemon_mod, "_is_same_process", lambda pid, st, ident: True)
    monkeypatch.setattr(backend, "_health_ready", lambda *a: True)

    backend.start()

    assert "PYINSTALLER_RESET_ENVIRONMENT" not in popen_kwargs["env"]
