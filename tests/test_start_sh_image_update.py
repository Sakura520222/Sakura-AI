"""Image-update diagnostics must preserve the actual blocking preflight gates."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.platform_support import posix_bash

ROOT = Path(__file__).parents[1]
IMAGE = (
    "ghcr.io/sakura520222/sakura-ai:"
    f"dev-20261104080949-v3.2.5-{'a' * 40}@sha256:{'b' * 64}"
)


def _update(payload: str, *, target_image: str = IMAGE) -> subprocess.CompletedProcess[str]:
    script = r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
require_image_deployment() { :; }
require_idle_image_deployment() { :; }
init_deployment_env() { :; }
require_image_updater_transaction() { :; }
read_deployment_value() { printf '%s\n' "$TEST_IMAGE"; }
production_resolve_dev_deployment() { printf '%s\n' "$TEST_TARGET_IMAGE"; }
docker() { echo UNEXPECTED_DOCKER >&2; return 99; }
apply_channel_image() { echo UNEXPECTED_COMPOSE >&2; return 99; }
updater_ipc_wait_job() { echo UNEXPECTED_JOB >&2; return 99; }
curl() {
    local output='' body=''
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --output) output="$2"; shift 2 ;;
            --data) body="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    printf 'REQUEST:%s\n' "$body" >&2
    printf '%s' "$TEST_PAYLOAD" > "$output"
    printf '422'
}
cmd_update_image
'''
    result = subprocess.run(
        [posix_bash()],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=(
            f"TEST_IMAGE={shlex.quote(IMAGE)}\n"
            f"TEST_TARGET_IMAGE={shlex.quote(target_image)}\n"
            f"TEST_PAYLOAD={shlex.quote(payload)}\n" + script
        ).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )
    assert "UNEXPECTED" not in output.stdout + output.stderr
    requests = [line[8:] for line in output.stderr.splitlines() if line.startswith("REQUEST:")]
    assert len(requests) == 1
    body = json.loads(requests[0])
    assert body["target"]["channel"] == "development"
    assert body["target"]["tag"] == target_image.split("@", 1)[0].rsplit(":", 1)[1]
    assert body["target"]["digest"] == target_image.rsplit("@", 1)[1]
    assert body.get("confirm_channel_switch", False) is False
    return output


@pytest.mark.parametrize("indent", [None, 2])
def test_missing_container_is_not_hidden_by_channel_confirmation(indent):
    payload = json.dumps(
        {
            "error": "preflight_failed",
            "checks": [
                {"name": "channel_switch_confirmed", "passed": False},
                {
                    "name": "current_image_identity_valid",
                    "passed": False,
                    "detail": "docker inspection failed: no such object: sakura-ai",
                },
                {"name": "registry_digest_matches", "passed": False},
            ],
        },
        indent=indent,
        sort_keys=indent is not None,
        separators=(",", ":") if indent is None else None,
    )
    result = _update(payload)

    assert result.returncode != 0
    assert "当前部署" in result.stderr
    assert "no such object: sakura-ai" in result.stderr
    assert "registry_digest_matches" in result.stderr
    assert "停服更新" in result.stderr
    assert "频道切换未确认，拒绝更新" not in result.stderr


def test_channel_confirmation_only_explains_unknown_runtime_channel():
    result = _update(json.dumps({
        "error": "preflight_failed",
        "checks": [{"name": "channel_switch_confirmed", "passed": False}],
    }))

    assert result.returncode != 0
    assert "运行频道" in result.stderr
    assert "[11]" in result.stderr
    assert "channel_switch_confirmed" in result.stderr


def test_converged_deployment_ignores_nonblocking_advisory():
    result = _update(json.dumps({
        "error": "preflight_failed",
        "checks": [
            {"name": "target_newer", "passed": False},
            {"name": "current_sandbox_pair_complete", "passed": False, "blocking": False},
        ],
    }))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "无需更新" in result.stdout


def test_current_channel_update_resolves_latest_target_instead_of_saved_pin():
    latest = IMAGE.replace("20261104080949", "20261105080949").replace("a" * 40, "c" * 40)
    latest = latest.replace("b" * 64, "d" * 64)
    result = _update(json.dumps({
        "error": "preflight_failed",
        "checks": [{"name": "disk_space_sufficient", "passed": False}],
    }), target_image=latest)

    assert result.returncode != 0
    assert latest in result.stdout


def test_converged_web_does_not_hide_another_blocking_failure():
    result = _update(json.dumps({
        "error": "preflight_failed",
        "checks": [
            {"name": "target_newer", "passed": False},
            {"name": "disk_space_sufficient", "passed": False, "detail": "free=1"},
        ],
    }))

    assert result.returncode != 0
    assert "disk_space_sufficient" in result.stderr
    assert "free=1" in result.stderr
    assert "无需更新" not in result.stdout


@pytest.mark.parametrize("checks", [
    None,
    {},
    [],
    [None],
    [{"name": "target_newer", "passed": "false"}],
    [{"name": "target_newer", "passed": False, "blocking": "false"}],
    [{"name": "target_newer", "passed": True}],
])
def test_malformed_or_inconsistent_preflight_never_reports_success(checks):
    result = _update(json.dumps({"error": "preflight_failed", "checks": checks}))

    assert result.returncode != 0
    assert "无需更新" not in result.stdout
    assert "preflight_failed" in result.stderr


@pytest.mark.parametrize("answer", ["y", "n", ""])
def test_selecting_saved_channel_requires_explicit_confirmation(answer):
    script = r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
require_image_deployment() { :; }
require_idle_image_deployment() { :; }
init_deployment_env() { :; }
require_image_updater_transaction() { :; }
read_deployment_value() { printf '%s\n' "$TEST_IMAGE"; }
updater_submit_image_transaction() { printf 'SUBMIT:%s:%s\n' "$1" "$3"; }
printf '%s' "$TEST_ANSWERS" | cmd_switch_channel
'''
    result = subprocess.run(
        [posix_bash()],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=(
            f"TEST_IMAGE={shlex.quote(IMAGE)}\n"
            f"TEST_ANSWERS={shlex.quote('2\n' + (answer + '\n' if answer else ''))}\n"
            + script
        ).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert ("SUBMIT:development:true" in output) is (answer == "y")


@pytest.mark.parametrize("resolver", ["return 1", "printf 'not-an-image\\n'"])
def test_failed_channel_resolution_never_submits_saved_target(resolver):
    script = r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
require_image_deployment() { :; }
require_idle_image_deployment() { :; }
init_deployment_env() { :; }
read_deployment_value() { printf '%s\n' "$TEST_IMAGE"; }
updater_submit_image_transaction() { echo UNEXPECTED_SUBMIT; }
production_resolve_dev_deployment() { __RESOLVER__; }
cmd_update_image
'''.replace("__RESOLVER__", resolver)
    result = subprocess.run(
        [posix_bash()], cwd=ROOT, env={**os.environ, "TERM": "dumb"},
        input=(f"TEST_IMAGE={shlex.quote(IMAGE)}\n" + script).encode("utf-8"),
        capture_output=True, check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode != 0
    assert "UNEXPECTED_SUBMIT" not in output
    assert "未提交更新" in output
