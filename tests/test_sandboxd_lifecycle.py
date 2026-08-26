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
    [[ "$1" == pull ]]
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


def test_missing_production_digests_are_fetched_from_strict_release_asset_and_persisted():
    result = _bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
DEPLOYMENT_ENV_FILE="$(mktemp)"
trap 'rm -f "$DEPLOYMENT_ENV_FILE"' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0' \
  'COMPOSE_PROJECT_NAME=sakura-ai' \
  'SAKURA_DB_PASSWORD=0000000000000000000000000000000000000000000000000000000000000000' \
  > "$DEPLOYMENT_ENV_FILE"
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
grep -Fq "SAKURA_SANDBOXD_IMAGE_DIGEST=$SANDBOX_IMAGE_DIGEST" "$DEPLOYMENT_ENV_FILE"
grep -Fq "SAKURA_AGENT_RUNNER_IMAGE_DIGEST=$SANDBOX_RUNNER_DIGEST" "$DEPLOYMENT_ENV_FILE"
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
trap 'rm -f "$DEPLOYMENT_ENV_FILE"' EXIT
printf '%s\n' \
  'SAKURA_DEPLOY_MODE=image' \
  'SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0' \
  'SAKURA_SANDBOX_RELEASE_VERSION=3.1.0' \
  'SAKURA_SANDBOXD_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'SAKURA_AGENT_RUNNER_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  > "$DEPLOYMENT_ENV_FILE"
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
grep -Fq 'SAKURA_SANDBOX_RELEASE_VERSION=3.2.0' "$DEPLOYMENT_ENV_FILE"
''',
    )
    assert result.returncode == 0, result.stdout + result.stderr
