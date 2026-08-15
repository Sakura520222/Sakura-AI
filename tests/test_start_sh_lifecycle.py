"""Behavioral contracts for start.sh lifecycle and uninstall commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _run_bash(command: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def test_updater_reinstall_is_ordered_stop_install_start_status():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { printf 'ROOT\n'; }
updater_require_idle_deployment() { printf 'IDLE\n'; }
updater_require_no_active_job() { printf 'NO_JOB\n'; }
stop_verified_updater() { printf 'STOP\n'; }
cmd_updater_install() { printf 'INSTALL\n'; }
ensure_updater_running() { printf 'START\n'; }
updater_backend() { printf 'STATUS:%s\n' "$*"; }
cmd_updater_reinstall
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    markers = ["ROOT", "IDLE", "NO_JOB", "STOP", "INSTALL", "START", "STATUS:status"]
    positions = [result.stdout.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_updater_reinstall_refuses_background_deployment_before_stop():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { :; }
updater_deployment_is_running() { return 0; }
stop_verified_updater() { printf 'UNEXPECTED_STOP\n'; }
cmd_updater_install() { printf 'UNEXPECTED_INSTALL\n'; }
cmd_updater_reinstall
"""
    )

    assert result.returncode != 0
    assert "deployment is still running in the background" in result.stderr
    assert "UNEXPECTED" not in result.stdout


def test_updater_lifecycle_refuses_an_active_update_job():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { :; }
updater_require_idle_deployment() { :; }
updater_socket_health_payload() { printf '{}\n'; }
updater_socket_status_payload() { printf '{"data":{"has_active_job": true}}\n'; }
stop_verified_updater() { printf 'UNEXPECTED_STOP\n'; }
cmd_updater_reinstall
"""
    )

    assert result.returncode != 0
    assert "an updater job is active" in result.stderr
    assert "UNEXPECTED_STOP" not in result.stdout


def test_updater_uninstall_removes_only_managed_state_files():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
export UPDATER_STATE_DIR="$case_dir/updater"
export UPDATER_BINARY="$UPDATER_STATE_DIR/sakura-ai-updater"
mkdir -p "$UPDATER_STATE_DIR/tmp"
touch "$UPDATER_BINARY" \
  "$UPDATER_STATE_DIR/daemon-meta.json" \
  "$UPDATER_STATE_DIR/updater.log" \
  "$UPDATER_STATE_DIR/updater.lock" \
  "$UPDATER_STATE_DIR/update-state.json" \
  "$UPDATER_STATE_DIR/install.lock" \
  "$UPDATER_STATE_DIR/tmp/runtime"
updater_require_root() { :; }
updater_require_idle_deployment() { :; }
updater_require_no_active_job() { :; }
updater_existing_state_dir_is_safe() { :; }
stop_verified_updater() { printf 'STOPPED\n'; }
cmd_updater_uninstall
[[ ! -e "$UPDATER_STATE_DIR" ]]
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STOPPED" in result.stdout
    assert "updater" in result.stdout


def test_compose_uninstall_preserves_volumes_by_default_and_purges_explicitly():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
export UPDATER_DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
touch "$UPDATER_DEPLOYMENT_ENV_FILE"
docker() {
  if [[ "$*" == 'compose version' ]]; then return 0; fi
  printf 'DOCKER:%s\n' "$*"
}
select_compose_from_deployment_mode() {
  COMPOSE_FILE='/opt/sakura-ai/docker/docker-compose.prod.yml'
  COMPOSE_PROJECT='sakura-ai'
}
sakura_compose_uninstall false
sakura_compose_uninstall true
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line for line in result.stdout.splitlines() if line.startswith("DOCKER:")]
    assert len(calls) == 2
    assert "down --remove-orphans" in calls[0]
    assert "--volumes" not in calls[0]
    assert "down --remove-orphans --volumes" in calls[1]
    assert "--project-name sakura-ai" in calls[0]
    assert "--env-file" in calls[0]


def test_full_uninstall_default_preserves_state_and_purge_is_explicit():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { printf 'ROOT\n'; }
stop_deployment_for_uninstall() { printf 'STOP_DEPLOYMENT\n'; }
updater_require_no_active_job() { printf 'NO_JOB\n'; }
sakura_compose_uninstall() { printf 'COMPOSE_PURGE=%s\n' "$1"; }
cmd_updater_uninstall() { printf 'REMOVE_UPDATER\n'; }
purge_sakura_deployment_state() { printf 'PURGE_STATE\n'; }
cmd_uninstall --yes
printf '%s\n' '---'
cmd_uninstall --purge --yes
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    default, purge = result.stdout.split("---", maxsplit=1)
    assert "COMPOSE_PURGE=false" in default
    assert "PURGE_STATE" not in default
    assert "COMPOSE_PURGE=true" in purge
    assert "PURGE_STATE" in purge


def test_uninstall_never_assumes_confirmation_from_noninteractive_stdin():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
confirm_sakura_uninstall false false
"""
    )

    assert result.returncode != 0
    assert "pass --yes for automation" in result.stderr


def test_purge_refuses_any_deployment_directory_other_than_project_dot_deploy():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
DEPLOY_DIR='../outside'
purge_sakura_deployment_state
"""
    )

    assert result.returncode != 0
    assert "refusing unsafe deployment state target" in result.stderr


def test_native_compose_pull_uses_tty_progress_and_has_safe_fallback():
    supported = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
docker() { printf '%s\n' '--progress'; }
compose_stub() { printf 'COMPOSE:%s\n' "$*"; }
COMPOSE=compose_stub
compose_pull_with_native_progress
"""
    )
    assert supported.returncode == 0, supported.stdout + supported.stderr
    assert "COMPOSE:--ansi always --progress tty pull" in supported.stdout

    fallback = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
docker() { printf '%s\n' 'compose help without progress renderer'; }
compose_stub() { printf 'COMPOSE:%s\n' "$*"; }
COMPOSE=compose_stub
compose_pull_with_native_progress
"""
    )
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    assert "COMPOSE:pull" in fallback.stdout
    assert "--progress tty" not in fallback.stdout


def test_start_sh_help_documents_destructive_scope_and_lifecycle_commands():
    result = _run_bash("bash ./start.sh --help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uninstall [--purge] [--yes]" in result.stdout
    assert "reinstall/uninstall" in result.stdout

    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'set_phase "pull"' in script
    assert "$COMPOSE up -d --pull never" in script
    assert "$COMPOSE up -d --pull always" not in script
