"""start.sh updater 生命周期分发契约（bash 子进程内 source 后执行）。

覆盖两类回归：
- 用户参数（如 ``--startup-timeout``）必须一路透传到 service-install / 手动 start；
- 生产 stop 必须经 systemctl stop job（裸 backend stop 的 SIGKILL 升格会被
  ``Restart=on-failure`` 视为失败并拉回 daemon），dev/未加载 unit 保留裸 stop。

/ Execution-level contracts for the updater lifecycle dispatch in start.sh,
sourced into a bash subprocess with stubbed seams.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=script.replace("\r\n", "\n").encode("utf-8"),
        capture_output=True,
        check=False,
    )


def test_cmd_updater_start_forwards_options_to_service_install():
    """systemd 模式下 `updater start --startup-timeout N` 必须到达 service-install。"""
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
updater_uses_systemd() { return 0; }
updater_socket_listener_responds() { return 1; }
updater_binary_is_safe() { return 0; }
updater_service_install() { echo "SERVICE_INSTALL:$*" >> "$LOG"; return 0; }
updater_backend() {
    echo "BACKEND:$*" >> "$LOG"
    [[ "$1" == "is-running" ]] && return 1
    return 0
}
cmd_updater start --startup-timeout 300
grep -q '^SERVICE_INSTALL:.*--startup-timeout 300' "$LOG"
grep -q '^BACKEND:install .*--startup-timeout 300' "$LOG"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_updater_start_daemon_manual_mode_forwards_options():
    """非 systemd 手动模式：updater_start_daemon 同样透传用户参数。"""
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
export COMPOSE_FILE="/tmp/sakura-test-compose.yml"
export UPDATER_DEPLOYMENT_ENV_FILE="/tmp/sakura-test.env"
updater_uses_systemd() { return 1; }
select_compose_from_deployment_mode() { return 0; }
updater_backend() { echo "BACKEND:$*" >> "$LOG"; return 0; }
updater_start_daemon --startup-timeout 300
grep -q '^BACKEND:start .*--startup-timeout 300' "$LOG"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cmd_updater_stop_uses_systemctl_when_unit_loaded():
    """生产 + unit 已加载：stop 走 systemctl stop job，不得直连 backend stop。"""
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
updater_uses_systemd() { return 0; }
updater_systemd_unit_is_loaded() { return 0; }
systemctl() { echo "SYSTEMCTL:$*" >> "$LOG"; return 0; }
updater_backend() { echo "BACKEND:$*" >> "$LOG"; return 0; }
cmd_updater stop
grep -q '^SYSTEMCTL:stop ' "$LOG"
# 裸 backend stop 的 SIGKILL 升格会被 Restart=on-failure 拉回，绝不能出现
if grep -q '^BACKEND:stop' "$LOG"; then exit 1; fi
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cmd_updater_stop_falls_back_to_backend_without_systemd():
    """非 systemd（dev/手动 daemon）：stop 保留裸 backend stop 兜底。"""
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
updater_uses_systemd() { return 1; }
systemctl() { echo "SYSTEMCTL:$*" >> "$LOG"; return 0; }
updater_backend() { echo "BACKEND:$*" >> "$LOG"; return 0; }
cmd_updater stop
grep -q '^BACKEND:stop ' "$LOG"
if grep -q '^SYSTEMCTL:' "$LOG"; then exit 1; fi
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr
