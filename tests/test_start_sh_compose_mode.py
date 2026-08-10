"""Behavioral contracts for start.sh's persisted deployment-mode routing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run_start_sh(mode: str | None, action: str) -> subprocess.CompletedProcess[str]:
    deployment_line = (
        f"printf 'SAKURA_DEPLOY_MODE=%s\\n' '{mode}' > \"$deployment_file\""
        if mode is not None
        else ": > \"$deployment_file\""
    )
    command = f"""
set -u
export _START_SH_SOURCED=1
source ./start.sh
case_dir=$(mktemp -d)
trap 'rm -rf "$case_dir"' EXIT
deployment_file="$case_dir/deployment.env"
{deployment_line}
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
    result = _run_start_sh("image", action)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--compose-file docker/docker-compose.prod.yml" in result.stdout
    assert "--compose-file docker/docker-compose.yml" not in result.stdout


@pytest.mark.parametrize("action", ["ensure_updater_running", "cmd_updater start"])
@pytest.mark.parametrize("mode", ["source", None, "unknown"])
def test_non_image_mode_keeps_development_compose_with_compatibility_warning(
    mode: str | None, action: str
):
    result = _run_start_sh(mode, action)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--compose-file docker/docker-compose.yml" in result.stdout
    assert "--compose-file docker/docker-compose.prod.yml" not in result.stdout
    if mode == "source":
        assert "using development compose" not in result.stdout.lower()
    else:
        assert "using development compose" in result.stdout.lower()


def test_mode_parser_does_not_source_or_evaluate_deployment_file():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    helper_start = script.index("select_compose_from_deployment_mode()")
    helper_end = script.index("\n}\n", helper_start) + 3
    helper = script[helper_start:helper_end]

    assert "source " not in helper
    assert "eval " not in helper
    assert "SAKURA_DEPLOY_MODE=" in helper
