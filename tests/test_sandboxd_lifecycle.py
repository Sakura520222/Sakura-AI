"""Shell-level deployment identity tests that do not require a Linux daemon."""

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


def test_source_build_uses_content_addressed_daemon_and_runner_ids():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
SANDBOX_SOURCE_MODE=1
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE"' EXIT
docker() {
    case "$1 $2" in
        "build -f") return 0 ;;
        "image inspect")
            if [[ "$*" == *'sakura-ai-sandboxd:latest'* ]]; then
                printf 'sha256:%064d\n' 1
            else
                printf 'sha256:%064d\n' 2
            fi
            ;;
        *) return 1 ;;
    esac
}
sandbox_pull_or_build_images false
[[ "$SANDBOX_IMAGE_DIGEST" == sha256:* ]]
[[ "$SANDBOX_RUNNER_DIGEST" == sha256:* ]]
[[ "$SANDBOX_IMAGE_DIGEST" != "$SANDBOX_IMAGE" ]]
[[ "$SANDBOX_RUNNER_DIGEST" != "$SANDBOX_RUNNER_IMAGE" ]]
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_pulls_both_complete_digest_references_only():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$log"' EXIT
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
log="$(mktemp)"
docker() {
    printf '%s\n' "$*" >> "$log"
    if [[ "$1" == pull ]]; then
        return 0
    fi
    if [[ "$1" == image && "$2" == inspect ]]; then
        if [[ "$*" == *sakura-ai-sandboxd* ]]; then
            printf '%s\n' "$SANDBOX_IMAGE_DIGEST"
        elif [[ "$*" == *sakura-ai-agent-runner* ]]; then
            printf '%s\n' "$SANDBOX_RUNNER_DIGEST"
        fi
        return 0
    fi
    return 1
}
sandbox_pull_or_build_images true
grep -Fq "$SANDBOX_IMAGE_DIGEST" "$log"
grep -Fq "$SANDBOX_RUNNER_DIGEST" "$log"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_without_both_digests_fails_closed_before_pull():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE"' EXIT
SANDBOX_IMAGE_DIGEST=''
SANDBOX_RUNNER_DIGEST=''
docker() { printf 'UNEXPECTED_DOCKER\n'; return 0; }
if sandbox_pull_or_build_images true; then
    exit 1
fi
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_production_digests_are_fetched_without_mutating_state_before_pull():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=0000000000000000000000000000000000000000000000000000000000000000' \
> "$DEPLOYMENT_ENV_FILE"
cp "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"
SANDBOX_IMAGE_DIGEST=''
SANDBOX_RUNNER_DIGEST=''
curl() {
    if [[ "$*" == *'api.github.com/repos/Sakura520222/Sakura-AI/releases/tags/v3.1.0'* ]]; then
        printf '%s\n' '{"assets":[{"name":"agent-sandbox-manifest.json","browser_download_url":"https://github.com/Sakura520222/Sakura-AI/releases/download/v3.1.0/agent-sandbox-manifest.json"}]}'
    else
        printf '%s\n' '{"schema_version":1,"manifest":"agent-sandbox","version":"3.1.0","channel":"stable","sandboxd_image":"ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","runner_image":"ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
    fi
}
sandbox_ensure_production_digests
[[ "$SANDBOX_IMAGE_DIGEST" == ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:* ]]
[[ "$SANDBOX_RUNNER_DIGEST" == ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:* ]]
cmp -s "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_sandbox_pair_is_refreshed_when_persisted_web_release_changes():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' \
  'SAKURA_SANDBOX_RELEASE_VERSION=3.1.0' \
  'SAKURA_SANDBOXD_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'SAKURA_AGENT_RUNNER_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
> "$DEPLOYMENT_ENV_FILE"
cp "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
curl() {
    if [[ "$*" == *'/releases/tags/v3.2.0'* ]]; then
        printf '%s\n' '{"assets":[{"name":"agent-sandbox-manifest.json","browser_download_url":"https://github.com/Sakura520222/Sakura-AI/releases/download/v3.2.0/agent-sandbox-manifest.json"}]}'
    else
        printf '%s\n' '{"schema_version":1,"manifest":"agent-sandbox","version":"3.2.0","channel":"stable","sandboxd_image":"ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","runner_image":"ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}'
    fi
}
sandbox_ensure_production_digests
[[ "$SANDBOX_IMAGE_DIGEST" == *'@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' ]]
[[ "$SANDBOX_RUNNER_DIGEST" == *'@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' ]]
cmp -s "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fresh_production_latest_resolves_one_stable_release_and_pins_all_images():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=0000000000000000000000000000000000000000000000000000000000000000' \
  > "$DEPLOYMENT_ENV_FILE"
SANDBOX_IMAGE_DIGEST=''
SANDBOX_RUNNER_DIGEST=''
pull_log="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$pull_log"' EXIT
curl() {
    if [[ "$*" == *'/releases/latest'* ]]; then
        printf '%s\n' '{"tag_name":"v3.2.0","draft":false,"prerelease":false}'
    elif [[ "$*" == *'/releases/tags/v3.2.0'* ]]; then
        printf '%s\n' '{"assets":[{"name":"agent-sandbox-manifest.json","browser_download_url":"https://github.com/Sakura520222/Sakura-AI/releases/download/v3.2.0/agent-sandbox-manifest.json"}]}'
    else
        printf '%s\n' '{"schema_version":1,"manifest":"agent-sandbox","version":"3.2.0","channel":"stable","sandboxd_image":"ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","runner_image":"ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
    fi
}
docker() {
    printf '%s\n' "$*" >> "$pull_log"
    if [[ "$1" == pull ]]; then
        return 0
    fi
    [[ "$1" == image && "$2" == inspect ]]
}
image_digest_of() {
    case "$1" in
        *sakura-ai-sandboxd*)
            printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            ;;
        *sakura-ai-agent-runner*)
            printf '%s\n' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            ;;
        *)
            printf '%s\n' 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
            ;;
    esac
}
sandbox_pull_or_build_images true
grep -Fxq 'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest' "$DEPLOYMENT_ENV_FILE"
[[ "$PRODUCTION_WEB_IMAGE" =~ ^ghcr\.io/sakura520222/sakura-ai:v3\.2\.0@sha256:[0-9a-f]{64}$ ]]
grep -Fq 'pull ghcr.io/sakura520222/sakura-ai:v3.2.0' "$pull_log"
grep -Fq 'pull ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' "$pull_log"
grep -Fq 'pull ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' "$pull_log"
[[ "$(sandbox_release_version)" == 3.2.0 ]]
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_version_accepts_only_official_complete_web_reference_or_version():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
unset SAKURA_SANDBOX_RELEASE_VERSION
case_dir="$(mktemp -d)"
trap 'rm -rf "$case_dir"' EXIT
DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
printf '%s\n' 'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' > "$DEPLOYMENT_ENV_FILE"
[[ "$(sandbox_release_version)" == 3.2.0 ]]
for image in \
    'ghcr.io/sakura520222/other:v3.2.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
    'docker.io/sakura520222/sakura-ai:v3.2.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
    'ghcr.io/sakura520222/sakura-ai:v3.2.0' \
    'ghcr.io/sakura520222/sakura-ai:edge@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
    'ghcr.io/sakura520222/sakura-ai:v03.2.0@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
    'ghcr.io/sakura520222/sakura-ai:v3.2.0@sha256:bad' \
    'ghcr.io/sakura520222/sakura-ai@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; do
    printf 'SAKURA_AI_IMAGE=%s\n' "$image" > "$DEPLOYMENT_ENV_FILE"
    if sandbox_release_version >/dev/null 2>&1; then
        exit 1
    fi
done
SAKURA_SANDBOX_RELEASE_VERSION=3.2.0
rm -f "$DEPLOYMENT_ENV_FILE"
[[ "$(sandbox_release_version)" == 3.2.0 ]]
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_latest_resolution_failure_does_not_reuse_old_sandbox_pair():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" error.log' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest' \
  'SAKURA_SANDBOXD_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'SAKURA_AGENT_RUNNER_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  > "$DEPLOYMENT_ENV_FILE"
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
curl() { return 22; }
docker() { printf 'UNEXPECTED_DOCKER:%s\n' "$*"; return 1; }
if sandbox_ensure_production_digests 2>error.log; then
    exit 1
fi
grep -Fq '拒绝沿用旧 sandbox' error.log
grep -Fq '恢复' error.log
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_pull_failure_reports_component_and_stays_fail_closed():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE" "$DEPLOYMENT_ENV_FILE.before"' EXIT
printf '%s\n' 'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0' > "$DEPLOYMENT_ENV_FILE"
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
SAKURA_SANDBOX_RELEASE_VERSION='3.2.0'
sandbox_fetch_release_digests() {
    printf '%s\n%s\n' "$SANDBOX_IMAGE_DIGEST" "$SANDBOX_RUNNER_DIGEST"
}
docker() { printf 'UNEXPECTED:%s\n' "$*"; return 1; }
trap 'rm -f "$DEPLOYMENT_ENV_FILE" error.log' EXIT
if sandbox_pull_or_build_images true 2>error.log; then
    exit 1
fi
grep -Fq 'sandboxd' error.log
grep -Fq '恢复' error.log
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sandboxd_start_passes_server_owned_egress_network_to_daemon_identity_and_argv():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
case_dir="$(mktemp -d)"
trap 'rm -rf "$case_dir"' EXIT
SANDBOX_RUNTIME_DIR="$case_dir/runtime"
SANDBOX_SOCKET_PATH="$SANDBOX_RUNTIME_DIR/sandboxd.sock"
SANDBOX_STATE_DIR="$case_dir/state"
SANDBOX_CONTAINER_ID_FILE="$SANDBOX_STATE_DIR/container.id"
SANDBOX_IDENTITY_FILE="$SANDBOX_STATE_DIR/container.identity"
SANDBOX_INSTANCE_ID_FILE="$SANDBOX_STATE_DIR/instance.id"
SANDBOX_WORKSPACE_ROOT="$case_dir/workspace"
SANDBOX_EGRESS_NETWORK='sakura-ai-deps'
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
log="$case_dir/docker.log"
sandbox_prepare_directories() { :; }
sandbox_instance_id() { printf 'sandbox-12345678\n'; }
sandbox_runner_reference() { printf '%s\n' "$SANDBOX_RUNNER_DIGEST"; }
sandbox_daemon_reference() { printf '%s\n' "$SANDBOX_IMAGE_DIGEST"; }
sandbox_persist_runtime_identity() { :; }
sandbox_read_container_id() { return 1; }
sandbox_container_id_from_name() { return 1; }
sandbox_remove_stale_socket() { :; }
sandbox_write_identity() { :; }
sandbox_container_owned() { return 0; }
sandbox_container_matches_expected() { return 0; }
sandbox_wait_ready() { return 0; }
docker() {
    printf '%s\n' "$*" >> "$log"
    if [[ "$1" == inspect && "$*" == *"{{.Id}}"* ]]; then
        printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
    fi
    return 0
}
sandbox_start_container false
grep -Fq -- '--label ai.sakura.egress-network=sakura-ai-deps' "$log"
grep -Fq -- '--egress-network sakura-ai-deps' "$log"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sandboxd_health_requires_matching_egress_capability_field():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
SANDBOX_EGRESS_NETWORK='sakura-ai-deps'
SANDBOX_IMAGE_DIGEST='ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
SANDBOX_RUNNER_DIGEST='ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
SANDBOX_WORKSPACE_ROOT='/workspace'
sandbox_instance_id() { printf 'sandbox-12345678\n'; }
sandbox_read_container_id() { printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'; }
sandbox_container_matches_expected() { return 0; }
sandbox_health_payload() {
    printf '%s\n' '{"protocol_version":2,"sandboxd_version":"0.1.0","data":{"ready":true,"runtime":"docker","profiles":["agent","dependency"],"instance_id":"sandbox-12345678","egress_capability":"egress","workspace_root":"/workspace","runner_image_digest":"ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}'
}
sandbox_health_ready
sandbox_health_payload() {
    printf '%s\n' '{"protocol_version":2,"sandboxd_version":"0.1.0","data":{"ready":true,"runtime":"docker","profiles":["agent","dependency"],"instance_id":"sandbox-12345678","egress_capability":"none","workspace_root":"/workspace","runner_image_digest":"ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}'
}
if sandbox_health_ready; then
    exit 1
fi
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr
