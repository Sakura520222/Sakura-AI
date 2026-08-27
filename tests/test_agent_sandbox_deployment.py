"""Static deployment contracts for the independent Agent sandbox daemon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
COMPOSE_FILES = (
    ROOT / "docker" / "docker-compose.yml",
    ROOT / "docker" / "docker-compose.prod.yml",
)


def _volumes(service: dict[str, object]) -> list[dict[str, object]]:
    return [item for item in service.get("volumes", []) if isinstance(item, dict)]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_web_receives_only_readonly_independent_sandbox_socket(compose_file: Path):
    config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    web = config["services"]["web"]
    groups = {str(value) for value in web.get("group_add", [])}
    assert "9472" in groups  # Host Updater compatibility contract remains.
    assert "9473" in groups  # Independent sandboxd group.
    assert groups != {"9472"}

    mounts = _volumes(web)
    sandbox_mounts = [
        mount
        for mount in mounts
        if mount.get("target") == "/run/sakura-ai-sandbox"
    ]
    assert sandbox_mounts == [
        {
            "type": "bind",
            "source": "/run/sakura-ai-sandbox",
            "target": "/run/sakura-ai-sandbox",
            "read_only": True,
        }
    ]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_no_compose_service_receives_docker_api_socket(compose_file: Path):
    config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    for service in config["services"].values():
        for mount in _volumes(service):
            assert "docker.sock" not in str(mount.get("source", ""))
            assert "docker.sock" not in str(mount.get("target", ""))


def test_production_workspace_is_host_bind_not_named_volume():
    config = yaml.safe_load(
        (ROOT / "docker" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    )
    web_mounts = _volumes(config["services"]["web"])
    assert {
        "type": "bind",
        "source": "${SAKURA_SANDBOX_WORKSPACE_ROOT:?SAKURA_SANDBOX_WORKSPACE_ROOT must be provided by start.sh}",
        "target": "/app/workplace",
    } in web_mounts
    assert all(
        not (isinstance(item, str) and "workplace_data" in item)
        for item in config.get("volumes", {})
    )


def test_runner_and_daemon_images_are_separate_and_hardened():
    runner = (ROOT / "docker" / "Dockerfile.agent-sandbox").read_text(encoding="utf-8")
    daemon = (ROOT / "docker" / "Dockerfile.sandboxd").read_text(encoding="utf-8")
    assert "USER 65532:65532" in runner
    assert "docker.sock" not in runner
    assert "docker.io" in daemon
    assert "com.sakura-ai.component=\"sandboxd\"" in daemon
    assert runner != daemon


def test_start_script_has_independent_fail_closed_lifecycle():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    for symbol in (
        "ensure_sandboxd_running",
        "sandbox_health_ready",
        "sandbox_identity_matches",
        "sandbox_stop",
        "sandbox_uninstall",
        "cmd_sandbox()",
        "SANDBOX_GID=\"${SANDBOX_GID:-9473}\"",
        "--socket-mode 0660",
        "--mount \"type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock\"",
        "SAKURA_AGENT_RUNNER_IMAGE_DIGEST",
        "SAKURA_SANDBOX_EGRESS_NETWORK",
        "--egress-network \"$SANDBOX_EGRESS_NETWORK\"",
    ):
        assert symbol in script
    assert "SANDBOX_GID=\"${SANDBOX_GID:-9472}\"" not in script
    assert script.index("ensure_sandboxd_running \"$prod\"") < script.index(
        "$COMPOSE up -d"
    )
    assert "workspace_root" in script
    assert "instance_id" in script


def test_named_egress_is_checked_by_startup_and_sandboxd_before_ready():
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    runtime = (ROOT / "sandboxer" / "src" / "sakura_ai_sandboxer" / "docker_runtime.py").read_text(encoding="utf-8")
    app = (ROOT / "sandboxer" / "src" / "sakura_ai_sandboxer" / "app.py").read_text(encoding="utf-8")
    assert 'docker network inspect "$SANDBOX_EGRESS_NETWORK"' in start
    assert '"network",\n                "inspect"' in runtime
    assert "validate_egress_network" in app
    assert "service.mark_runtime_ready()" in app


def test_production_compose_injects_sandbox_identity_contract():
    config = yaml.safe_load(
        (ROOT / "docker" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    )
    environment = config["services"]["web"]["environment"]
    assert environment["AGENT_TEAM_SANDBOX_RUNTIME"] == "${SAKURA_SANDBOX_RUNTIME:-docker}"
    assert environment["AGENT_TEAM_SANDBOX_RUNNER_IMAGE_DIGEST"] == (
        "${SAKURA_AGENT_RUNNER_IMAGE_DIGEST:-}"
    )
    assert environment["AGENT_TEAM_SANDBOX_EXPECTED_INSTANCE_ID"] == (
        "${SAKURA_SANDBOX_INSTANCE_ID:-}"
    )
    assert environment["AGENT_TEAM_SANDBOX_EXPECTED_WORKSPACE_ROOT"] == (
        "${SAKURA_SANDBOX_WORKSPACE_ROOT:?SAKURA_SANDBOX_WORKSPACE_ROOT must be provided by start.sh}"
    )


def test_standalone_source_compose_resolves_absolute_workspace_identity(tmp_path: Path):
    """The source fallback must satisfy sandboxd's host-path admission gate."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")

    # Compose merges stdin overrides with the file configuration, so an
    # ``env_file: []`` override cannot remove the source file's deployment
    # env_file.  Build the smallest source-checkout layout in tmp_path
    # instead; this keeps the probe independent from a developer's runtime
    # .deploy state and from a clean checkout that has no deployment.env.
    temp_root = tmp_path
    (temp_root / "docker").mkdir()
    (temp_root / ".deploy").mkdir()
    (temp_root / ".deploy" / "deployment.env").write_text(
        "# Non-sensitive test fixture\n",
        encoding="utf-8",
    )
    compose_file = temp_root / "docker" / "docker-compose.yml"
    shutil.copy2(ROOT / "docker" / "docker-compose.yml", compose_file)

    environment = os.environ.copy()
    environment.pop("SAKURA_SANDBOX_WORKSPACE_ROOT", None)
    environment.pop("COMPOSE_FILE", None)
    environment["PWD"] = str(temp_root)
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        cwd=temp_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    web = config["services"]["web"]
    expected = web["environment"]["AGENT_TEAM_SANDBOX_EXPECTED_WORKSPACE_ROOT"]
    workspace_mount = next(
        mount
        for mount in _volumes(web)
        if mount.get("target") == "/app/workplace"
    )
    assert Path(expected).is_absolute()
    assert workspace_mount["source"] == expected
    assert Path(expected).resolve() == (temp_root / "workplace").resolve()


def test_standalone_source_compose_honors_explicit_absolute_workspace_override(
    tmp_path: Path,
):
    """An explicit absolute workspace remains stable from an external cwd."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")

    temp_root = tmp_path / "source"
    external_cwd = tmp_path / "external-cwd"
    explicit_workspace = tmp_path / "explicit-workplace"
    (temp_root / "docker").mkdir(parents=True)
    (temp_root / ".deploy").mkdir()
    external_cwd.mkdir()
    explicit_workspace.mkdir()
    (temp_root / ".deploy" / "deployment.env").write_text(
        "# Non-sensitive test fixture\n",
        encoding="utf-8",
    )
    compose_file = temp_root / "docker" / "docker-compose.yml"
    shutil.copy2(ROOT / "docker" / "docker-compose.yml", compose_file)

    environment = os.environ.copy()
    environment.pop("COMPOSE_FILE", None)
    environment["PWD"] = str(external_cwd)
    environment["SAKURA_SANDBOX_WORKSPACE_ROOT"] = str(explicit_workspace)
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        cwd=external_cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    web = config["services"]["web"]
    expected = web["environment"]["AGENT_TEAM_SANDBOX_EXPECTED_WORKSPACE_ROOT"]
    workspace_mount = next(
        mount
        for mount in _volumes(web)
        if mount.get("target") == "/app/workplace"
    )
    assert expected == str(explicit_workspace)
    assert workspace_mount["source"] == expected


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_compose_workspace_source_and_backend_expected_identity_are_same(compose_file: Path):
    config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    web = config["services"]["web"]
    expected = web["environment"]["AGENT_TEAM_SANDBOX_EXPECTED_WORKSPACE_ROOT"]
    workspace_mounts = [
        mount for mount in _volumes(web) if mount.get("target") == "/app/workplace"
    ]
    assert len(workspace_mounts) == 1
    assert workspace_mounts[0]["source"] == expected
    assert "SAKURA_SANDBOX_WORKSPACE_ROOT" in expected
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert 'workspace_root="$SANDBOX_WORKSPACE_ROOT"' in start
    assert '--workspace-root "$SANDBOX_WORKSPACE_ROOT"' in start


def test_start_script_requires_both_production_digests_and_structured_identity():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "SAKURA_SANDBOXD_IMAGE_DIGEST" in script
    assert "production sandbox requires SAKURA_SANDBOXD_IMAGE_DIGEST=NAME@sha256:<64>" in script
    assert "production sandbox requires SAKURA_AGENT_RUNNER_IMAGE_DIGEST=NAME@sha256:<64>" in script
    assert "--label \"ai.sakura.protocol-version=$SANDBOX_PROTOCOL_VERSION\"" in script
    assert "--label \"ai.sakura.runner-image-digest=$runner_ref\"" in script
    assert "sandbox_container_matches_expected" in script
    assert "sandbox_cleanup_known_container" in script
    assert "set(data) != required" in script
    assert "sandbox_fetch_release_digests" in script
    assert "agent-sandbox-manifest.json" in script
    assert "sandbox_ensure_production_digests" in script
    assert "SAKURA_SANDBOX_WORKSPACE_ROOT" in script
    assert 'ai.sakura.egress-network=$SANDBOX_EGRESS_NETWORK' in script


def test_source_and_registry_image_reference_contracts_are_distinct():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "docker image inspect --format '{{.Id}}' \"$SANDBOX_IMAGE\"" in script
    assert "docker image inspect --format '{{.Id}}' \"$SANDBOX_RUNNER_IMAGE\"" in script
    assert "sandbox_registry_digest_is_safe" in script
    assert 'sandbox_pull_image "sandboxd" "$daemon_ref"' in script
    assert 'sandbox_pull_image "Agent runner" "$runner_ref"' in script


def test_upgrade_and_create_failure_paths_are_fail_closed_and_bounded():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    # Existing containers are compared before the early healthy return, and
    # drift goes through exact-ID stop/rm instead of restarting stale config.
    assert "sandbox_container_matches_expected \"$id\" \"$instance\" \"$daemon_ref\" \"$runner_ref\"" in script
    assert "sandbox_cleanup_known_container \"$id\"" in script
    assert "docker start \"$id\"" in script
    # Every post-run failure branch has a known-ID cleanup attempt.
    assert "if ! \"${run_args[@]}\" >/dev/null; then" in script
    assert "if id=$(sandbox_container_id_from_name \"$instance\"); then" in script
    assert "sandbox_cleanup_known_container \"$id\" || true" in script
    assert "sandbox_stop_known_container" in script


def test_state_missing_recovery_uses_exact_name_and_structured_labels():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "sandbox_recover_missing_state_instance" in script
    assert "--no-trunc --filter \"name=^/${SANDBOX_CONTAINER_NAME}$\"" in script
    assert "labels[\"ai.sakura.managed-by\"]" in script
    assert "labels[\"ai.sakura.instance-id\"]" in script
    assert "labels[\"ai.sakura.protocol-version\"]" in script
    assert "re.fullmatch(r\"sandbox-[a-z0-9-]{8,55}\", instance)" in script
    assert "rm -f -- \"$SANDBOX_CONTAINER_ID_FILE\" \"$SANDBOX_IDENTITY_FILE\" \"$SANDBOX_INSTANCE_ID_FILE\"" in script


def test_sandboxd_base_python_is_digest_pinned_and_release_manifest_carries_both():
    daemon = (ROOT / "docker" / "Dockerfile.sandboxd").read_text(encoding="utf-8")
    assert "FROM python:3.14-slim-bookworm@sha256:" in daemon
    release = (ROOT / ".github/workflows/release-on-pr-merge.yml").read_text(encoding="utf-8")
    assert "publish-stable-sandbox.outputs.sandboxd_digest" in release
    assert "publish-stable-sandbox.outputs.runner_digest" in release
    assert '"sandboxd_image"' in release
    assert '"runner_image"' in release
    assert "agent-sandbox-manifest.json" in release
    assert '"manifest":"agent-sandbox"' in release
    assert '"sandbox":{"sandboxd_image"' not in release


def test_start_script_is_bash_valid_when_bash_is_available():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this host")
    result = subprocess.run(
        [bash, "-n", "start.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_sandbox_workflow_builds_both_images_and_has_immutable_output():
    quality = (ROOT / ".github/workflows/sandbox-quality.yml").read_text(encoding="utf-8")
    publish = (ROOT / ".github/workflows/sandbox-publish.yml").read_text(encoding="utf-8")
    assert "sandboxer/tests -q" in quality
    assert "sakura-ai-agent-runner:ci" in quality
    assert "SAKURA_AGENT_RUNNER_IMAGE_DIGEST" in publish or "runner_digest" in publish
    assert "Dockerfile.sandboxd" in publish
    assert "Dockerfile.agent-sandbox" in publish
    assert "docker buildx imagetools inspect" in publish
    assert "@sha256" in publish or "digest" in publish
    assert "Resolve immutable runner image ID" in quality
    assert "SAKURA_SANDBOX_DOCKER_INTEGRATION: '1'" in quality
    assert "SAKURA_AGENT_RUNNER_IMAGE_DIGEST" in quality
    assert 'venv="$RUNNER_TEMP/sandboxer-quality-venv"' in quality
    assert 'python -m venv "$venv"' in quality
    assert 'SANDBOXER_QUALITY_PYTHON=$venv/bin/python' in quality
    assert 'SANDBOXER_QUALITY_RUFF=$venv/bin/ruff' in quality
    assert '"$SANDBOXER_QUALITY_PYTHON" -m pytest sandboxer/tests/integration/' in quality
    assert "sudo --preserve-env=SAKURA_SANDBOX_DOCKER_INTEGRATION,SAKURA_AGENT_RUNNER_IMAGE_DIGEST" in quality
    assert "sudo --preserve-env=PATH" not in quality
    assert "-v /var/run/docker.sock:/var/run/docker.sock" not in quality
    assert "unexpectedly skipped" in quality
