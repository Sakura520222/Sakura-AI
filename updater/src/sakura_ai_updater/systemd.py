"""systemd 集成：P0.5 unit 渲染与 install/uninstall 生命周期。

设计定案（``docs/superpowers/plans/2026-09-10-updater-systemd-boot-lifecycle.md``）：
``Type=forking`` 复用现有 self-daemonize + readiness gate 模型——``backend start``
等价于传统 forking daemon 的 parent（``_wait_ready`` 通过后才退出 exit 0），systemd
在 ExecStart 退出后经 ``PIDFile=`` 接管 main PID，从而同时获得 reboot 恢复与运行期
崩溃监督（``Restart=on-failure``；SIGHUP/SIGINT/SIGTERM/SIGPIPE 属 clean 信号不触发
重启，故 ``backend stop`` / ``systemctl stop`` 不会被自动拉回）。

仅支持生产 binary 模式（root-owned executable）；dev module 模式拒绝渲染。所有
systemctl 调用经模块级 ``_systemctl`` 间接，测试可整体 monkeypatch。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from sakura_ai_updater.backends.daemon import (
    DaemonBackend,
    UnsafeDeploymentPathError,
    _is_safe_executable,
    backend_cli_flags,
    pid_file_path,
)

UNIT_NAME = "sakura-ai-updater.service"
UNIT_INSTALL_DIR = "/etc/systemd/system"
_UNIT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*\.service\Z")
_UNIT_PATH_UNSAFE_CHARS = frozenset("%$'\\\";")

# 超时契约：必须大于 backend 内部相应门（readiness gate / SIGTERM 等待窗口，均默认
# DEFAULT_STARTUP_TIMEOUT=5s），否则 systemd 会先杀 Exec 命令、打断 CLI 自己的清理
# 阶梯。由 test_systemd_unit.py 的契约测试固化。
# / Must exceed the backend's internal gates; contract-tested.
TIMEOUT_START_SEC = 120
TIMEOUT_STOP_SEC = 60
RESTART_SEC = 5
START_LIMIT_INTERVAL_SEC = 60
START_LIMIT_BURST = 5


class ServiceError(RuntimeError):
    """unit 渲染或 systemctl 生命周期操作失败。"""


def _run_systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run systemctl and translate an unavailable executable into ServiceError."""
    try:
        return subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise ServiceError(
            f"systemctl {' '.join(args)} failed (OSError): {exc}"
        ) from exc


def _systemctl_error(args: tuple[str, ...], proc: subprocess.CompletedProcess) -> ServiceError:
    # ``str.split`` collapses newlines/tabs so the CLI's ``ERROR:`` contract
    # remains one physical line even when systemctl emits a multi-line error.
    stderr = " ".join(str(getattr(proc, "stderr", "") or "").split())
    detail = f": {stderr}" if stderr else ""
    return ServiceError(
        f"systemctl {' '.join(args)} failed (rc={proc.returncode}){detail}"
    )


def _systemctl(*args: str) -> None:
    """执行 systemctl（模块级 seam，测试 monkeypatch 用）；失败转 ServiceError。"""
    proc = _run_systemctl(*args)
    if proc.returncode != 0:
        raise _systemctl_error(args, proc)


def _systemctl_is_active(unit_name: str) -> bool:
    """Return whether a unit is active while tolerating inactive/not-found states."""
    proc = _run_systemctl("is-active", unit_name)
    state = " ".join(str(getattr(proc, "stdout", "") or "").split())
    if proc.returncode == 0:
        if state == "active":
            return True
        raise _systemctl_error(("is-active", unit_name), proc)
    # systemctl uses rc=3 for inactive/failed and rc=4 for unknown/not-found
    # units.  Require both a known state and the expected return code: an empty
    # response or a transient activating/deactivating state may indicate a
    # broken systemd connection or a concurrent lifecycle transition.
    if proc.returncode in (3, 4) and state in {
        "failed",
        "inactive",
        "not-found",
        "unknown",
    }:
        return False
    raise _systemctl_error(("is-active", unit_name), proc)


def _systemctl_is_enabled(unit_name: str) -> bool:
    """Return whether systemd has an enablement link for ``unit_name``."""
    proc = _run_systemctl("is-enabled", unit_name)
    state = " ".join(str(getattr(proc, "stdout", "") or "").split())
    if proc.returncode == 0:
        return state in {"enabled", "linked", "generated", "alias"}
    if proc.returncode in (1, 3, 4) and state in {
        "",
        "disabled",
        "indirect",
        "static",
        "masked",
        "not-found",
        "unknown",
    }:
        return False
    raise _systemctl_error(("is-enabled", unit_name), proc)


def _require_unit_name(unit_name: str) -> str:
    if not isinstance(unit_name, str) or _UNIT_NAME_RE.fullmatch(unit_name) is None:
        raise ServiceError(
            f"invalid systemd unit name {unit_name!r}; expected a simple .service name"
        )
    return unit_name


def _require_unit_safe_path(path: str, label: str) -> None:
    """Reject path syntax that systemd would parse or expand in Exec lines."""
    if not isinstance(path, str) or not path:
        raise ServiceError(f"{label} must be a non-empty path")
    unsafe = next(
        (
            ch
            for ch in path
            if ch.isspace()
            or ch in _UNIT_PATH_UNSAFE_CHARS
            or ord(ch) < 0x20
            or ord(ch) == 0x7F
        ),
        None,
    )
    if unsafe is not None:
        reason = "whitespace" if unsafe.isspace() else f"unsupported character {unsafe!r}"
        raise ServiceError(
            f"{label} cannot be rendered in a systemd unit ({reason}): "
            f"{path!r}"
        )


def _exec_line(binary_path: str, action: str, flags: list[str]) -> str:
    return " ".join([binary_path, "backend", action, *flags])


def render_unit(backend: DaemonBackend) -> str:
    """渲染 P0.5 unit 文本；仅生产 binary 模式（root-owned executable）可用。

    Exec* 三行共用 ``backend_cli_flags`` 单一来源（与手工命令零漂移）；
    ``PIDFile=`` 与 ``DaemonBackend.pid_file_path`` 同一派生。
    """
    if not _is_safe_executable(backend.binary_path, require_root_owner=True):
        raise ServiceError(
            "service installation requires the root-owned updater binary at "
            f"{backend.binary_path!r} (dev module mode cannot be managed by systemd)"
        )
    try:
        # Freeze/validate production deployment inputs before a caller can
        # install and enable a unit that is guaranteed to fail at boot.
        backend._validate_production_paths()
    except UnsafeDeploymentPathError as exc:
        raise ServiceError(str(exc)) from exc
    paths: list[tuple[str, str]] = [
        ("updater binary", backend.binary_path),
        ("state dir", backend.state_dir),
        ("socket path", backend.socket_path),
        ("updater TMPDIR", backend.runtime_tmp_path),
    ]
    if backend.compose_file is not None:
        paths.append(("compose file", backend.compose_file))
    if backend.deployment_env is not None:
        paths.append(("deployment.env", backend.deployment_env))
    for label, path in paths:
        _require_unit_safe_path(path, label)

    flags = backend_cli_flags(
        backend.state_dir,
        backend.socket_path,
        backend.binary_path,
        backend.compose_file,
        backend.deployment_env,
    )
    return "\n".join(
        [
            "# Generated by sakura-ai-updater backend service-install; do not edit.",
            "[Unit]",
            "Description=Sakura-AI Updater (host update daemon)",
            f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}",
            f"StartLimitBurst={START_LIMIT_BURST}",
            "",
            "[Service]",
            "Type=forking",
            f"Environment=TMPDIR={backend.runtime_tmp_path}",
            f"ExecStartPre={_exec_line(backend.binary_path, 'install', flags)}",
            f"ExecStart={_exec_line(backend.binary_path, 'start', flags)}",
            f"ExecStop={_exec_line(backend.binary_path, 'stop', flags)}",
            f"PIDFile={pid_file_path(backend.socket_path)}",
            "Restart=on-failure",
            f"RestartSec={RESTART_SEC}s",
            f"TimeoutStartSec={TIMEOUT_START_SEC}s",
            f"TimeoutStopSec={TIMEOUT_STOP_SEC}s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def unit_install_path(
    unit_dir: str = UNIT_INSTALL_DIR, unit_name: str = UNIT_NAME
) -> str:
    return os.path.join(unit_dir, _require_unit_name(unit_name))


def _read_existing_unit(path: str) -> str | None:
    """Read an existing managed unit, rejecting symlinked/non-regular files."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        raise ServiceError(f"refusing unsafe existing systemd unit path: {path!r}")
    try:
        with open(path, encoding="utf-8") as unit_file:
            return unit_file.read()
    except OSError as exc:
        raise ServiceError(f"cannot read existing systemd unit {path!r}: {exc}") from exc


def install_service(
    backend: DaemonBackend,
    *,
    unit_dir: str = UNIT_INSTALL_DIR,
    unit_name: str = UNIT_NAME,
) -> None:
    """渲染并安装 a systemd unit without interrupting an active managed daemon.

    An active unit with the same rendered configuration is only reloaded/enabled.
    An active unit with changed configuration, or an active daemon whose unit is
    inactive, is rejected so an update job cannot be interrupted implicitly.
    """
    unit_name = _require_unit_name(unit_name)
    backend._require_root("service-install")
    content = render_unit(backend)
    # ExecStartPre repeats this check at boot, but service-install must establish
    # the persistent onefile extraction directory before asking systemd to start.
    backend.ensure_runtime_tmp()
    os.makedirs(unit_dir, exist_ok=True)
    path = unit_install_path(unit_dir, unit_name)
    previous_content = _read_existing_unit(path)
    active = _systemctl_is_active(unit_name)
    if active:
        if previous_content != content:
            raise ServiceError(
                f"refusing to replace active unit {unit_name!r} with changed configuration; "
                "stop it through the deployment maintenance gate first"
            )
        _systemctl("daemon-reload")
        _systemctl("enable", unit_name)
        return
    if backend.is_running():
        raise ServiceError(
            f"refusing to install {unit_name!r}: an updater daemon is running outside "
            "systemd; stop it through the deployment maintenance gate first"
        )

    fd, tmp = tempfile.mkstemp(dir=unit_dir, prefix=f".{unit_name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _systemctl("daemon-reload")
    _systemctl("enable", unit_name)
    _systemctl("start", unit_name)


def uninstall_service(
    backend: DaemonBackend,
    *,
    unit_dir: str = UNIT_INSTALL_DIR,
    unit_name: str = UNIT_NAME,
) -> None:
    """stop → backend.stop()（幂等兜底）→ disable → 删 unit → daemon-reload。

    / Idempotent: unit 文件不存在时仅做 daemon-reload；systemctl stop/disable
    对未加载 unit 天然容忍。
    """
    unit_name = _require_unit_name(unit_name)
    backend._require_root("service-uninstall")
    path = unit_install_path(unit_dir, unit_name)
    has_unit_file = os.path.lexists(path)
    if has_unit_file:
        _read_existing_unit(path)
        _systemctl("stop", unit_name)
    elif _systemctl_is_active(unit_name):
        # A unit loaded from another unit directory (or a removed file with a
        # lingering systemd load state) still needs an explicit stop before we
        # claim uninstall completed.
        _systemctl("stop", unit_name)
    backend.stop()  # unit 未加载/半安装状态下兜底停 daemon（幂等）
    if has_unit_file or _systemctl_is_enabled(unit_name):
        _systemctl("disable", unit_name)
        if has_unit_file:
            try:
                os.remove(path)
            except OSError as exc:
                raise ServiceError(f"cannot remove systemd unit {path!r}: {exc}") from exc
    _systemctl("daemon-reload")
