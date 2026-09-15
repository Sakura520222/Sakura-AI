"""Behavioral contracts for dev-channel image label validation in start.sh."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]

_WEB_REPO = "ghcr.io/sakura520222/sakura-ai"
_SANDBOXD_REPO = "ghcr.io/sakura520222/sakura-ai-sandboxd"
_RUNNER_REPO = "ghcr.io/sakura520222/sakura-ai-agent-runner"
_DEV_TAG_REVISION = "0123456789abcdef0123456789abcdef01234567"
_DEV_TAG = f"dev-20260914000000-v3.2.0-{_DEV_TAG_REVISION}"
_FAKE_DIGEST = "sha256:" + "a" * 64


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


def _harness(web_revision: str, web_component: str) -> str:
    """Source start.sh and run the dev pull with stubbed docker helpers.

    ``web_revision``/``web_component`` control the Web image labels; sandbox
    labels always match the tag, isolating the Web-only validation paths.
    """

    return rf'''
set -u
export _START_SH_SOURCED=1
source ./start.sh
read_deployment_value() {{ printf '\n'; }}
production_resolve_dev_deployment() {{ printf '%s\n' '{_WEB_REPO}:{_DEV_TAG}@{_FAKE_DIGEST}' '{_SANDBOXD_REPO}@{_FAKE_DIGEST}' '{_RUNNER_REPO}@{_FAKE_DIGEST}'; }}
sandbox_pull_image() {{ return 0; }}
image_digest_of() {{ printf '{_FAKE_DIGEST}\n'; }}
info() {{ :; }}
fail() {{ printf '%s\n' "$1" >&2; }}
image_label_of() {{
    case "$1" in
        *{_SANDBOXD_REPO}@*|*{_RUNNER_REPO}@*)
            case "$2" in
                com.sakura-ai.build.channel) printf 'development\n' ;;
                org.opencontainers.image.revision) printf '{_DEV_TAG_REVISION}\n' ;;
                org.opencontainers.image.version) echo 3.2.0 ;;
                com.sakura-ai.component) case "$1" in *sakura-ai-sandboxd*) echo sandboxd ;; *) echo agent-runner ;; esac ;;
                *) return 1 ;;
            esac
            ;;
        *{_WEB_REPO}:*)
            case "$2" in
                com.sakura-ai.build.channel) printf 'development\n' ;;
                org.opencontainers.image.revision) printf '{web_revision}\n' ;;
                com.sakura-ai.component) printf '{web_component}\n' ;;
                org.opencontainers.image.version) echo 3.2.0 ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}}
production_pull_dev_channel_images
status=$?
printf 'WEB_IMAGE=%s\nSANDBOXD_DIGEST=%s\nRUNNER_DIGEST=%s\n' \
    "$PRODUCTION_WEB_IMAGE" "$SANDBOX_IMAGE_DIGEST" "$SANDBOX_RUNNER_DIGEST"
exit $status
'''


def test_dev_channel_rejects_web_image_revision_mismatch():
    result = _run_bash(_harness(web_revision="f" * 40, web_component="web"))

    assert result.returncode != 0
    assert "Web" in result.stderr or "revision" in result.stderr


def test_dev_channel_rejects_web_image_with_wrong_component():
    result = _run_bash(_harness(web_revision=_DEV_TAG_REVISION, web_component="sandboxd"))

    assert result.returncode != 0
    assert "component" in result.stderr


def test_dev_channel_accepts_aligned_labels_and_pins_digests():
    result = _run_bash(
        _harness(web_revision=_DEV_TAG_REVISION, web_component="web")
    )

    assert result.returncode == 0, result.stderr
    assert f"{_WEB_REPO}:{_DEV_TAG}@{_FAKE_DIGEST}" in result.stdout
    assert f"{_SANDBOXD_REPO}@{_FAKE_DIGEST}" in result.stdout
    assert f"{_RUNNER_REPO}@{_FAKE_DIGEST}" in result.stdout


def _persisted_harness(sandbox_revision: str) -> str:
    """Model an old pre-manifest pin and the currently published manifest."""
    return r"""
set -u
export _START_SH_SOURCED=1
source ./start.sh
RESOLVED=0
read_deployment_value() {
    case "$1" in
        SAKURA_AI_IMAGE) printf '%s\n' 'ghcr.io/sakura520222/sakura-ai:dev-20260914000000-v3.2.0-0123456789abcdef0123456789abcdef01234567@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
        SAKURA_SANDBOXD_IMAGE_DIGEST) printf '%s\n' 'ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' ;;
        SAKURA_AGENT_RUNNER_IMAGE_DIGEST) printf '%s\n' 'ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' ;;
        *) printf '\n' ;;
    esac
}
production_resolve_dev_deployment() {
    RESOLVED=1
    printf '%s\n' 'ghcr.io/sakura520222/sakura-ai:dev-20260915000000-v3.2.0-ffffffffffffffffffffffffffffffffffffffff@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' \
        'ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' \
        'ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
}
sandbox_pull_image() { return 0; }
info() { :; }
warn() { :; }
fail() { printf '%s\n' "$1" >&2; }
image_label_of() {
    case "$1" in
        *@sha256:dddddd*) revision='ffffffffffffffffffffffffffffffffffffffff' ;;
        *ghcr.io/sakura520222/sakura-ai:*) revision='0123456789abcdef0123456789abcdef01234567' ;;
        *ghcr.io/sakura520222/sakura-ai-sandboxd@*|*ghcr.io/sakura520222/sakura-ai-agent-runner@*) revision='__PERSISTED_SANDBOX_REVISION__' ;;
        *) return 1 ;;
    esac
    case "$2" in
        com.sakura-ai.build.channel) printf 'development\n' ;;
        org.opencontainers.image.revision) printf '%s\n' "$revision" ;;
        org.opencontainers.image.version) echo 3.2.0 ;;
        com.sakura-ai.component)
            case "$1" in
                *ghcr.io/sakura520222/sakura-ai:*) echo web ;;
                *ghcr.io/sakura520222/sakura-ai-sandboxd@*) echo sandboxd ;;
                *) echo agent-runner ;;
            esac ;;
        *) return 1 ;;
    esac
}
production_pull_dev_channel_images
status=$?
printf 'WEB_IMAGE=%s\nSANDBOXD_DIGEST=%s\nRUNNER_DIGEST=%s\n' \
    "$PRODUCTION_WEB_IMAGE" "$SANDBOX_IMAGE_DIGEST" "$SANDBOX_RUNNER_DIGEST"
exit $status
""".replace("__PERSISTED_SANDBOX_REVISION__", sandbox_revision)


def test_pre_manifest_complete_pin_stays_on_its_immutable_deployment():
    result = _run_bash(_persisted_harness(_DEV_TAG_REVISION))

    assert result.returncode == 0, result.stderr
    assert "sha256:" + "a" * 64 in result.stdout
    assert "sha256:" + "b" * 64 in result.stdout
    assert "sha256:" + "c" * 64 in result.stdout


def test_pre_manifest_mixed_pin_restarts_from_complete_channel_manifest():
    result = _run_bash(_persisted_harness("e" * 40))

    assert result.returncode == 0, result.stderr
    assert "sha256:" + "d" * 64 in result.stdout
