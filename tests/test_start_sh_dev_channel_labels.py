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
_DEV_TAG = f"dev-3.2.0-{_DEV_TAG_REVISION}"
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
production_dev_web_reference_is_safe() {{ return 1; }}
sandbox_registry_digest_is_safe() {{ return 1; }}
production_resolve_dev_tag() {{ printf '{_DEV_TAG}\n'; }}
sandbox_pull_image() {{ return 0; }}
image_digest_of() {{ printf '{_FAKE_DIGEST}\n'; }}
info() {{ :; }}
fail() {{ printf '%s\n' "$1" >&2; }}
image_label_of() {{
    case "$1" in
        *{_SANDBOXD_REPO}:*|*{_RUNNER_REPO}:*)
            case "$2" in
                com.sakura-ai.build.channel) printf 'development\n' ;;
                org.opencontainers.image.revision) printf '{_DEV_TAG_REVISION}\n' ;;
                *) return 1 ;;
            esac
            ;;
        *{_WEB_REPO}:*)
            case "$2" in
                com.sakura-ai.build.channel) printf 'development\n' ;;
                org.opencontainers.image.revision) printf '{web_revision}\n' ;;
                com.sakura-ai.component) printf '{web_component}\n' ;;
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
