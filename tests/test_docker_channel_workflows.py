from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _load(name: str) -> tuple[dict, str]:
    path = ROOT / ".github" / "workflows" / name
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    return document, text


def test_development_workflow_is_develop_only_and_immutable_source():
    workflow, text = _load("docker-edge.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["push"]["branches"] == ["develop"]
    assert "github.sha" in text
    assert "channel: development" in text
    assert "github.ref == 'refs/heads/develop'" in text
    assert "latest" not in text.lower()


def test_reusable_publish_fails_closed_and_sets_build_identity():
    _, text = _load("docker-publish.yml")
    assert "stable|development" in text
    assert "unsupported channel" in text
    assert "DEV_TAG=\"dev-${UTC_CREATED}-v${VERSION}-${REVISION}\"" in text
    assert "SAKURA_BUILD_CHANNEL" in text
    assert "SAKURA_BUILD_REVISION" in text
    assert "platforms: linux/amd64,linux/arm64" in text
    # crane is required in the build job before channel tags are materialized;
    # the sync job has its own installation as well.
    assert text.count("uses: imjasonh/setup-crane@v0.4") >= 2
    assert 'existing_error=$(mktemp)' in text
    assert 'unable to verify immutable tag; refusing publication' in text
    assert 'crane copy "$SOURCE" "$IMMUTABLE"' in text
    assert "push-by-digest=true" in text
    assert "name-canonical=true" in text
    assert 'SOURCE="${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${DIGEST}"' in text
    assert "BUILD_TAG=" not in text
    assert "build_tag=" not in text
    assert "crane copy \"$SOURCE\" \"docker.io/${IMAGE_NAME}:${{ needs.build-and-publish.outputs.immutable_tag }}\"" in text
    assert "ACTUAL_REVISION=$(git rev-parse HEAD)" in text
    assert 'REVISION" != "$ACTUAL_REVISION"' in text
    assert 'COMMIT_CREATED=$(git show -s --format=%cI "$ACTUAL_REVISION")' in text
    # Stable release callers check out the immutable tag; identity is derived
    # inside the reusable workflow and must not be copied from main HEAD.
    release = (ROOT / ".github" / "workflows" / "release-on-pr-merge.yml").read_text(encoding="utf-8")
    publish_start = release.index("publish-stable-image:")
    publish_end = release.index("publish-update-manifest:", publish_start)
    publish_call = release[publish_start:publish_end]
    assert "revision:" not in publish_call
    assert "created:" not in publish_call


def test_reconcile_checks_latest_digest_and_shares_stable_writer_lock():
    workflow, text = _load("docker-reconcile.yml")
    assert workflow["concurrency"]["group"] == "release-stable-writer"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "latest" in text
    assert "docker-content-digest" in text
    assert "version_digest" in text and "latest_digest" in text
    assert "version_missing" in text
    assert "latest_needs_repair" in text
    assert "if: needs.detect.outputs.version_missing == 'true'" in text
    assert "if: needs.detect.outputs.latest_needs_repair == 'true'" in text
    assert "按现有 digest 修复稳定 latest" in text
    assert 'SOURCE="ghcr.io/${IMAGE_NAME}@${VERSION_DIGEST}"' in text
    assert 'TARGET="ghcr.io/${IMAGE_NAME}:latest"' in text
    assert 'crane copy "$SOURCE" "$TARGET"' in text
    # Existing version tags are repaired by digest only; the latest-repair job
    # must not call the reusable build workflow.
    repair = workflow["jobs"]["repair-latest"]
    assert "uses" not in repair
    for status in ("401|403", "429", "5*"):
        assert status in text


def test_main_sync_workflow_runs_for_every_main_push():
    workflow, text = _load("gitflow-sync-main-to-develop.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["push"]["branches"] == ["main"]
    assert "pull_request" not in trigger
    assert "MY_RELEASE_PAT || secrets.GITHUB_TOKEN" in text
    assert "source=main" in text
    assert "target=develop" in text
    assert workflow["concurrency"]["group"] == "gitflow-sync-main-to-develop"
    assert workflow["concurrency"]["cancel-in-progress"] is False
