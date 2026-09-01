"""Behavioral contracts for start.sh's persisted deployment-mode routing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run_start_sh(
    mode: str | None,
    action: str,
    *,
    project: str | None = None,
) -> subprocess.CompletedProcess[str]:
    deployment_line = (
        f"printf 'SAKURA_DEPLOY_MODE=%s\\n' '{mode}' > \"$deployment_file\""
        if mode is not None
        else ': > "$deployment_file"'
    )
    project_line = (
        f"printf 'COMPOSE_PROJECT_NAME=%s\\n' '{project}' >> \"$deployment_file\""
        if project is not None
        else ":"
    )
    command = f"""
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
deployment_file="$case_dir/deployment.env"
{deployment_line}
{project_line}
export UPDATER_DEPLOYMENT_ENV_FILE="$deployment_file"
export UPDATER_STATE_DIR="$case_dir/updater"
export UPDATER_BINARY="$case_dir/updater/sakura-ai-updater"
export SAKURA_UPDATER_DEV=1
COMPOSE_FILE='docker/docker-compose.yml'
PROD_COMPOSE_FILE='docker/docker-compose.prod.yml'
updater_binary_is_safe() {{ return 1; }}
updater_backend() {{
    printf 'BACKEND:%s\\n' "$*"
    if [[ "$1" == 'is-running' ]]; then
        return 1
    fi
    return 0
}}
{action}
"""
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


@pytest.mark.parametrize("action", ["ensure_updater_running", "cmd_updater start"])
def test_image_mode_routes_updater_to_production_compose(action: str):
    result = _run_start_sh("image", action, project="sakura-ai")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "/docker/docker-compose.prod.yml --deployment-env" in result.stdout
    assert "/docker/docker-compose.yml --deployment-env" not in result.stdout


@pytest.mark.parametrize("action", ["ensure_updater_running", "cmd_updater start"])
def test_source_mode_routes_updater_to_development_compose(action: str):
    result = _run_start_sh("source", action)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "/docker/docker-compose.yml --deployment-env" in result.stdout
    assert "/docker/docker-compose.prod.yml --deployment-env" not in result.stdout


@pytest.mark.parametrize("mode", [None, "unknown"])
def test_invalid_deployment_mode_has_no_compose_fallback(mode: str | None):
    result = _run_start_sh(mode, "select_compose_from_deployment_mode")

    assert result.returncode != 0
    assert "SAKURA_DEPLOY_MODE must be 'source' or 'image'" in result.stderr


def test_live_unmanaged_socket_blocks_duplicate_updater_start():
    command = """
set -u
export _START_SH_SOURCED=1
source ./start.sh
updater_backend() {
    printf 'BACKEND:%s\n' "$*"
    return 1
}
updater_socket_listener_responds() { return 0; }
updater_binary_is_safe() { printf 'UNEXPECTED_BINARY_CHECK\n'; return 0; }
ensure_updater_running
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode != 0
    assert "refusing duplicate start" in stderr
    assert "UNEXPECTED_BINARY_CHECK" not in stdout
    assert "BACKEND:install" not in stdout
    assert "BACKEND:start" not in stdout


def test_updater_runtime_inputs_are_anchored_when_script_is_called_elsewhere(tmp_path):
    project = tmp_path / "project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    shutil.copyfile(ROOT / "start.sh", project / "start.sh")
    command = """
set -u
export _START_SH_SOURCED=1
source ../project/start.sh
printf 'SOURCE=%s\n' "$UPDATER_SOURCE_COMPOSE_FILE"
printf 'PROD=%s\n' "$UPDATER_PROD_COMPOSE_FILE"
printf 'ENV=%s\n' "$UPDATER_DEPLOYMENT_ENV_FILE"
"""
    result = subprocess.run(
        ["bash"],
        cwd=caller,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    assert result.returncode == 0, stdout + result.stderr.decode(
        "utf-8", errors="replace"
    )
    assert "/docker/docker-compose.yml" in stdout
    assert "/docker/docker-compose.prod.yml" in stdout
    assert "/.deploy/deployment.env" in stdout
    assert "/caller/" not in stdout


def test_mode_parser_does_not_source_or_evaluate_deployment_file():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    helper_start = script.index("read_deployment_value()")
    helper_end = script.index("\n}\n", helper_start) + 3
    helper = script[helper_start:helper_end]

    assert "source " not in helper
    assert "eval " not in helper


@pytest.mark.parametrize(
    ("persisted_mode", "requested_prod", "expected"),
    [
        ("image", "false", True),
        ("source", "false", False),
        (None, "false", False),
        ("source", "true", True),
    ],
)
def test_start_mode_honors_persisted_image_deployment(
    persisted_mode: str | None,
    requested_prod: str,
    expected: bool,
):
    deployment_line = (
        f"printf 'SAKURA_DEPLOY_MODE=%s\\n' '{persisted_mode}' > \"$DEPLOYMENT_ENV_FILE\""
        if persisted_mode is not None
        else ': > "$DEPLOYMENT_ENV_FILE"'
    )
    command = f"""
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
{deployment_line}
should_use_production_mode {requested_prod}
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )

    assert (result.returncode == 0) is expected


def test_status_defers_updater_bootstrap_while_deployment_is_running():
    command = """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
PID_FILE="$case_dir/build.pid"
BUILD_LOG="$case_dir/build.log"
printf '4242\n' > "$PID_FILE"
: > "$BUILD_LOG"
is_running() { return 0; }
get_phase() { printf 'start\\n'; }
updater_backend() { return 1; }
ensure_updater_running() { printf 'UNEXPECTED_BOOTSTRAP\\n'; return 1; }
tail() { :; }
cmd_status
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace")

    assert result.returncode == 0, output + result.stderr.decode(errors="replace")
    assert "UNEXPECTED_BOOTSTRAP" not in output
    assert "等待应用健康后再恢复" in output


@pytest.mark.parametrize(
    ("action", "verb"), [("do_ps false", "ps"), ("do_down false", "down")]
)
def test_service_commands_route_persisted_image_mode_to_fixed_project(
    action: str,
    verb: str,
):
    command = f"""
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' \
    'SAKURA_DEPLOY_MODE=image' \
    'COMPOSE_PROJECT_NAME=sakura-ai' \
    'SAKURA_DB_PASSWORD={"0" * 64}' > "$DEPLOYMENT_ENV_FILE"
docker() {{
    if [[ "$1 $2" == 'compose version' ]]; then
        return 0
    fi
    printf 'DOCKER:%s\n' "$*"
}}
# This test isolates Compose routing.  Independent sandbox lifecycle behavior
# is covered by test_start_sh_lifecycle.py.
sandbox_lifecycle_enabled() {{ return 1; }}
{action}
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace")

    assert result.returncode == 0, output + result.stderr.decode(errors="replace")
    assert "--project-name sakura-ai" in output
    assert f"-f docker/docker-compose.prod.yml {verb}" in output


def test_status_restarts_safe_installed_updater_without_application_health():
    command = """
set -u
export _START_SH_SOURCED=1
source ./start.sh
is_running() { return 1; }
get_phase() { printf 'none\n'; }
get_result() { :; }
updater_backend() { return 1; }
updater_binary_is_safe() { return 0; }
updater_health_payload() { printf 'UNEXPECTED_HEALTH\n'; return 1; }
ensure_updater_running() { printf 'ENSURED\n'; return 0; }
cmd_status
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace")

    assert result.returncode == 0, output + result.stderr.decode(errors="replace")
    assert "ENSURED" in output
    assert "UNEXPECTED_HEALTH" not in output


def test_docker_compose_v1_is_rejected_with_clear_requirement():
    command = """
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
mkdir -p "$case_dir/bin"
printf '#!/usr/bin/env sh\nexit 0\n' > "$case_dir/bin/docker-compose"
chmod +x "$case_dir/bin/docker-compose"
PATH="$case_dir/bin:$PATH"
docker() { return 1; }
compose_result="$(detect_compose)"
printf 'RESULT=<%s>\n' "$compose_result"
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")

    assert result.returncode == 0, output
    assert "Docker Compose V2 is required" in output
    assert "RESULT=<>" in output
