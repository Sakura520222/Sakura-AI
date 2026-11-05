"""Refresh the host controller before admitting an application update."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.platform_support import posix_bash

ROOT = Path(__file__).parents[1]
CAPABILITIES = [
    "three-image-transaction-v1",
    "deployment-reconcile-v1",
    "deployment-manifest-v1",
]


def _identity(release: str, revision: str = "a" * 40) -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "updater_version": "0.3.1",
            "build_release": release,
            "build_revision": revision,
            "capabilities": CAPABILITIES,
            "data": {},
        }
    )


HARNESS = r"""
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
updater_current_uid() { printf '0\n'; }
updater_require_idle_deployment() { return 0; }
updater_path_exists() { [[ "$TEST_DISK_MODE" != missing ]]; }
updater_binary_is_safe() { [[ "$TEST_DISK_MODE" == safe ]]; }
updater_installed_identity() { printf '%s\n' "$INSTALLED_IDENTITY"; }
updater_ipc_get() { printf '%s\n' "$RUNNING_IDENTITY"; }
updater_daemon_is_running() { return 0; }
resolve_latest_stable_updater_version() {
    [[ "$TEST_RELEASE_FAILURE" == 0 ]] || return 1
    printf '%s\n' "$TEST_LATEST_RELEASE"
}
updater_prepare_stop() {
    echo MAINTENANCE_GATE
    [[ "$TEST_GATE_FAILURE" == 0 ]]
}
updater_stop_daemon() { echo STOP_DAEMON; }
updater_cancel_stop() { echo CANCEL_STOP; }
ensure_updater_running() {
    echo ENSURE_RUNNING
    if [[ "$TEST_STALE_RUNTIME" == 0 ]]; then RUNNING_IDENTITY="$INSTALLED_IDENTITY"; fi
}
updater_reinstall_release() {
    printf 'UPGRADE:%s\n' "$1"
    [[ "$TEST_UPGRADE_FAILURE" == 0 ]] || return 17
    INSTALLED_IDENTITY="$LATEST_IDENTITY"
    if [[ "$TEST_STALE_RUNTIME" == 0 ]]; then RUNNING_IDENTITY="$INSTALLED_IDENTITY"; fi
}
"""


def _run(
    command: str = "ensure_image_updater_current",
    *,
    installed: str | None = None,
    running: str | None = None,
    **settings: str,
) -> subprocess.CompletedProcess[str]:
    installed = installed if installed is not None else _identity("v3.2.4")
    env = {
        **os.environ,
        "TERM": "dumb",
        "SAKURA_UPDATER_DEV": "0",
        "INSTALLED_IDENTITY": installed,
        "RUNNING_IDENTITY": running if running is not None else installed,
        "LATEST_IDENTITY": _identity("v3.2.5", "b" * 40),
        "TEST_LATEST_RELEASE": "3.2.5",
        "TEST_DISK_MODE": "safe",
        "TEST_RELEASE_FAILURE": "0",
        "TEST_UPGRADE_FAILURE": "0",
        "TEST_STALE_RUNTIME": "0",
        "TEST_GATE_FAILURE": "0",
        **settings,
    }
    result = subprocess.run(
        [posix_bash()],
        cwd=ROOT,
        env=env,
        input=(HARNESS + "\n" + command + "\n").encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=20,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


@pytest.mark.parametrize(
    "installed",
    [
        _identity("v3.2.4"),
        _identity("source"),
        json.dumps({"protocol_version": 1, "capabilities": CAPABILITIES}),
        "invalid JSON",
    ],
)
def test_compatible_protocol_does_not_hide_an_old_or_unknown_release(installed):
    result = _run(installed=installed)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UPGRADE:3.2.5" in result.stdout


@pytest.mark.parametrize("release", ["v3.2.5", "v3.2.6", "v3.10.0"])
def test_current_or_newer_controller_is_not_downloaded_or_downgraded(release):
    result = _run(installed=_identity(release))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ENSURE_RUNNING" in result.stdout
    assert "UPGRADE:" not in result.stdout
    assert "STOP_DAEMON" not in result.stdout


@pytest.mark.parametrize(
    "running", ["", _identity("v3.2.4"), _identity("v3.2.5", "c" * 40)]
)
def test_installed_controller_restarts_a_missing_or_different_daemon(running):
    result = _run(installed=_identity("v3.2.5"), running=running)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.index("MAINTENANCE_GATE") < result.stdout.index("STOP_DAEMON")
    assert result.stdout.index("STOP_DAEMON") < result.stdout.index("ENSURE_RUNNING")
    assert "UPGRADE:" not in result.stdout


def test_release_lookup_failure_does_not_stop_or_replace_controller():
    result = _run(TEST_RELEASE_FAILURE="1")
    assert result.returncode != 0
    assert "UPGRADE:" not in result.stdout
    assert "STOP_DAEMON" not in result.stdout


def test_unsafe_executable_is_rejected_before_release_lookup():
    result = _run(TEST_DISK_MODE="unsafe", TEST_RELEASE_FAILURE="1")
    assert result.returncode == 126
    assert "unsafe updater executable" in result.stderr
    assert "UPGRADE:" not in result.stdout


def test_daemon_restart_retains_the_atomic_active_job_gate():
    result = _run(installed=_identity("v3.2.5"), running="", TEST_GATE_FAILURE="1")
    assert result.returncode != 0
    assert "MAINTENANCE_GATE" in result.stdout
    assert "STOP_DAEMON" not in result.stdout


@pytest.mark.parametrize(
    "failure", [{"TEST_UPGRADE_FAILURE": "1"}, {"TEST_STALE_RUNTIME": "1"}]
)
def test_failed_refresh_never_submits_an_application_update(failure):
    result = _run(
        r"""
require_image_deployment() { :; }
require_idle_image_deployment() { :; }
init_deployment_env() { :; }
read_deployment_value() { printf 'ghcr.io/sakura520222/sakura-ai:v3.2.4\n'; }
updater_ipc_wait_job() { echo WAIT_JOB; }
curl() {
    local output=''
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --output) output="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    echo SUBMIT_UPDATE >&2
    printf '{"data":{"job_id":"upd_test"}}' > "$output"
    printf '202'
}
cmd_update_image
""",
        **failure,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SUBMIT_UPDATE" not in result.stderr
    assert "WAIT_JOB" not in result.stdout


def test_refresh_finishes_before_the_update_is_submitted():
    result = _run(r"""
updater_submit_image_transaction() {
    require_image_updater_transaction || return $?
    echo SUBMIT_UPDATE
}
updater_submit_image_transaction
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.index("UPGRADE:3.2.5") < result.stdout.index("SUBMIT_UPDATE")


@pytest.mark.parametrize(
    "candidate,bad_checksum,success",
    [
        (_identity("v3.2.5"), "0", True),
        (_identity("v3.2.4"), "0", False),
        (_identity("v3.2.5"), "1", False),
    ],
)
def test_selected_release_acquisition_checks_bytes_and_build_before_replacement(
    candidate,
    bad_checksum,
    success,
):
    script = r"""
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
UPDATER_STATE_DIR="$case_dir/state"
UPDATER_BINARY="$UPDATER_STATE_DIR/sakura-ai-updater"
mkdir -p "$UPDATER_STATE_DIR"
printf 'old-controller\n' > "$UPDATER_BINARY"
chmod 0700 "$UPDATER_BINARY"
cat > "$case_dir/candidate" <<'SH'
#!/bin/sh
printf '%s\n' "$CANDIDATE_IDENTITY"
SH
original_hash=$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)
candidate_hash=$(sha256sum "$case_dir/candidate" | cut -d' ' -f1)
if [[ "$BAD_CHECKSUM" == 1 ]]; then candidate_hash=$(printf '%064d' 0); fi
updater_current_uid() { printf '0\n'; }
updater_prepare_state_dir() { :; }
updater_binary_is_safe() { [[ -f "$1" && ! -L "$1" ]]; }
updater_flock() { :; }
updater_sync_temp() { :; }
updater_sync_state_dir() { :; }
updater_uname_s() { printf 'Linux\n'; }
updater_uname_m() { printf 'x86_64\n'; }
resolve_updater_app_version() { echo UNEXPECTED_BASELINE_RELEASE >&2; return 99; }
updater_curl() {
    case "$1" in
        */v3.2.5/sakura-ai-updater-linux-amd64) cp "$case_dir/candidate" "$2" ;;
        */v3.2.5/SHA256SUMS)
            printf '%s  sakura-ai-updater-linux-amd64\n' "$candidate_hash" > "$2" ;;
        *) echo UNEXPECTED_DOWNLOAD >&2; return 99 ;;
    esac
    : > "$3"
}
rc=0
install_updater_binary 3.2.5 || rc=$?
printf 'INSTALL_RESULT:%s\n' "$rc"
if [[ "$rc" == 0 ]]; then
    cmp "$case_dir/candidate" "$UPDATER_BINARY"
else
    [[ "$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)" == "$original_hash" ]]
fi
"""
    result = subprocess.run(
        [posix_bash()],
        cwd=ROOT,
        env={
            **os.environ,
            "CANDIDATE_IDENTITY": candidate,
            "BAD_CHECKSUM": bad_checksum,
        },
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=20,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "UNEXPECTED" not in output
    assert ("INSTALL_RESULT:0" in output) is success, output
