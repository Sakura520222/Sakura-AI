"""Static contracts for the reusable updater release workflow and CI job."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "updater-build.yml"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_PATH = ROOT / ".github" / "workflows" / "release-on-pr-merge.yml"
DOCKER_PUBLISH_PATH = ROOT / ".github" / "workflows" / "docker-publish.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
BUILD_IMAGE = (
    "python:3.12-slim-bullseye@"
    "sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db"
)
RUNTIME_IMAGE = (
    "debian:bullseye-slim@"
    "sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792"
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
        step.get("run", "")
        for step in job.get("steps", [])
        if isinstance(step, dict)
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
        (entry["arch"], entry["runner"], entry["platform"])
        for entry in matrix
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
    assert "--platform \"${{ matrix.platform }}\"" in build_text
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
    assert 'socket_path=/run/sakura-ai/updater.sock' in helper
    assert 'curl --unix-socket "$socket_path"' in helper
    assert "backend stop" in helper
    assert "remained running" in helper

    upload = steps[upload_index]
    assert upload["uses"] == "actions/upload-artifact@v4"
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
        if step.get("uses") == "actions/download-artifact@v5"
    ]
    assert len(download_steps) == 2
    assert {step["with"]["path"] for step in download_steps} == {
        "release-assets/amd64",
        "release-assets/arm64",
    }

    publish_text = _job_run_text(publish)
    for asset in EXPECTED_ASSETS.values():
        assert asset in publish_text
    assert "release_dir=\"$source_root/final\"" in publish_text
    assert "find" in publish_text
    assert "! -L" in publish_text
    assert "-s" in publish_text
    assert "sha256sum" in publish_text
    checksum_index = publish_text.index("sha256sum")
    amd64_index = publish_text.index(
        '"sakura-ai-updater-linux-amd64"', checksum_index
    )
    arm64_index = publish_text.index(
        '"sakura-ai-updater-linux-arm64"', checksum_index
    )
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
        if workflow_path.name != "release-on-pr-merge.yml":
            assert "gh release create" not in workflow_text
            assert "gh release edit" not in workflow_text
    assert ".assets[].name" not in text
    check_assets = next(
        step for step in build["steps"] if step.get("name") == "检查并清理 Release 附件状态"
    )
    cleanup_text = check_assets["run"]
    assert 'Sakura-AI-v${VERSION}.tar.gz' in cleanup_text
    assert 'Sakura-AI-v${VERSION}.zip' in cleanup_text
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

    assert stable["needs"] == "generate-release"
    assert stable["if"] == "needs.generate-release.result == 'success'"
    assert stable["with"]["source_ref"] == (
        "refs/tags/v${{ needs.generate-release.outputs.version }}"
    )
    assert stable["with"]["channel"] == "stable"
    assert stable["with"]["version"] == "${{ needs.generate-release.outputs.version }}"
    assert release["concurrency"]["cancel-in-progress"] is False


def test_docker_hub_stable_sync_tags_the_copied_docker_hub_image():
    workflow, _ = _load(DOCKER_PUBLISH_PATH)
    sync = workflow["jobs"]["sync-dockerhub"]
    run_text = _job_run_text(sync)

    assert (
        'crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:v${{ inputs.version }}"'
        in run_text
    )
    assert (
        'crane tag "docker.io/${IMAGE_NAME}:v${{ inputs.version }}" latest'
        in run_text
    )
    assert 'crane tag "$SOURCE" latest' not in run_text
    assert 'crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:edge"' in run_text


def test_publish_update_manifest_waits_for_release_assets_and_stable_image():
    release, text = _load(RELEASE_PATH)
    manifest = release["jobs"]["publish-update-manifest"]

    assert manifest["needs"] == [
        "generate-release",
        "publish-updater-assets",
        "publish-stable-image",
    ]
    condition = manifest["if"].strip()
    assert condition.startswith("always()")
    assert "needs.generate-release.result == 'success'" in condition
    assert "needs.publish-updater-assets.result == 'success'" in condition
    assert "needs.publish-stable-image.result == 'success'" in condition
    assert "needs.publish-stable-image.result == 'skipped'" not in condition

    checkout = next(
        step
        for step in manifest["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["uses"] == "actions/checkout@v7"
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
    assert 'VERSION: ${{ needs.generate-release.outputs.version }}' in text
    assert "docker manifest inspect" in run_text
    assert "update-manifest.json" in run_text
    assert 'gh release upload "$TAG_NAME" update-manifest.json --clobber' in run_text
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
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    setup_python = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert checkout["uses"] == "actions/checkout@v7"
    assert setup_python["uses"] == "actions/setup-python@v7"
    assert setup_python["with"]["python-version"] == "3.12"

    run_text = _job_run_text(updater)
    assert "pip install -e './updater[dev]' ruff" in run_text
    assert "ruff check updater" in run_text
    assert "pytest updater/tests -q" in run_text
    assert "pytest updater/tests/test_build_config.py -q" in run_text
