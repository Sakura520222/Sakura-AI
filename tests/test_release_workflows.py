"""Static contracts for the reusable updater release workflow and CI job."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "updater-build.yml"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_PATH = ROOT / ".github" / "workflows" / "release-on-pr-merge.yml"
DOCKER_PUBLISH_PATH = ROOT / ".github" / "workflows" / "docker-publish.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SANDBOX_PUBLISH_PATH = WORKFLOWS_DIR / "sandbox-publish.yml"
GITFLOW_SYNC_PATH = WORKFLOWS_DIR / "gitflow-sync-main-to-develop.yml"
WEBUI_BUILD_PATH = WORKFLOWS_DIR / "webui-build.yml"
# Release-creation allowlist: release-on-pr-merge.yml stays the single owner
# of the versioned Release (exactly one `gh release create` / `gh release
# edit`). webui-build.yml is the one allowed extra creator -- it owns only
# the immutable prebuilt-WebUI bundle release (webui-builds-v<version>),
# whose guard is asserted structurally by
# test_webui_bundle_release_is_immutable_and_single_purpose.
ALLOWED_RELEASE_CREATORS = {
    "release-on-pr-merge.yml",
    "webui-build.yml",
}
BUILD_IMAGE = (
    "python:3.14-slim-bookworm@"
    "sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52"
)
RUNTIME_IMAGE = (
    "debian:bookworm-slim@"
    "sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241"
)
EXPECTED_ASSETS = {
    "amd64": "sakura-ai-updater-linux-amd64",
    "arm64": "sakura-ai-updater-linux-arm64",
}


def _load(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    return document, text


def _workflow_triggers(document: dict) -> dict:
    """Handle PyYAML's YAML 1.1 conversion of the `on` key to True."""
    trigger = document.get("on", document.get(True))
    assert isinstance(trigger, dict)
    return trigger


def _job_run_text(job: dict) -> str:
    return "\n".join(
        step.get("run", "") for step in job.get("steps", []) if isinstance(step, dict)
    )


def _step_index(job: dict, needle: str) -> int:
    for index, step in enumerate(job.get("steps", [])):
        if needle in step.get("name", ""):
            return index
    raise AssertionError(f"missing workflow step containing {needle!r}")


def test_updater_workflow_is_reusable_native_matrix_with_two_gates():
    workflow, text = _load(WORKFLOW_PATH)
    triggers = _workflow_triggers(workflow)
    call = triggers["workflow_call"]
    assert call["inputs"]["version"]["required"] is True
    assert call["inputs"]["version"]["type"] == "string"
    assert call["inputs"]["source_ref"]["required"] is True
    assert call["inputs"]["source_ref"]["type"] == "string"

    jobs = workflow["jobs"]
    build = jobs["build-updater"]
    matrix = build["strategy"]["matrix"]["include"]
    assert {
        (entry["arch"], entry["runner"], entry["platform"]) for entry in matrix
    } == {
        ("amd64", "ubuntu-24.04", "linux/amd64"),
        ("arm64", "ubuntu-24.04-arm", "linux/arm64"),
    }
    assert build["runs-on"] == "${{ matrix.runner }}"
    assert build["permissions"]["contents"] == "read"

    steps = build["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.source_ref }}"
    build_index = _step_index(build, "Build onefile")
    runtime_index = _step_index(build, "fresh runtime")
    upload_index = _step_index(build, "Upload updater artifact")
    assert build_index < runtime_index < upload_index

    build_text = _job_run_text(build)
    assert BUILD_IMAGE in text
    assert RUNTIME_IMAGE in text
    assert "updater/build/build.sh" in build_text
    assert "check_glibc.py" in build_text or "build.sh" in build_text
    assert '--platform "${{ matrix.platform }}"' in build_text
    assert "gh release" not in build_text

    runtime_text = steps[runtime_index]["run"]
    helper = (ROOT / "updater" / "build" / "run-fresh-runtime-smoke.sh").read_text(
        encoding="utf-8"
    )
    assert RUNTIME_IMAGE in text
    assert "run-fresh-runtime-smoke.sh" in runtime_text
    assert ":ro" in runtime_text
    assert "--version" in helper
    assert "backend install" in helper
    assert "backend start" in helper
    assert "backend status" in helper
    assert "backend is-running" in helper
    assert "socket_path=/run/sakura-ai/updater.sock" in helper
    assert 'curl --unix-socket "$socket_path"' in helper
    assert "backend stop" in helper
    assert "remained running" in helper

    upload = steps[upload_index]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["retention-days"] == 1
    assert "matrix.arch" in upload["with"]["name"]
    assert "github.run_id" in upload["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"


def test_publish_is_single_writer_and_uploads_only_two_binaries_and_checksum():
    workflow, text = _load(WORKFLOW_PATH)
    publish = workflow["jobs"]["publish-updater-assets"]
    assert publish["needs"] == "build-updater"
    assert publish["runs-on"] == "ubuntu-24.04"
    assert publish["permissions"]["contents"] == "write"

    publish_step = publish["steps"][_step_index(publish, "Verify assets")]
    assert publish_step["env"]["GH_REPO"] == "${{ github.repository }}"

    download_steps = [
        step
        for step in publish["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert len(download_steps) == 2
    assert {step["with"]["path"] for step in download_steps} == {
        "release-assets/amd64",
        "release-assets/arm64",
    }

    publish_text = _job_run_text(publish)
    for asset in EXPECTED_ASSETS.values():
        assert asset in publish_text
    assert 'release_dir="$source_root/final"' in publish_text
    assert "find" in publish_text
    assert "! -L" in publish_text
    assert "-s" in publish_text
    assert "sha256sum" in publish_text
    checksum_index = publish_text.index("sha256sum")
    amd64_index = publish_text.index('"sakura-ai-updater-linux-amd64"', checksum_index)
    arm64_index = publish_text.index('"sakura-ai-updater-linux-arm64"', checksum_index)
    assert amd64_index < arm64_index
    assert "SHA256SUMS" in publish_text
    assert "wc -l" in publish_text or "mapfile" in publish_text

    assert publish_text.count('gh release view "v${VERSION}"') == 1
    assert publish_text.count('gh release upload "v${VERSION}"') == 1
    assert "sakura-ai-updater-linux-amd64" in publish_text
    assert "sakura-ai-updater-linux-arm64" in publish_text
    assert "--clobber" in publish_text
    assert "gh release create" not in text
    assert "gh release edit" not in text
    assert "latest" not in text.lower()
    assert "update-manifest.json" not in text


def test_release_workflow_keeps_single_owner_and_source_asset_cleanup_contract():
    release, text = _load(RELEASE_PATH)
    jobs = release["jobs"]
    build = jobs["build-and-upload-assets"]
    updater = jobs["publish-updater-assets"]
    stable = jobs["publish-stable-image"]

    assert text.count("gh release create") == 1
    assert text.count("gh release edit") == 1
    for workflow_path in WORKFLOWS_DIR.glob("*.yml"):
        workflow_text = workflow_path.read_text(encoding="utf-8")
        if workflow_path.name in ALLOWED_RELEASE_CREATORS:
            continue
        assert "gh release create" not in workflow_text
        assert "gh release edit" not in workflow_text
    assert ".assets[].name" not in text
    check_assets = next(
        step
        for step in build["steps"]
        if step.get("name") == "检查并清理 Release 附件状态"
    )
    cleanup_text = check_assets["run"]
    assert "Sakura-AI-v${VERSION}.tar.gz" in cleanup_text
    assert "Sakura-AI-v${VERSION}.zip" in cleanup_text
    assert "gh release delete-asset" in cleanup_text
    assert "for asset in" not in cleanup_text
    assert "source upload" not in cleanup_text.lower()

    upload_text = _job_run_text(build)
    assert 'gh release upload "$TAG_NAME"' in upload_text
    assert '"${ASSET_NAME}.tar.gz" "${ASSET_NAME}.zip" --clobber' in upload_text

    assert updater["needs"] == ["generate-release", "build-and-upload-assets"]
    assert "needs.generate-release.result == 'success'" in updater["if"]
    assert "needs.build-and-upload-assets.result == 'success'" in updater["if"]
    assert updater["uses"] == "./.github/workflows/updater-build.yml"
    assert updater["with"] == {
        "version": "${{ needs.generate-release.outputs.version }}",
        "source_ref": "refs/tags/v${{ needs.generate-release.outputs.version }}",
    }
    assert updater["secrets"] == "inherit"
    assert "runs-on" not in updater
    assert "steps" not in updater

    # The allowed second release writer must stay driven by the release
    # pipeline itself: pinned to the immutable tag with a matching
    # release_tag so webui-build.yml never invents its own version.
    # webui-build's input is `source_revision` (immutable SHA/tag contract);
    # updater-build/docker-publish/sandbox-publish keep their own `source_ref`.
    webui = jobs["publish-webui-assets"]
    assert webui["needs"] == "generate-release"
    assert webui["if"] == "needs.generate-release.result == 'success'"
    assert webui["uses"] == "./.github/workflows/webui-build.yml"
    assert webui["with"] == {
        "source_revision": "refs/tags/v${{ needs.generate-release.outputs.version }}",
        "release_tag": "v${{ needs.generate-release.outputs.version }}",
    }
    assert "runs-on" not in webui
    assert "steps" not in webui

    assert stable["needs"] == ["generate-release", "publish-webui-assets"]
    assert stable["if"] == (
        "needs.generate-release.result == 'success'"
        " && needs.publish-webui-assets.result == 'success'"
    )
    assert stable["with"]["source_ref"] == (
        "refs/tags/v${{ needs.generate-release.outputs.version }}"
    )
    assert stable["with"]["channel"] == "stable"
    assert stable["with"]["version"] == "${{ needs.generate-release.outputs.version }}"
    assert stable["with"]["webui_artifact"] == (
        "${{ needs.publish-webui-assets.outputs.artifact_name }}"
    )
    assert release["concurrency"]["cancel-in-progress"] is False


def test_webui_bundle_release_is_immutable_and_single_purpose():
    """webui-build.yml 是唯一被允许的额外 Release 创建者：只为不可变预构建
    bundle（webui-builds-v<version>）创建 Release，所有上传经 publish_file()
    字节比对守卫，绝不覆盖已发布字节，也绝不创建/编辑版本化 Release。"""
    workflow, text = _load(WEBUI_BUILD_PATH)
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["permissions"]["contents"] == "write"
    assert publish["concurrency"] == {
        "group": "webui-release-assets",
        "cancel-in-progress": False,
    }
    publish_text = _job_run_text(publish)

    # Exactly one creation: the immutable bundle tag keyed by the built
    # version, targeting the built revision, and never claiming "latest".
    assert text.count("gh release create") == 1
    assert 'gh release create "$build_tag"' in publish_text
    assert 'build_tag="webui-builds-v$VERSION"' in publish_text
    assert '--target "$REVISION"' in publish_text
    assert "--prerelease" in publish_text
    assert "--latest=false" in publish_text
    assert "gh release edit" not in text
    assert 'gh release create "$RELEASE_TAG"' not in publish_text

    # Tag/version consistency: versioned-Release assets are only published
    # when the release tag matches the built version exactly, and the
    # commit-keyed bundle assets carry the full source revision.
    assert '[[ "$RELEASE_TAG" == "v$VERSION" ]] || exit 1' in publish_text
    assert "sakura-ai-webui-$REVISION.zip" in publish_text

    # Every upload goes through publish_file(): same-name remote assets are
    # downloaded and compared first; differing bytes abort instead of being
    # replaced. No upload path may bypass the guard.
    assert publish_text.count("gh release upload") == 1
    assert 'gh release upload "$tag" "$file"' in publish_text
    assert "Refusing to overwrite a different WebUI artifact" in publish_text
    assert 'publish_file "$RELEASE_TAG"' in publish_text


def test_source_archive_uses_unified_config_section_contract():
    release, text = _load(RELEASE_PATH)
    build = release["jobs"]["build-and-upload-assets"]
    structure = next(
        step for step in build["steps"] if step.get("name") == "验证项目结构"
    )
    package = next(
        step for step in build["steps"] if step.get("name") == "创建发布资源包"
    )

    structure_text = structure["run"]
    package_text = package["run"]

    # strategies.yaml/labels.yaml were migrated into the app_config sections;
    # the release workflow must validate the new source of built-in defaults.
    assert "config/strategies.yaml" not in text
    assert "config/labels.yaml" not in text
    assert "backend/core/config_section_defaults.py" in structure_text

    # connection.json is deployment-time state and is intentionally ignored.
    # The source archive still carries the runtime directory for Setup/Compose.
    assert 'cp -r config "$RELEASE_DIR/"' not in package_text
    assert 'mkdir -p "$RELEASE_DIR/config"' in package_text


def test_source_archive_keeps_uv_and_pip_install_paths_executable():
    release, _ = _load(RELEASE_PATH)
    build = release["jobs"]["build-and-upload-assets"]
    package = next(
        step for step in build["steps"] if step.get("name") == "创建发布资源包"
    )
    package_text = package["run"]

    # The README source-development instructions (uv sync and the classic
    # pip fallback with `pip install -e './updater[dev]'`) must stay
    # executable inside the release archive, so the archive has to carry
    # the uv project files and the updater package source.
    assert 'cp pyproject.toml "$RELEASE_DIR/"' in package_text
    assert 'cp uv.lock "$RELEASE_DIR/"' in package_text
    assert 'cp .python-version "$RELEASE_DIR/"' in package_text
    assert 'cp -r updater "$RELEASE_DIR/"' in package_text


def test_docker_hub_stable_sync_tags_the_copied_docker_hub_image():
    workflow, _ = _load(DOCKER_PUBLISH_PATH)
    sync = workflow["jobs"]["sync-dockerhub"]
    run_text = _job_run_text(sync)

    assert (
        'crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:v${{ inputs.version }}"'
        in run_text
    )
    assert (
        'crane tag "docker.io/${IMAGE_NAME}:v${{ inputs.version }}" latest' in run_text
    )
    assert 'crane tag "$SOURCE" latest' not in run_text
    assert 'crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:edge"' in run_text


def test_development_web_and_sandbox_tags_share_the_full_revision_identity():
    _, web_text = _load(DOCKER_PUBLISH_PATH)
    _, sandbox_text = _load(SANDBOX_PUBLISH_PATH)

    # All builds carry the same full revision. The publication join binds
    # the two sandbox build output digests into the canonical Web OCI index.
    assert 'DEV_TAG="dev-${UTC_CREATED}-v${VERSION}-${REVISION}"' in web_text
    assert 'primary="dev-${utc_created}-v${VERSION}-${actual}"' in sandbox_text
    assert 'immutable="sha-${actual:0:40}"' in sandbox_text
    assert 'echo "revision=$actual"' in sandbox_text
    assert (
        "org.opencontainers.image.revision=${{ steps.tags.outputs.revision }}"
        in sandbox_text
    )
    assert (
        "org.opencontainers.image.version=${{ steps.tags.outputs.version }}"
        in sandbox_text
    )
    assert "com.sakura-ai.build.channel=${{ inputs.channel }}" in sandbox_text
    assert 'com.sakura-ai.component="web"' in (
        ROOT / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert 'com.sakura-ai.component="agent-runner"' in (
        ROOT / "docker" / "Dockerfile.agent-sandbox"
    ).read_text(encoding="utf-8")
    assert "org.opencontainers.image.revision=${{ github.sha }}" not in sandbox_text
    assert "name=${{ env.SANDBOXD_REPOSITORY }},push-by-digest=true" in sandbox_text
    assert "name=${{ env.RUNNER_REPOSITORY }},push-by-digest=true" in sandbox_text
    edge, _ = _load(ROOT / ".github/workflows/docker-edge.yml")
    assert set(edge["jobs"]["publish-deployment"]["needs"]) == {
        "publish-edge",
        "publish-sandbox-edge",
    }


def test_gitflow_failure_notification_uses_validated_environment_branches():
    workflow, _ = _load(GITFLOW_SYNC_PATH)
    branches = next(
        step
        for step in workflow["jobs"]["sync-main-to-develop"]["steps"]
        if step.get("id") == "branches"
    )
    branch_run = branches["run"]
    assert "INPUT_SOURCE_BRANCH" in branches["env"]
    assert "INPUT_TARGET_BRANCH" in branches["env"]
    assert "git check-ref-format --branch" in branch_run
    assert 'validate_branch "$source"' in branch_run
    assert 'validate_branch "$target"' in branch_run

    notify = next(
        step
        for step in workflow["jobs"]["sync-main-to-develop"]["steps"]
        if step.get("name") == "同步失败时创建 Issue"
    )
    assert notify["env"] == {
        "SOURCE_BRANCH": "${{ steps.branches.outputs.source }}",
        "TARGET_BRANCH": "${{ steps.branches.outputs.target }}",
    }
    script = notify["with"]["script"]
    assert "process.env.SOURCE_BRANCH" in script
    assert "process.env.TARGET_BRANCH" in script
    assert "validBranch" in script
    assert "'${{ steps.branches.outputs.source }}'" not in script
    assert "'${{ steps.branches.outputs.target }}'" not in script
    assert "steps.branches.outputs.source" not in script
    assert "steps.branches.outputs.target" not in script


def test_gitflow_token_fallback_publishes_complete_deployment():
    workflow, text = _load(GITFLOW_SYNC_PATH)
    jobs = workflow["jobs"]
    web = jobs["publish-synchronized-development"]
    sandbox = jobs["publish-synchronized-development-sandbox"]

    assert web["uses"] == "./.github/workflows/docker-publish.yml"
    assert (
        web["with"]["source_ref"]
        == "${{ needs.sync-main-to-develop.outputs.revision }}"
    )
    assert web["with"]["channel"] == "development"
    assert sandbox["uses"] == "./.github/workflows/sandbox-publish.yml"
    assert sandbox["needs"] == [
        "sync-main-to-develop",
        "publish-synchronized-development",
    ]
    assert (
        sandbox["with"]["source_ref"]
        == "${{ needs.sync-main-to-develop.outputs.revision }}"
    )
    assert sandbox["with"]["channel"] == "development"
    condition = sandbox["if"]
    assert "needs.sync-main-to-develop.outputs.changed == 'true'" in condition
    assert "needs.sync-main-to-develop.outputs.using_pat != 'true'" in condition
    assert "needs.publish-synchronized-development.result == 'success'" in condition
    deployment = jobs["publish-synchronized-development-deployment"]
    mirror = jobs["mirror-synchronized-development"]
    assert deployment["needs"] == [
        "sync-main-to-develop",
        "publish-synchronized-development",
        "publish-synchronized-development-sandbox",
    ]
    assert deployment["runs-on"] == "ubuntu-24.04"
    assert deployment["concurrency"] == {
        "group": "docker-development",
        "cancel-in-progress": False,
    }
    assert "scripts/publish_development_deployment.py" in text
    assert (
        "DEPLOYMENT_TAG: ${{ needs.publish-synchronized-development.outputs.immutable_tag }}"
        in text
    )
    assert (
        "WEB_DIGEST: ${{ needs.publish-synchronized-development.outputs.digest }}"
        in text
    )
    assert (
        "SANDBOXD_DIGEST: ${{ needs.publish-synchronized-development-sandbox.outputs.sandboxd_digest }}"
        in text
    )
    assert (
        "RUNNER_DIGEST: ${{ needs.publish-synchronized-development-sandbox.outputs.runner_digest }}"
        in text
    )
    assert "advanced: ${{ steps.deployment.outputs.advanced }}" in text
    assert 'echo "advanced=false" >> "$GITHUB_OUTPUT"' in text
    assert 'echo "advanced=true" >> "$GITHUB_OUTPUT"' in text
    assert (
        "needs.publish-synchronized-development-deployment.outputs.advanced == 'true'"
        in mirror["if"]
    )
    assert mirror["needs"] == [
        "publish-synchronized-development",
        "publish-synchronized-development-deployment",
    ]
    assert mirror["permissions"] == {}
    assert mirror["continue-on-error"] is True
    assert (
        "DEPLOYMENT_DIGEST: ${{ needs.publish-synchronized-development-deployment.outputs.digest }}"
        in text
    )
    assert 'crane tag "docker.io/sakura520222/sakura-ai:${DEPLOYMENT_TAG}" edge' in text
    assert web["with"]["sync_dockerhub"] is False


def test_publish_update_manifest_waits_for_release_assets_and_stable_image():
    release, text = _load(RELEASE_PATH)
    manifest = release["jobs"]["publish-update-manifest"]

    assert manifest["needs"] == [
        "generate-release",
        "publish-webui-assets",
        "publish-updater-assets",
        "publish-stable-image",
        "publish-stable-sandbox",
    ]
    condition = manifest["if"].strip()
    assert condition.startswith("always()")
    assert "needs.generate-release.result == 'success'" in condition
    assert "needs.publish-webui-assets.result == 'success'" in condition
    assert "needs.publish-updater-assets.result == 'success'" in condition
    assert "needs.publish-stable-image.result == 'success'" in condition
    assert "needs.publish-stable-image.result == 'skipped'" not in condition
    assert "needs.publish-stable-sandbox.result == 'success'" in condition

    checkout = next(
        step
        for step in manifest["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == (
        "refs/tags/v${{ needs.generate-release.outputs.version }}"
    )

    source_assets = release["jobs"]["build-and-upload-assets"]
    source_checkout = next(
        step
        for step in source_assets["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert source_checkout["with"]["ref"] == (
        "refs/tags/v${{ needs.generate-release.outputs.version }}"
    )

    run_text = _job_run_text(manifest)
    assert "VERSION: ${{ needs.generate-release.outputs.version }}" in text
    assert "docker manifest inspect" in run_text
    assert "update-manifest.json" in run_text
    assert 'gh release upload "$TAG_NAME" update-manifest.json --clobber' in run_text
    assert (
        'gh release upload "$TAG_NAME" agent-sandbox-manifest.json --clobber'
        in run_text
    )
    assert "gh release create" not in run_text
    assert "gh release edit" not in run_text
    assert "${{ inputs.version }}" not in text

    # The generated manifest is owned by this job; source archives and the
    # reusable updater workflow must not accidentally package or upload it.
    for job_id, job in release["jobs"].items():
        if job_id == "publish-update-manifest":
            continue
        assert "update-manifest.json" not in _job_run_text(job)

    assert '"updater":{"protocol_version"' in run_text
    assert '"asset_linux_amd64"' in run_text
    assert '"asset_linux_arm64"' in run_text
    assert "cat > agent-sandbox-manifest.json" in run_text
    assert '"manifest":"agent-sandbox"' in run_text
    assert '"sandboxd_image"' in run_text
    assert '"runner_image"' in run_text
    # The updater v1 manifest remains strict and must not receive sandbox
    # extension fields; the independent asset carries them instead.
    assert '"sandbox":{"sandboxd_image"' not in run_text
    assert '"deployment":{"env"' not in run_text

    smoke = next(
        step
        for step in manifest["steps"]
        if step.get("name") == "验证已发布 updater 的 HTTPS 就绪性"
    )
    smoke_text = smoke["run"]
    assert smoke["env"]["RUNTIME_IMAGE"] == RUNTIME_IMAGE
    assert "gh release download" in smoke_text
    assert "sakura-ai-updater-linux-amd64" in smoke_text
    assert "run-fresh-runtime-smoke.sh /mnt/sakura-ai-updater 1" in smoke_text
    assert "--platform linux/amd64" in smoke_text


def test_release_notes_require_ai_success_without_a_client_timeout():
    """AI failure must stop publication instead of publishing commit titles."""
    release, _ = _load(RELEASE_PATH)
    job = release["jobs"]["generate-release"]

    # main↔develop 回流会产生同名不同 SHA 的提交，仍须排除 merge 并按标题去重。
    facts = next(
        step for step in job["steps"] if step.get("name") == "收集变更事实信号"
    )
    facts_text = facts["run"]
    assert "--no-merges" in facts_text
    assert "!seen[$0]++" in facts_text
    for artifact in (
        "subjects.txt",
        "changed_paths.txt",
        "changed_areas.txt",
        "sensitive_stat.txt",
        "sensitive_diffs.txt",
    ):
        assert artifact in facts_text
    # 敏感文件 diff 不做预算截断（模型上下文 1M）
    assert "PER_FILE_CAP" not in facts_text
    assert "TOTAL_CAP" not in facts_text
    # 文档信号：只传中文 README（不含 README_EN）与 docs 下的 Markdown
    assert '"README.md"' in facts_text
    assert "docs/**/*.md" in facts_text
    assert "README*.md" not in facts_text

    ai_step = next(
        step for step in job["steps"] if step.get("name") == "AI 生成 Release 说明"
    )
    ai_text = ai_step["run"]
    assert not ai_step.get("continue-on-error", False)
    assert "if" not in ai_step
    assert "timeout-minutes" not in ai_step
    assert "timeout-minutes" not in job
    assert "timeout=" not in ai_text
    assert "fail_fallback" not in ai_text
    assert not any(
        step.get("name", "").startswith("生成 Release 描述") for step in job["steps"]
    )
    # AI 只消费收集好的事实信号，不得自行 git log
    assert "git log" not in ai_text
    # 版本定位并入 summary 两段结构，不引入独立 version_note 字段
    assert "version_note" not in ai_text
    assert "版本定位" in ai_text
    assert '"upgrade_notes"' in ai_text
    assert '"important_notes"' in ai_text
    for level in ('"note"', '"important"', '"warning"'):
        assert level in ai_text
    # 新章节与 GitHub Alert 映射（WebUI marked 端降级为引用块仍可读）
    assert "### 升级提示 / Upgrade Notes" in ai_text
    assert "### 注意事项 / Important Notes" in ai_text
    for alert in ("[!NOTE]", "[!IMPORTANT]", "[!WARNING]"):
        assert alert in ai_text
    # 浏览器伪装头：网关按浏览器请求放行，默认 urllib UA/缺 Origin/Referer 会 403
    assert '"User-Agent"' in ai_text
    assert '"Origin"' in ai_text
    assert '"Referer"' in ai_text
    # HTTP 错误须读取响应体，让 4xx 的真实拒绝原因可从日志定位
    assert "e.read()" in ai_text


def _release_notes_response(content: str) -> io.BytesIO:
    return io.BytesIO(
        json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    )


@pytest.fixture
def release_notes_execution(monkeypatch, tmp_path):
    release, _ = _load(RELEASE_PATH)
    step = next(
        step
        for step in release["jobs"]["generate-release"]["steps"]
        if step.get("name") == "AI 生成 Release 说明"
    )
    script = step["run"].partition("<<'PY'\n")[2].rsplit("\nPY", 1)[0]
    assert script
    facts_dir = tmp_path / "release_facts"
    facts_dir.mkdir()
    (facts_dir / "subjects.txt").write_text("fix: preserve release notes\n")
    for name, value in {
        "LLM_BASE_URL": "https://example.invalid/v1",
        "LLM_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
        "VERSION": "3.2.5",
        "DATE": "2026-11-05",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    request = Mock()
    sleep = Mock()
    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr("time.sleep", sleep)

    def run():
        # Execute the repository-owned workflow code with mocked network I/O.
        exec(compile(script, str(RELEASE_PATH), "exec"), {"__name__": "__main__"})  # noqa: S102

    return run, request, sleep, tmp_path / "release_notes.txt"


def test_release_notes_retry_network_failure_then_publish_ai_content(
    release_notes_execution,
):
    run, request, sleep, notes = release_notes_execution
    content = json.dumps(
        {
            "summary_zh": "维护版本。\n\n修复发布说明。",
            "summary_en": "Maintenance release.\n\nFix release notes.",
            "groups": [
                {
                    "type": "fix",
                    "title_zh": "问题修复",
                    "title_en": "Bug Fixes",
                    "items": [{"zh": "保留完整说明", "en": "Preserve complete notes"}],
                }
            ],
        }
    )
    request.side_effect = [
        URLError("connection reset"),
        _release_notes_response(content),
    ]

    run()

    assert request.call_count == 2
    assert all(not call.kwargs for call in request.call_args_list)
    sleep.assert_called_once()
    result = notes.read_text(encoding="utf-8")
    assert "保留完整说明" in result
    assert "Preserve complete notes" in result
    assert "<summary>English Changelog</summary>" in result


def test_release_notes_exhausted_network_errors_fail_publication(
    release_notes_execution,
):
    run, request, sleep, notes = release_notes_execution
    request.side_effect = URLError("connection reset")

    with pytest.raises(SystemExit) as exc:
        run()

    assert exc.value.code == 1
    assert request.call_count == 3
    assert sleep.call_count == 2
    assert not notes.exists()


@pytest.mark.parametrize("content", ["not JSON", '{"summary_zh": "摘要"}'])
def test_release_notes_invalid_ai_content_fails_publication(
    release_notes_execution, content
):
    run, request, _, notes = release_notes_execution
    request.return_value = _release_notes_response(content)

    with pytest.raises(SystemExit) as exc:
        run()

    assert exc.value.code == 1
    assert not notes.exists()


def test_release_notes_missing_credentials_fails_before_request(
    release_notes_execution, monkeypatch
):
    run, request, _, notes = release_notes_execution
    monkeypatch.delenv("LLM_API_KEY")

    with pytest.raises(SystemExit) as exc:
        run()

    assert exc.value.code == 1
    request.assert_not_called()
    assert not notes.exists()


def test_ci_keeps_main_job_and_adds_independent_updater_quality():
    ci, _ = _load(CI_PATH)
    jobs = ci["jobs"]
    assert "python-quality" in jobs
    assert "updater-quality" in jobs
    assert jobs["python-quality"]["runs-on"] == "ubuntu-latest"
    assert jobs["python-quality"]["permissions"] == {"contents": "read"}

    updater = jobs["updater-quality"]
    assert updater["runs-on"] == "ubuntu-latest"
    assert updater["permissions"] == {"contents": "read"}
    steps = updater["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    setup_python = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert checkout["uses"].startswith("actions/checkout@")
    assert setup_python["uses"].startswith("actions/setup-python@")
    assert setup_python["with"]["python-version"] == "3.14"

    run_text = _job_run_text(updater)
    assert "pip install -e './updater[dev]' ruff" in run_text
    assert "ruff check updater" in run_text
    assert "pytest updater/tests -q" in run_text
    assert "pytest updater/tests/test_build_config.py -q" in run_text
