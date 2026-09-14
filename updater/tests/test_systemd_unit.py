"""systemd P0.5 集成单测：unit 渲染契约、单一来源、install/uninstall 生命周期。

All systemctl invocations are monkeypatched (no real systemd needed); the
root-owned binary check is faked the same way as test_daemon_backend.py.
"""

from __future__ import annotations

import stat as stat_mod
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sakura_ai_updater import __main__ as main_mod
from sakura_ai_updater import systemd as systemd_mod
from sakura_ai_updater.backends import daemon as daemon_mod
from sakura_ai_updater.backends.daemon import (
    DEFAULT_STARTUP_TIMEOUT,
    DaemonBackend,
    PrivilegeError,
    backend_cli_flags,
    pid_file_path,
)
from sakura_ai_updater.systemd import (
    RESTART_SEC,
    START_LIMIT_BURST,
    START_LIMIT_INTERVAL_SEC,
    TIMEOUT_START_SEC,
    TIMEOUT_STOP_SEC,
    UNIT_NAME,
    ServiceError,
    install_service,
    render_unit,
    uninstall_service,
    unit_install_path,
)


def _patch_root_owned_lstat(monkeypatch):
    """所有平台将被测 inode 模拟为 root-owned；Windows 补充 POSIX mode。"""
    real_lstat = daemon_mod.os.lstat

    def fake_lstat(path):
        result = real_lstat(path)
        # Preserve the real mode so secure-directory tests can observe chmod,
        # while replacing ownership with root for the production fixture.
        mode = result.st_mode
        if stat_mod.S_ISDIR(mode) and mode & 0o022:
            # pytest's /tmp parent is intentionally 1777; model the isolated
            # root-trusted fixture as a private 0755 ancestor.
            mode = stat_mod.S_IFDIR | 0o755
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(daemon_mod.os, "lstat", fake_lstat)


def _make_production_backend(
    tmp_path: Path, monkeypatch, **kwargs
) -> DaemonBackend:
    """构造带 root-owned binary 的生产形态 backend（compose/env 可选）。"""
    binary = tmp_path / "sakura-ai-updater"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    _patch_root_owned_lstat(monkeypatch)
    defaults = {
        "state_dir": str(tmp_path / "state"),
        "socket_path": str(tmp_path / "run" / "updater.sock"),
        "binary_path": str(binary),
    }
    defaults.update(kwargs)
    return DaemonBackend(**defaults)


def _directive(unit_text: str, key: str) -> str:
    for line in unit_text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"directive not found: {key}")


# =============================================================================
# 渲染契约
# =============================================================================


def test_render_unit_contains_p05_directives(tmp_path, monkeypatch):
    backend = _make_production_backend(tmp_path, monkeypatch)
    text = render_unit(backend)
    assert _directive(text, "Type") == "forking"
    assert _directive(text, "PIDFile") == backend.pid_file_path
    assert _directive(text, "Restart") == "on-failure"
    assert _directive(text, "RestartSec") == f"{RESTART_SEC}s"
    assert _directive(text, "TimeoutStartSec") == f"{TIMEOUT_START_SEC}s"
    assert _directive(text, "TimeoutStopSec") == f"{TIMEOUT_STOP_SEC}s"
    assert _directive(text, "StartLimitIntervalSec") == str(START_LIMIT_INTERVAL_SEC)
    assert _directive(text, "StartLimitBurst") == str(START_LIMIT_BURST)
    assert _directive(text, "WantedBy") == "multi-user.target"
    assert _directive(text, "Environment") == f"TMPDIR={backend.runtime_tmp_path}"


def test_render_unit_exec_lines_share_single_source(tmp_path, monkeypatch):
    """Exec* 三行 = binary + backend <action> + backend_cli_flags（零漂移契约）。"""
    compose = tmp_path / "docker-compose.prod.yml"
    compose.write_text("x\n", encoding="utf-8")
    compose.chmod(0o644)
    env = tmp_path / "deployment.env"
    env.write_text("x\n", encoding="utf-8")
    env.chmod(0o600)
    backend = _make_production_backend(
        tmp_path,
        monkeypatch,
        compose_file=str(compose),
        deployment_env=str(env),
    )
    flags = backend_cli_flags(
        backend.state_dir,
        backend.socket_path,
        backend.binary_path,
        backend.compose_file,
        backend.deployment_env,
    )
    text = render_unit(backend)
    assert _directive(text, "ExecStartPre").split() == [
        backend.binary_path,
        "backend",
        "install",
        *flags,
    ]
    assert _directive(text, "ExecStart").split() == [
        backend.binary_path,
        "backend",
        "start",
        *flags,
    ]
    assert _directive(text, "ExecStop").split() == [
        backend.binary_path,
        "backend",
        "stop",
        *flags,
    ]


def test_render_unit_preserves_configured_startup_timeout(tmp_path, monkeypatch):
    """service-install 的 --startup-timeout 必须进入 Exec* 三行并抬高 TimeoutStartSec。"""
    backend = _make_production_backend(tmp_path, monkeypatch, startup_timeout=300.0)
    text = render_unit(backend)
    flags = backend_cli_flags(
        backend.state_dir,
        backend.socket_path,
        backend.binary_path,
        backend.compose_file,
        backend.deployment_env,
        backend.startup_timeout,
    )
    assert _directive(text, "ExecStart").split() == [
        backend.binary_path,
        "backend",
        "start",
        *flags,
    ]
    assert "--startup-timeout" in _directive(text, "ExecStartPre").split()
    assert "--startup-timeout" in _directive(text, "ExecStop").split()
    # TimeoutStartSec 派生：max(默认地板, ceil(N)+余量)，否则 systemd 会先杀 Exec。
    assert _directive(text, "TimeoutStartSec") == (
        f"{300 + systemd_mod.TIMEOUT_START_MARGIN_SEC}s"
    )


def test_render_unit_default_startup_timeout_keeps_timeout_floor(tmp_path, monkeypatch):
    """默认 5s：flag 仍显式渲染进 unit，TimeoutStartSec 维持 120s 地板。"""
    backend = _make_production_backend(tmp_path, monkeypatch)
    text = render_unit(backend)
    assert "--startup-timeout" in _directive(text, "ExecStart").split()
    assert str(DEFAULT_STARTUP_TIMEOUT) in _directive(text, "ExecStart").split()
    assert _directive(text, "TimeoutStartSec") == f"{TIMEOUT_START_SEC}s"


def test_render_unit_pidfile_matches_daemon_derivation(tmp_path, monkeypatch):
    """PIDFile= 与 daemon.pid_file_path / 模块级派生三处同源。"""
    backend = _make_production_backend(tmp_path, monkeypatch)
    text = render_unit(backend)
    assert _directive(text, "PIDFile") == pid_file_path(backend.socket_path)
    assert _directive(text, "PIDFile") == backend.pid_file_path


def test_render_unit_rejects_whitespace_paths(tmp_path, monkeypatch):
    backend = _make_production_backend(
        tmp_path, monkeypatch, state_dir=str(tmp_path / "state dir")
    )
    with pytest.raises(ServiceError, match="whitespace"):
        render_unit(backend)


@pytest.mark.parametrize("unsafe", ["$", "'", '"', "\\", ";", "%"])
def test_render_unit_rejects_systemd_exec_syntax_paths(tmp_path, monkeypatch, unsafe):
    backend = _make_production_backend(
        tmp_path, monkeypatch, state_dir=str(tmp_path / f"state{unsafe}path")
    )

    with pytest.raises(ServiceError, match="cannot be rendered"):
        render_unit(backend)


def test_render_unit_rejects_unsafe_unit_name(tmp_path, monkeypatch):
    backend = _make_production_backend(tmp_path, monkeypatch)
    with pytest.raises(ServiceError, match="invalid systemd unit name"):
        install_service(backend, unit_dir=str(tmp_path / "units"), unit_name="../x.service")


def test_render_unit_requires_production_binary(tmp_path, monkeypatch):
    """无 root-owned binary（dev module 模式）→ 拒绝渲染。"""
    monkeypatch.delenv("SAKURA_UPDATER_DEV", raising=False)
    backend = DaemonBackend(
        state_dir=str(tmp_path / "state"),
        socket_path=str(tmp_path / "run" / "updater.sock"),
    )
    with pytest.raises(ServiceError, match="root-owned updater binary"):
        render_unit(backend)


def test_unit_timeout_contract_exceeds_internal_gates():
    """systemd 超时必须大于 backend 内部门（readiness / SIGTERM 窗口）。"""
    assert TIMEOUT_START_SEC > DEFAULT_STARTUP_TIMEOUT
    assert TIMEOUT_STOP_SEC > DEFAULT_STARTUP_TIMEOUT


# =============================================================================
# install / uninstall 生命周期（systemctl 全部 monkeypatch）
# =============================================================================


def _patch_systemctl(monkeypatch, commands: list[list[str]]):
    def _fake_systemctl(*args):
        commands.append(list(args))

    monkeypatch.setattr(systemd_mod, "_systemctl", _fake_systemctl)
    monkeypatch.setattr(systemd_mod, "_systemctl_is_active", lambda unit: False)
    monkeypatch.setattr(systemd_mod, "_systemctl_is_enabled", lambda unit: False)


def test_install_service_writes_enables_starts_idempotently(
    tmp_path, monkeypatch
):
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    unit_dir = str(tmp_path / "units")

    install_service(backend, unit_dir=unit_dir)
    unit_path = Path(unit_install_path(unit_dir))
    assert unit_path.is_file()
    assert stat_mod.S_IMODE(unit_path.stat().st_mode) == 0o644
    assert unit_path.read_text(encoding="utf-8") == render_unit(backend)
    assert commands == [
        ["daemon-reload"],
        ["enable", UNIT_NAME],
        ["start", UNIT_NAME],
    ]

    # 幂等：重复 install 重新渲染 + 相同命令序列
    commands.clear()
    install_service(backend, unit_dir=unit_dir)
    assert commands == [
        ["daemon-reload"],
        ["enable", UNIT_NAME],
        ["start", UNIT_NAME],
    ]


def test_install_service_fsyncs_unit_directory(tmp_path, monkeypatch):
    """os.replace 后必须 fsync unit 目录，断电后 rename 才可恢复。"""
    fsynced: list[str] = []
    monkeypatch.setattr(systemd_mod, "_fsync_directory", fsynced.append)
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    unit_dir = str(tmp_path / "units")

    install_service(backend, unit_dir=unit_dir)

    assert fsynced == [unit_dir]


def test_install_service_fsync_failure_is_service_error(tmp_path, monkeypatch):
    """目录 fsync 失败必须 fail-closed：不得带着未持久化的 rename 报告安装成功。"""

    def _boom(path):
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(systemd_mod, "_fsync_directory", _boom)
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)

    with pytest.raises(ServiceError, match="fsync"):
        install_service(backend, unit_dir=str(tmp_path / "units"))
    assert commands == []


def test_fsync_directory_flushes_real_directory(tmp_path):
    """真实目录 fsync 冒烟：Linux 上正常文件系统不抛错。"""
    systemd_mod._fsync_directory(str(tmp_path))


def test_install_service_requires_root(tmp_path, monkeypatch):
    backend = _make_production_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 1000, raising=False)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    with pytest.raises(PrivilegeError, match="service-install"):
        install_service(backend, unit_dir=str(tmp_path / "units"))
    assert commands == []


def test_install_service_rejects_changed_active_configuration(
    tmp_path, monkeypatch
):
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    unit_dir = str(tmp_path / "units")

    install_service(backend, unit_dir=unit_dir)
    unit_path = Path(unit_install_path(unit_dir))
    original = unit_path.read_text(encoding="utf-8")
    monkeypatch.setattr(systemd_mod, "_systemctl_is_active", lambda unit: True)
    backend.socket_path = str(tmp_path / "different-run" / "updater.sock")

    with pytest.raises(ServiceError, match="changed configuration"):
        install_service(backend, unit_dir=unit_dir)

    assert unit_path.read_text(encoding="utf-8") == original


def test_install_service_rejects_running_manual_daemon(
    tmp_path, monkeypatch
):
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(backend, "is_running", lambda: True)
    unit_dir = str(tmp_path / "units")

    with pytest.raises(ServiceError, match="running outside systemd"):
        install_service(backend, unit_dir=unit_dir)

    assert not Path(unit_install_path(unit_dir)).exists()
    assert commands == []


def test_install_service_active_same_configuration_does_not_start_again(
    tmp_path, monkeypatch
):
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    unit_dir = str(tmp_path / "units")
    install_service(backend, unit_dir=unit_dir)

    commands.clear()
    monkeypatch.setattr(systemd_mod, "_systemctl_is_active", lambda unit: True)
    install_service(backend, unit_dir=unit_dir)

    assert commands == [["daemon-reload"], ["enable", UNIT_NAME]]


def test_uninstall_service_stops_disables_removes_idempotently(
    tmp_path, monkeypatch
):
    backend = _make_production_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    unit_dir = str(tmp_path / "units")
    install_service(backend, unit_dir=unit_dir)

    commands.clear()
    uninstall_service(backend, unit_dir=unit_dir)
    assert not Path(unit_install_path(unit_dir)).exists()
    assert commands == [
        ["stop", UNIT_NAME],
        ["disable", UNIT_NAME],
        ["daemon-reload"],
    ]

    # 幂等：unit 文件已不存在 → 仅 daemon-reload（backend.stop 对无 meta 幂等）
    commands.clear()
    uninstall_service(backend, unit_dir=unit_dir)
    assert commands == [["daemon-reload"]]


def test_uninstall_service_fsyncs_unit_directory_after_removal(tmp_path, monkeypatch):
    """删除 unit 后必须 fsync 目录：卸载方随后删除 binary，断电不得复活悬空 unit。"""
    fsynced: list[str] = []
    monkeypatch.setattr(systemd_mod, "_fsync_directory", fsynced.append)
    backend = _make_production_backend(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _patch_systemctl(monkeypatch, commands)
    monkeypatch.setattr(daemon_mod.os, "geteuid", lambda: 0, raising=False)
    unit_dir = str(tmp_path / "units")
    install_service(backend, unit_dir=unit_dir)

    fsynced.clear()
    commands.clear()
    uninstall_service(backend, unit_dir=unit_dir)

    assert not Path(unit_install_path(unit_dir)).exists()
    assert fsynced == [unit_dir]


def test_systemctl_failure_raises_service_error(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Unit not found"
    )
    monkeypatch.setattr(
        systemd_mod.subprocess, "run", lambda *a, **kw: completed
    )
    with pytest.raises(ServiceError, match="systemctl"):
        systemd_mod._systemctl("enable", UNIT_NAME)


def test_systemctl_oserror_is_service_error(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("systemctl missing")

    monkeypatch.setattr(systemd_mod.subprocess, "run", _boom)
    with pytest.raises(ServiceError, match="OSError"):
        systemd_mod._systemctl("daemon-reload")


def test_systemctl_multiline_stderr_is_single_line(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="first line\nsecond line\n"
    )
    monkeypatch.setattr(systemd_mod.subprocess, "run", lambda *a, **kw: completed)
    with pytest.raises(ServiceError) as excinfo:
        systemd_mod._systemctl("start", UNIT_NAME)
    assert "\n" not in str(excinfo.value)
    assert "first line second line" in str(excinfo.value)


def test_systemctl_transient_active_state_fails_closed(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=3, stdout="activating\n", stderr=""
    )
    monkeypatch.setattr(systemd_mod, "_run_systemctl", lambda *a: completed)
    with pytest.raises(ServiceError, match="is-active"):
        systemd_mod._systemctl_is_active(UNIT_NAME)


# (returncode, state) 组合来自 man systemctl 的 is-enabled 表与本机实测：
# enabled-runtime rc=0；linked/linked-runtime/masked-runtime rc=1。
@pytest.mark.parametrize(
    ("returncode", "state", "expected"),
    [
        (0, "enabled", True),
        # ``systemctl enable --runtime`` 的产物；rc=0 但曾被误判为未启用。
        (0, "enabled-runtime", True),
        (0, "alias", True),
        (0, "generated", True),
        # linked = 通过符号链接可用的 unit（文件在搜索路径之外）；链接存在即
        # 视为已启用，卸载时 disable 才会移除这些链接。
        (1, "linked", True),
        (1, "linked-runtime", True),
        (0, "static", False),
        (1, "disabled", False),
        (1, "masked", False),
        (1, "masked-runtime", False),
        (4, "not-found", False),
    ],
)
def test_systemctl_is_enabled_states(returncode, state, expected, monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=f"{state}\n", stderr=""
    )
    monkeypatch.setattr(systemd_mod, "_run_systemctl", lambda *a: completed)
    assert systemd_mod._systemctl_is_enabled(UNIT_NAME) is expected


def test_systemctl_is_enabled_unknown_state_fails_closed(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="brand-new-state\n", stderr=""
    )
    monkeypatch.setattr(systemd_mod, "_run_systemctl", lambda *a: completed)
    with pytest.raises(ServiceError, match="is-enabled"):
        systemd_mod._systemctl_is_enabled(UNIT_NAME)


# =============================================================================
# CLI dispatch（__main__.py）
# =============================================================================


def test_cli_service_install_dispatches(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(
        main_mod, "create_backend", lambda *a, **kw: _make_production_backend(
            tmp_path, monkeypatch
        )
    )
    monkeypatch.setattr(
        main_mod, "install_service", lambda backend: calls.append(("install", backend))
    )
    main_mod.main(["backend", "service-install"])
    assert [name for name, _ in calls] == ["install"]


def test_cli_service_uninstall_dispatches(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(
        main_mod, "create_backend", lambda *a, **kw: _make_production_backend(
            tmp_path, monkeypatch
        )
    )
    monkeypatch.setattr(
        main_mod,
        "uninstall_service",
        lambda backend: calls.append(("uninstall", backend)),
    )
    main_mod.main(["backend", "service-uninstall"])
    assert [name for name, _ in calls] == ["uninstall"]


def test_cli_service_error_single_line_stderr(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        main_mod, "create_backend", lambda *a, **kw: _make_production_backend(
            tmp_path, monkeypatch
        )
    )

    def _boom(backend, **kwargs):
        raise ServiceError("systemctl start failed (rc=1)")

    monkeypatch.setattr(main_mod, "install_service", _boom)
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["backend", "service-install"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("ERROR:") == 1
    assert "Traceback" not in captured.err
