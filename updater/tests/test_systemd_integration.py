"""systemd 真机集成测试（默认 skip）。

启用条件（三者同时满足）：
- 环境变量 ``SAKURA_UPDATER_SYSTEMD_INTEGRATION=1``；
- 以 root 运行且 PID 1 确实是 systemd；
- ``systemctl`` 与 ``systemd-analyze`` 可用。

测试使用随机 unit 名和 ``/var/lib`` 下的 root-owned 临时目录，不触碰正式
``sakura-ai-updater.service`` 或默认 ``/run/sakura-ai``。wrapper 通过显式
venv 的绝对解释器加载 updater，并用一个 venv 同目录的 ``exec -a`` 名称保留
生产 binary identity；``--version`` smoke 会先验证 wrapper 的实际 import。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from sakura_ai_updater.backends.daemon import DaemonBackend
from sakura_ai_updater.systemd import (
    RESTART_SEC,
    install_service,
    uninstall_service,
    unit_install_path,
)

_INTEGRATION_ENV = "SAKURA_UPDATER_SYSTEMD_INTEGRATION"
_SYSTEMCTL = shutil.which("systemctl")
_SYSTEMD_ANALYZE = shutil.which("systemd-analyze")


def _integration_enabled() -> bool:
    if os.environ.get(_INTEGRATION_ENV) != "1":
        return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return False
    if _SYSTEMCTL is None or _SYSTEMD_ANALYZE is None:
        return False
    try:
        pid1 = Path("/proc/1/comm").read_text(encoding="ascii").strip()
        probe = subprocess.run(
            [_SYSTEMCTL, "is-system-running"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    state = probe.stdout.strip()
    # degraded/starting are still real systemd; offline/unknown are not.
    return pid1 == "systemd" and state not in {"", "offline", "unknown"}


pytestmark = pytest.mark.skipif(
    not _integration_enabled(),
    reason=(
        f"requires {_INTEGRATION_ENV}=1 + root + PID1 systemd + systemctl "
        "+ systemd-analyze"
    ),
)


def _systemctl_output(*args: str) -> tuple[int, str]:
    assert _SYSTEMCTL is not None
    proc = subprocess.run(
        [_SYSTEMCTL, *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout.strip()


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _main_pid(unit_name: str) -> int:
    rc, value = _systemctl_output("show", unit_name, "--property=MainPID", "--value")
    assert rc == 0, f"systemd MainPID query failed for {unit_name}: rc={rc}"
    return int(value)


@pytest.fixture()
def harness():
    # /run is commonly mounted noexec.  Keep the isolated root under the
    # persistent root-owned /var/lib tree so the wrapper can execute there.
    root = Path(tempfile.mkdtemp(prefix="sakura-ai-updater-test-", dir="/var/lib"))
    state_dir = root / "state"
    run_dir = root / "run"
    state_dir.mkdir(mode=0o700)
    run_dir.mkdir(mode=0o700)
    binary = root / "sakura-ai-updater"
    argv0 = Path(sys.executable).with_name("sakura-ai-updater")
    binary.write_text(
        "#!/bin/bash\n"
        f"exec -a {shlex.quote(str(argv0))} {shlex.quote(sys.executable)} "
        "-m sakura_ai_updater \"$@\"\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    os.chown(binary, 0, 0)
    unit_name = f"sakura-ai-updater-test-{uuid.uuid4().hex}.service"
    backend = DaemonBackend(
        state_dir=str(state_dir),
        socket_path=str(run_dir / "updater.sock"),
        binary_path=str(binary),
        run_dir=str(run_dir),
    )
    try:
        yield backend, unit_name, root, binary
    finally:
        # 卸载兜底：即使断言中途失败也清干净隔离 unit 与 daemon。
        try:
            uninstall_service(backend, unit_name=unit_name)
        except Exception as exc:
            # Never remove the binary while systemd may still reference it.
            # Keep the root available for recovery and make the cleanup failure
            # visible in the test result instead of silently leaking a unit.
            raise RuntimeError(
                f"systemd integration cleanup failed; preserving harness root "
                f"{root}: {exc}"
            ) from exc
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise RuntimeError(
                f"systemd integration root cleanup failed; preserving harness root "
                f"{root}: {exc}"
            ) from exc


def test_wrapper_imports_checkout_module(harness):
    _backend, _unit_name, _root, binary = harness
    proc = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


def test_service_lifecycle_restart_and_crash_recovery(harness):
    backend, unit_name, root, _binary = harness

    # 1. 安装 → unit active，先验证 systemd-analyze 能解析渲染产物。
    install_service(backend, unit_name=unit_name)
    unit_path = Path(unit_install_path(unit_name=unit_name))
    assert unit_path.is_file()
    assert _SYSTEMD_ANALYZE is not None
    verify = subprocess.run(
        [_SYSTEMD_ANALYZE, "verify", str(unit_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr

    rc, active = _systemctl_output("is-active", unit_name)
    assert rc == 0, f"unit should be active, got rc={rc}"
    assert active == "active"
    assert _wait_until(backend.is_running), "daemon should be running per meta"
    status = backend.status()
    assert status["running"] is True
    pid_file = Path(backend.pid_file_path)
    assert pid_file.read_text(encoding="ascii").strip() == str(status["pid"])
    file_stat = pid_file.stat()
    assert file_stat.st_uid == 0 and file_stat.st_gid == 0
    assert file_stat.st_mode & 0o777 == 0o600
    tmp_stat = Path(backend.runtime_tmp_path).stat()
    assert tmp_stat.st_uid == 0 and tmp_stat.st_gid == 0
    assert tmp_stat.st_mode & 0o777 == 0o700
    assert _main_pid(unit_name) == status["pid"]
    first_pid = status["pid"]

    # 2. 模拟 /run 重建：停止后删除隔离 runtime 目录，再由 ExecStartPre 重建。
    subprocess.run([_SYSTEMCTL, "stop", unit_name], check=True)
    assert _wait_until(lambda: not backend.is_running())
    shutil.rmtree(root / "run")
    assert not (root / "run").exists()
    install_service(backend, unit_name=unit_name)
    assert (root / "run").is_dir()
    assert _wait_until(backend.is_running)
    recreated_pid = backend.status()["pid"]
    assert recreated_pid != first_pid
    assert _wait_until(lambda: backend._health_ready(backend.socket_path))
    assert _main_pid(unit_name) == recreated_pid

    # 3. systemctl restart → 新 pid，并且 socket health 已恢复。
    subprocess.run([_SYSTEMCTL, "restart", unit_name], check=True)
    assert _wait_until(
        lambda: backend.status().get("pid") not in (None, recreated_pid)
    ), "restart should produce a new daemon pid"
    restarted_pid = backend.status()["pid"]
    assert _wait_until(lambda: backend._health_ready(backend.socket_path))
    assert _main_pid(unit_name) == restarted_pid

    # 4. kill -9 → Restart=on-failure 自动拉回，socket health 恢复。
    killed_pid = restarted_pid
    os.kill(killed_pid, 9)
    assert _wait_until(
        lambda: backend.status().get("pid") not in (None, killed_pid),
        timeout=RESTART_SEC + 15,
    ), "Restart=on-failure should pull the daemon back after SIGKILL"
    assert _wait_until(backend.is_running)
    assert _wait_until(lambda: backend._health_ready(backend.socket_path))
    recovered_pid = backend.status()["pid"]
    assert _main_pid(unit_name) == recovered_pid
    assert pid_file.read_text(encoding="ascii").strip() == str(recovered_pid)

    # 5. 卸载 → unit 文件删除、daemon 停止、inactive。
    uninstall_service(backend, unit_name=unit_name)
    assert not unit_path.exists()
    rc, _ = _systemctl_output("is-active", unit_name)
    assert rc != 0
    assert not backend.is_running()
    assert not pid_file.exists()


def test_service_install_is_idempotent(harness):
    backend, unit_name, _root, _binary = harness
    install_service(backend, unit_name=unit_name)
    install_service(backend, unit_name=unit_name)  # 重复执行不重启 active daemon
    rc, _ = _systemctl_output("is-active", unit_name)
    assert rc == 0
