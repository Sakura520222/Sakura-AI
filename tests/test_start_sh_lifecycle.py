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


def test_sandbox_container_id_from_name_rejects_nonempty_malformed_ps_rows():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
SANDBOX_CONTAINER_NAME='sakura-ai-sandboxd-test'
sandbox_container_owned() { return 0; }
docker() {
    printf '%s\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' 'not-a-docker-id'
}
sandbox_container_id_from_name 'sandbox-test1234'
'''
    )

    assert result.returncode != 0


def test_sandbox_container_id_from_name_accepts_one_full_hex_ps_id():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
SANDBOX_CONTAINER_NAME='sakura-ai-sandboxd-test'
sandbox_container_owned() { return 0; }
docker() {
    printf '%s\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
}
sandbox_container_id_from_name 'sandbox-test1234'
'''
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'


def test_sandbox_stop_reports_retained_container():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
SANDBOX_RUNTIME_DIR="$case_dir/run"
SANDBOX_STATE_DIR="$case_dir/state"
SANDBOX_SOCKET_PATH="$SANDBOX_RUNTIME_DIR/sandboxd.sock"
SANDBOX_CONTAINER_NAME='sakura-ai-sandboxd-test'
mkdir -p "$SANDBOX_RUNTIME_DIR" "$SANDBOX_STATE_DIR"
sandbox_prepare_directories() { :; }
sandbox_load_deployment_config() { :; }
sandbox_instance_id() { printf '%s\n' 'sandbox-test1234'; }
sandbox_read_container_id() { printf '%064d\n' 0; }
sandbox_container_owned() { return 0; }
sandbox_write_identity() { :; }
sandbox_stop_known_container() { :; }
sandbox_stop
'''
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "sandboxd 已停止并保留容器" in output


def test_down_uses_persisted_image_mode_to_stop_independent_sandboxd():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=0000000000000000000000000000000000000000000000000000000000000000' \
  > "$DEPLOYMENT_ENV_FILE"
sandbox_load_deployment_config() { :; }
ensure_prod_compose_file() { :; }
sandbox_lifecycle_enabled() {
    printf 'LIFECYCLE:%s\n' "$1"
    [[ "$1" == 'true' ]]
}
sandbox_require_root() { :; }
sandbox_stop() { printf 'SANDBOX:STOP\n'; }
docker() {
    if [[ "$1 $2" == 'compose version' ]]; then
        return 0
    fi
    printf 'DOCKER:%s\n' "$*"
}
do_down false
'''
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "LIFECYCLE:true" in output
    assert "SANDBOX:STOP" in output
    assert "DOCKER:compose" in output
    assert " down" in output


def test_down_cli_propagates_incomplete_stop_exit_status():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'down)   do_down "$prod"; exit $? ;;' in script


def test_sandbox_uninstall_ignores_stale_recorded_container_id():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
SANDBOX_RUNTIME_DIR="$case_dir/run"
SANDBOX_STATE_DIR="$case_dir/state"
SANDBOX_SOCKET_PATH="$SANDBOX_RUNTIME_DIR/sandboxd.sock"
SANDBOX_CONTAINER_ID_FILE="$SANDBOX_STATE_DIR/container.id"
SANDBOX_IDENTITY_FILE="$SANDBOX_STATE_DIR/container.identity"
SANDBOX_INSTANCE_ID_FILE="$SANDBOX_STATE_DIR/instance.id"
mkdir -p "$SANDBOX_RUNTIME_DIR" "$SANDBOX_STATE_DIR"
printf '%064d\n' 1 > "$SANDBOX_CONTAINER_ID_FILE"
printf '%s\n' 'sandbox-test1234' > "$SANDBOX_INSTANCE_ID_FILE"
sandbox_prepare_directories() { :; }
sandbox_load_deployment_config() { :; }
sandbox_instance_id() { printf '%s\n' 'sandbox-test1234'; }
sandbox_container_inspect() { return 1; }
sandbox_container_id_from_name() { return 1; }
docker() {
    if [[ "$1 $2" == 'ps -aq' ]]; then
        return 0
    fi
    printf 'UNEXPECTED_DOCKER:%s\n' "$*"
    return 1
}
sandbox_uninstall false
[[ ! -e "$SANDBOX_CONTAINER_ID_FILE" ]]
[[ ! -e "$SANDBOX_INSTANCE_ID_FILE" ]]
'''
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "container.id 已过期" in output
    assert "UNEXPECTED_DOCKER" not in output


def test_sandbox_uninstall_recovers_migrated_controller_instance():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
SANDBOX_RUNTIME_DIR="$case_dir/run"
SANDBOX_STATE_DIR="$case_dir/state"
SANDBOX_SOCKET_PATH="$SANDBOX_RUNTIME_DIR/sandboxd.sock"
SANDBOX_CONTAINER_ID_FILE="$SANDBOX_STATE_DIR/container.id"
SANDBOX_IDENTITY_FILE="$SANDBOX_STATE_DIR/container.identity"
SANDBOX_INSTANCE_ID_FILE="$SANDBOX_STATE_DIR/instance.id"
SANDBOX_CONTAINER_NAME='sakura-ai-sandboxd-test'
container_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
log="$case_dir/docker.log"
mkdir -p "$SANDBOX_RUNTIME_DIR" "$SANDBOX_STATE_DIR"
printf '%s\n' "$container_id" > "$SANDBOX_CONTAINER_ID_FILE"
printf '%s\n' 'sandbox-new12345' > "$SANDBOX_INSTANCE_ID_FILE"
sandbox_prepare_directories() { :; }
sandbox_load_deployment_config() { :; }
sandbox_instance_id() { printf '%s\n' 'sandbox-new12345'; }
sandbox_container_inspect() { return 0; }
sandbox_container_owned() { return 1; }
sandbox_container_has_controller_identity() { return 0; }
sandbox_write_identity() { :; }
sandbox_stop_known_container() { printf 'STOP:%s\n' "$1" >> "$log"; }
docker() { printf 'DOCKER:%s\n' "$*" >> "$log"; }
sandbox_uninstall false
grep -Fq "STOP:$container_id" "$log"
grep -Fq "DOCKER:rm $container_id" "$log"
'''
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "instance 与当前安装目录不一致" in output


def test_sandbox_uninstall_still_refuses_unowned_recorded_container():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
SANDBOX_RUNTIME_DIR="$case_dir/run"
SANDBOX_STATE_DIR="$case_dir/state"
SANDBOX_SOCKET_PATH="$SANDBOX_RUNTIME_DIR/sandboxd.sock"
SANDBOX_CONTAINER_ID_FILE="$SANDBOX_STATE_DIR/container.id"
SANDBOX_IDENTITY_FILE="$SANDBOX_STATE_DIR/container.identity"
SANDBOX_INSTANCE_ID_FILE="$SANDBOX_STATE_DIR/instance.id"
container_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
mkdir -p "$SANDBOX_RUNTIME_DIR" "$SANDBOX_STATE_DIR"
printf '%s\n' "$container_id" > "$SANDBOX_CONTAINER_ID_FILE"
printf '%s\n' 'sandbox-test1234' > "$SANDBOX_INSTANCE_ID_FILE"
sandbox_prepare_directories() { :; }
sandbox_load_deployment_config() { :; }
sandbox_instance_id() { printf '%s\n' 'sandbox-test1234'; }
sandbox_container_inspect() { return 0; }
sandbox_container_owned() { return 1; }
sandbox_container_has_controller_identity() { return 1; }
sandbox_stop_known_container() { printf 'UNEXPECTED_STOP\n'; }
docker() { printf 'UNEXPECTED_DOCKER:%s\n' "$*"; }
if sandbox_uninstall false; then
    exit 1
fi
'''
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "refusing to remove an unowned sandboxd container" in output
    assert "UNEXPECTED_STOP" not in output
    assert "UNEXPECTED_DOCKER" not in output


def test_migrated_controller_identity_requires_exact_daemon_labels():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
SANDBOX_CONTAINER_NAME='sakura-ai-sandboxd-test'
container_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
docker_payload='{"Name":"/sakura-ai-sandboxd-test","Config":{"Labels":{"ai.sakura.managed-by":"sandboxd-daemon","ai.sakura.instance-id":"sandbox-old12345","ai.sakura.protocol-version":"2"}}}'
docker() { printf '%s\n' "$docker_payload"; }
sandbox_container_has_controller_identity "$container_id"
docker_payload='{"Name":"/sakura-ai-sandboxd-test","Config":{"Labels":{"ai.sakura.managed-by":"sandboxd","ai.sakura.instance-id":"sandbox-old12345","ai.sakura.protocol-version":"2"}}}'
sandbox_container_has_controller_identity "$container_id"
docker_payload='{"Name":"/sakura-ai-sandboxd-test","Config":{"Labels":{"ai.sakura.managed-by":"sandbox-runner","ai.sakura.instance-id":"sandbox-old12345","ai.sakura.protocol-version":"2"}}}'
if sandbox_container_has_controller_identity "$container_id"; then
    exit 1
fi
docker_payload='{"Name":"/unrelated","Config":{"Labels":{"ai.sakura.managed-by":"sandboxd-daemon","ai.sakura.instance-id":"sandbox-old12345","ai.sakura.protocol-version":"2"}}}'
if sandbox_container_has_controller_identity "$container_id"; then
    exit 1
fi
'''
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_image_update_fails_closed_without_updater_and_never_calls_web_only_helper():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' > "$DEPLOYMENT_ENV_FILE"
init_deployment_env() { :; }
require_idle_image_deployment() { :; }
updater_daemon_is_running() { return 1; }
apply_channel_image() { printf 'UNEXPECTED_WEB_ONLY\n'; return 99; }
cmd_update_image
"""
    )

    assert result.returncode != 0
    assert "UNEXPECTED_WEB_ONLY" not in result.stdout
    assert "Web-only Compose fallback" in result.stderr


def test_image_update_request_failure_does_not_fallback_to_web_only_compose():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' > "$DEPLOYMENT_ENV_FILE"
init_deployment_env() { :; }
require_idle_image_deployment() { :; }
updater_daemon_is_running() { return 0; }
curl() { return 7; }
apply_channel_image() { printf 'UNEXPECTED_WEB_ONLY\n'; return 99; }
cmd_update_image
"""
    )

    assert result.returncode != 0
    assert "UNEXPECTED_WEB_ONLY" not in result.stdout
    assert "Web-only Compose fallback" in result.stderr


def test_channel_switch_image_mode_uses_updater_gate_without_web_only_fallback():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' > "$DEPLOYMENT_ENV_FILE"
init_deployment_env() { :; }
require_idle_image_deployment() { :; }
updater_daemon_is_running() { return 1; }
apply_channel_image() { printf 'UNEXPECTED_WEB_ONLY\n'; return 99; }
printf '2\ny\n' | cmd_switch_channel
""",
    )

    assert result.returncode != 0
    assert "UNEXPECTED_WEB_ONLY" not in result.stdout
    assert "Web-only Compose fallback" in result.stderr


def test_updater_reinstall_is_ordered_prepare_stop_install_start_status():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { printf 'ROOT\n'; }
updater_require_idle_deployment() { printf 'IDLE\n'; }
updater_socket_listener_responds() { return 0; }
updater_prepare_stop() { printf 'PREPARE\n'; }
stop_verified_updater() { printf 'STOP\n'; }
cmd_updater_install() { printf 'INSTALL\n'; }
ensure_updater_running() { printf 'START\n'; }
updater_backend() { printf 'STATUS:%s\n' "$*"; }
cmd_updater_reinstall
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    markers = ["ROOT", "IDLE", "PREPARE", "STOP", "INSTALL", "START", "STATUS:status"]
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


def test_updater_lifecycle_refuses_when_atomic_stop_gate_is_rejected():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { :; }
updater_require_idle_deployment() { :; }
updater_prepare_stop() { printf 'ACTIVE_JOB\n' >&2; return 1; }
stop_verified_updater() { printf 'UNEXPECTED_STOP\n'; }
cmd_updater_reinstall
"""
    )

    assert result.returncode != 0
    assert "ACTIVE_JOB" in result.stderr
    assert "UNEXPECTED_STOP" not in result.stdout


def test_updater_reinstall_restarts_preserved_binary_after_install_failure():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_require_root() { :; }
updater_require_idle_deployment() { :; }
updater_socket_listener_responds() { return 0; }
updater_prepare_stop() { :; }
stop_verified_updater() { printf 'STOP\n'; }
cmd_updater_install() { printf 'INSTALL_FAIL\n'; return 23; }
ensure_updater_running() { printf 'RESTORE_OLD\n'; }
cmd_updater_reinstall
"""
    )

    assert result.returncode == 23, result.stdout + result.stderr
    assert "STOP" in result.stdout
    assert "INSTALL_FAIL" in result.stdout
    assert "RESTORE_OLD" in result.stdout


def test_socket_probe_treats_http_500_as_a_live_listener():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
curl() { printf '500'; return 0; }
updater_socket_listener_responds
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_socket_probe_treats_malformed_http_as_a_live_listener():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
curl() { return 52; }
updater_socket_listener_responds
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_atomic_stop_gate_fails_closed_when_live_daemon_rejects_endpoint():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
curl() {
  if [[ "$*" == *'/v1/lifecycle/prepare-stop'* ]]; then return 22; fi
  printf '500'
  return 0
}
updater_prepare_stop
"""
    )

    assert result.returncode != 0
    assert "refused the atomic lifecycle gate" in result.stderr


def test_dev_mode_stop_uses_backend_when_binary_is_absent():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
export SAKURA_UPDATER_DEV=1
updater_binary_is_safe() { return 1; }
updater_path_exists() { return 1; }
updater_backend() { printf 'BACKEND:%s\n' "$*"; }
updater_socket_listener_responds() { return 1; }
stop_verified_updater
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BACKEND:stop" in result.stdout


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
updater_existing_state_dir_is_safe() { :; }
updater_prepare_stop() { :; }
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
printf 'SAKURA_DEPLOY_MODE=image\n' > "$UPDATER_DEPLOYMENT_ENV_FILE"
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


def test_individual_image_pull_uses_compose_progress_without_pty_wrapper():
    result = _run_bash(
        r'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
sleep() { :; }
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
calls="$case_dir/calls"
reference='ghcr.io/sakura520222/sakura-ai:v3.2.0'
    docker() {
        printf '%s\n' "$*" >> "$calls"
        case "$*" in
            'compose --help')
                printf '%s\n' '--progress'
                ;;
            *'compose --ansi always --progress tty'*'pull pull_target')
                compose_file=''
                previous=''
                for arg in "$@"; do
                    if [[ "$previous" == '--file' ]]; then
                        compose_file="$arg"
                    fi
                    previous="$arg"
                done
                [[ -n "$compose_file" ]] || return 1
                grep -Fq "image: $reference" "$compose_file"
                ;;
            pull*)
                printf 'DIRECT_PULL_USED\n' >> "$calls"
                return 1
                ;;
        esac
    }
# 旧方案会命中该函数并得到 143；新方案不得再创建或调用 PTY 包装器。
script() { printf 'SCRIPT_WRAPPER_USED\n' >> "$calls"; return 143; }
docker_pull_native_progress "$reference"
! grep -Fq 'SCRIPT_WRAPPER_USED' "$calls"
! grep -Fq 'DIRECT_PULL_USED' "$calls"
grep -Fq -- 'compose --ansi always --progress tty' "$calls"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_web_pull_failure_keeps_authoritative_env_unchanged():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
export DEPLOY_DIR="$case_dir/.deploy"
mkdir -p "$DEPLOY_DIR"
export DEPLOYMENT_ENV_FILE="$DEPLOY_DIR/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  'SAKURA_SANDBOX_EGRESS_NETWORK=bridge' > "$DEPLOYMENT_ENV_FILE"
cp "$DEPLOYMENT_ENV_FILE" "$case_dir/before"
prod=true
production_prepare_env_stage
DEPLOYMENT_ENV_FILE="$PRODUCTION_STAGED_ENV_FILE"
  sandbox_lifecycle_enabled() { return 1; }
  production_verify_stable_web_alias() { PRODUCTION_STABLE_MANIFEST_DIGEST='sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; }
  sandbox_pull_image() {
    [[ "$1" == 'Web' ]] || return 1
    return 1
}
if production_prepare_and_pull_images; then
    exit 1
fi
production_restore_env_transaction 1
cmp -s "$DEPLOYMENT_ENV_FILE" "$case_dir/before"
[[ ! -e "$PRODUCTION_STAGED_ENV_FILE" ]]
'''
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_partial_sandbox_pull_failure_keeps_authoritative_env_unchanged():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
export DEPLOY_DIR="$case_dir/.deploy"
mkdir -p "$DEPLOY_DIR"
export DEPLOYMENT_ENV_FILE="$DEPLOY_DIR/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  'SAKURA_SANDBOX_EGRESS_NETWORK=bridge' > "$DEPLOYMENT_ENV_FILE"
cp "$DEPLOYMENT_ENV_FILE" "$case_dir/before"
prod=true
production_prepare_env_stage
DEPLOYMENT_ENV_FILE="$PRODUCTION_STAGED_ENV_FILE"
  sandbox_lifecycle_enabled() { return 0; }
  production_verify_stable_web_alias() { PRODUCTION_STABLE_MANIFEST_DIGEST='sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; }
  sandbox_pull_image() {
    printf '%s\n' "$1" >> "$case_dir/pulls"
    [[ "$1" == 'Web' ]] && return 0
    return 1
}
sandbox_pull_or_build_images() {
    sandbox_pull_image 'sandboxd' 'ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
    return 1
}
if production_prepare_and_pull_images; then
    exit 1
fi
grep -Fxq 'Web' "$case_dir/pulls"
grep -Fxq 'sandboxd' "$case_dir/pulls"
production_restore_env_transaction 1
cmp -s "$DEPLOYMENT_ENV_FILE" "$case_dir/before"
[[ ! -e "$PRODUCTION_STAGED_ENV_FILE" ]]
'''
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_transaction_cleanup_failure_is_idempotent_and_keeps_committed_env():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
deploy_root="$case_dir/.deploy"
mkdir -p "$deploy_root"
authoritative="$deploy_root/deployment.env"
staged="$deploy_root/.deployment.env.pending-test"
original="$deploy_root/.deployment.env.original-test"
journal="$deploy_root/.deployment.env.transaction"
printf '%s\n' 'SAKURA_AI_IMAGE=old-image' 'COMPOSE_PROJECT_NAME=sakura-ai' > "$authoritative"
cp "$authoritative" "$original"
cp "$authoritative" "$staged"
PRODUCTION_AUTH_ENV_FILE="$authoritative"
PRODUCTION_STAGED_ENV_FILE="$staged"
PRODUCTION_ORIGINAL_ENV_FILE="$original"
PRODUCTION_TRANSACTION_JOURNAL_FILE="$journal"
PRODUCTION_ENV_COMMITTED=0
production_write_transaction_journal prepared
printf '%s\n' 'SAKURA_AI_IMAGE=committed-image' 'COMPOSE_PROJECT_NAME=sakura-ai' > "$staged"
production_commit_env_stage
cp "$authoritative" "$case_dir/committed"

# Simulate the precise failure: the journal unlink fails, so the complete
# journal+rollback pair must remain available for the next trap/restart.
production_remove_transaction_file() {
    if [[ "$1" == "$PRODUCTION_TRANSACTION_JOURNAL_FILE" ]]; then
        return 1
    fi
    rm -f -- "$1"
}
if production_restore_env_transaction 0; then
    exit 1
fi
cmp -s "$authoritative" "$case_dir/committed"
[[ -f "$original" && -f "$journal" ]]

# A second EXIT-trap attempt and a fresh-process-style recovery must both be
# strict no-ops for the already committed authority while cleanup is blocked.
if production_restore_env_transaction 1; then
    exit 1
fi
cmp -s "$authoritative" "$case_dir/committed"
[[ -f "$original" && -f "$journal" ]]
if production_recover_env_transaction "$authoritative" "$journal" "$deploy_root"; then
    exit 1
fi
cmp -s "$authoritative" "$case_dir/committed"
[[ -f "$original" && -f "$journal" ]]
''',
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_pinned_web_ref_still_requires_stable_alias_digest_match():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
export DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'COMPOSE_PROJECT_NAME=sakura-ai' > "$DEPLOYMENT_ENV_FILE"
sandbox_load_deployment_config() { :; }
sandbox_release_version() { printf '%s\n' '3.2.0'; }
sandbox_lifecycle_enabled() { return 1; }
sandbox_pull_image() { printf 'UNEXPECTED_PULL\n'; return 99; }
docker() {
    if [[ "$*" == *':latest' ]]; then
        printf '%s\n' '{"Descriptor":{"digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}'
    else
        printf '%s\n' '{"Descriptor":{"digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}}'
    fi
}
if production_prepare_and_pull_images; then
    exit 1
fi
''',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNEXPECTED_PULL" not in result.stdout
    assert "manifest digest 不一致" in result.stderr


def test_start_sh_help_documents_destructive_scope_and_lifecycle_commands():
    result = _run_bash("bash ./start.sh --help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uninstall [--purge] [--yes]" in result.stdout
    assert "reinstall/uninstall" in result.stdout

    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'set_phase "pull"' in script
    assert "$COMPOSE up -d --pull never" not in script
    assert "$COMPOSE up -d --pull always" not in script


def test_source_local_or_disabled_mode_skips_root_owned_sandbox_and_sandbox_requires_root():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
id() { if [[ "$1" == "-u" ]]; then printf '1000\\n'; else command id "$@"; fi; }
AGENT_TEAM_EXECUTION_BACKEND=local
if sandbox_lifecycle_enabled false; then exit 1; fi
AGENT_TEAM_ENABLED=false
AGENT_TEAM_EXECUTION_BACKEND=sandbox
if sandbox_lifecycle_enabled false; then exit 1; fi
unset AGENT_TEAM_ENABLED
AGENT_TEAM_EXECUTION_BACKEND=sandbox
if sandbox_lifecycle_enabled false; then
    if sandbox_require_root; then exit 1; fi
else
    exit 1
fi
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "require root" in result.stderr


def test_standalone_source_compose_has_workspace_default_without_interpolation_env():
    compose = (ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SAKURA_SANDBOX_WORKSPACE_ROOT:-${PWD}/workplace" in compose
    assert "SAKURA_SANDBOX_WORKSPACE_ROOT:?" not in compose


def test_legacy_dependency_network_is_migrated_to_server_owned_egress():
    result = _run_bash(
        """
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
if ! sandbox_egress_network_is_safe none; then exit 1; fi
if ! sandbox_egress_network_is_safe sakura-deps_1; then exit 1; fi
for value in host container:abc ns:abc '-bad' 'bad/name' 'bad name' ''; do
    if sandbox_egress_network_is_safe "$value"; then exit 1; fi
done
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
DEPLOY_DIR="$case_dir"
printf 'SAKURA_DEPLOY_MODE=source\nSAKURA_SANDBOX_DEPENDENCY_NETWORK=sakura-deps_1\n' > "$DEPLOYMENT_ENV_FILE"
SANDBOX_EGRESS_NETWORK=bridge
sandbox_load_deployment_config
[[ "$SANDBOX_EGRESS_NETWORK" == sakura-deps_1 ]]
printf 'SAKURA_DEPLOY_MODE=source\nSAKURA_SANDBOX_DEPENDENCY_NETWORK=none\n' > "$DEPLOYMENT_ENV_FILE"
SANDBOX_EGRESS_NETWORK=bridge
init_deployment_env
grep -Fq 'SAKURA_SANDBOX_EGRESS_NETWORK=bridge' "$DEPLOYMENT_ENV_FILE"
! grep -Fq 'SAKURA_SANDBOX_DEPENDENCY_NETWORK=' "$DEPLOYMENT_ENV_FILE"
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_egress_network_defaults_to_bridge_and_rejects_runtime_namespace_inputs():
    result = _run_bash(
        """
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
[[ "$SANDBOX_EGRESS_NETWORK" == bridge ]]
if ! sandbox_egress_network_is_safe bridge; then exit 1; fi
if ! sandbox_egress_network_is_safe sakura-egress_1; then exit 1; fi
for value in host container:abc ns:abc '--network=host' 'bad/name' 'bad name' ''; do
    if sandbox_egress_network_is_safe "$value"; then exit 1; fi
done
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
DEPLOY_DIR="$case_dir"
printf 'SAKURA_SANDBOX_EGRESS_NETWORK=sakura-egress_1\\n' > "$DEPLOYMENT_ENV_FILE"
sandbox_load_deployment_config
[[ "$SANDBOX_EGRESS_NETWORK" == sakura-egress_1 ]]
"""
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_uninstall_refuses_to_signal_live_pid_without_matching_runner_identity():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
PID_FILE="$case_dir/build.pid"
RUNNER_IDENTITY_FILE="$case_dir/build-runner.identity"
printf '4242\n' > "$PID_FILE"
kill() {
  if [[ "$1" == '-0' ]]; then return 0; fi
  printf 'UNEXPECTED_SIGNAL:%s\n' "$*"
  return 0
}
stop_deployment_for_uninstall
"""
    )

    assert result.returncode != 0
    assert "without verified runner identity" in result.stderr
    assert "UNEXPECTED_SIGNAL" not in result.stdout


def test_runner_identity_requires_matching_starttime_and_command():
    result = _run_bash(
        """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
PID_FILE="$case_dir/build.pid"
RUNNER_IDENTITY_FILE="$case_dir/build-runner.identity"
printf '4242\n' > "$PID_FILE"
printf '12345\n.deploy/_runner.sh\n' > "$RUNNER_IDENTITY_FILE"
runner_process_starttime() { printf '%s\n' "${FAKE_STARTTIME:-12345}"; }
runner_process_command_matches() {
  [[ "$1" == '4242' && "$2" == '.deploy/_runner.sh' ]]
}
runner_identity_matches
FAKE_STARTTIME=99999
if runner_identity_matches; then
  printf 'UNEXPECTED_MATCH\n'
  exit 1
fi
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNEXPECTED_MATCH" not in result.stdout
