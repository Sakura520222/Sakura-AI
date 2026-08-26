"""Opt-in Linux Docker quality gates for the real sandbox runtime.

The default Windows/source test job skips these tests.  A release runner must
set ``SAKURA_SANDBOX_DOCKER_INTEGRATION=1`` and provide an immutable
``SAKURA_AGENT_RUNNER_IMAGE_DIGEST`` to obtain runtime evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.docker_runtime import (
    DockerRuntimeAdapter,
    _workspace_key_for_relative_identity,
)
from sakura_ai_sandboxer.models import ExecutionProfile, ExecutionRequest


def _integration_config(tmp_path: Path) -> tuple[SandboxdConfig, str]:
    if os.name != "posix":
        pytest.skip("real OCI isolation gate requires a Linux host")
    if os.environ.get("SAKURA_SANDBOX_DOCKER_INTEGRATION") != "1":
        pytest.skip("set SAKURA_SANDBOX_DOCKER_INTEGRATION=1 to run the Docker gate")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    try:
        probe = subprocess.run(
            [docker, "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker daemon is unavailable")
    if probe.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    digest = os.environ.get("SAKURA_AGENT_RUNNER_IMAGE_DIGEST")
    if not digest:
        pytest.skip("immutable runner digest is not configured")
    root = tmp_path / "workplace"
    workspace = root / "owner" / "repo" / "worktrees" / "42-integration"
    workspace.mkdir(parents=True)
    key = _workspace_key_for_relative_identity("owner/repo/worktrees/42-integration")
    return (
        SandboxdConfig(
            workspace_root=str(root),
            runner_image_digest=digest,
            instance_id="sandbox-integration",
            timeout_seconds=30,
            max_timeout_seconds=30,
        ),
        key,
    )


@pytest.mark.asyncio
async def test_real_docker_is_nonroot_offline_readonly_and_cleans_container(tmp_path: Path):
    config, key = _integration_config(tmp_path)
    adapter = DockerRuntimeAdapter(config)
    request = ExecutionRequest(
        request_id="integration-isolation",
        workspace_key=key,
        command=(
            "set -eu; "
            "test \"$(id -u)\" = 65532; "
            "touch /workspace/probe; "
            "test -f /workspace/probe; "
            "python -c 'import sys; assert sys.version_info >= (3, 14)'; "
            "test -w \"$HOME\"; "
            "command -v node; command -v go; command -v rustc; "
            "command -v cargo; command -v java; command -v cc; "
            "command -v stat; "
            "test \"$(stat -c '%u:%g:%a' \"$HOME\")\" = \"65532:65532:700\"; "
            "test ! -w /etc/passwd; "
            "test ! -e /run/sakura-ai-sandbox/sandboxd.sock; "
            "command -v getent; "
            "! getent hosts example.com"
        ),
        profile=ExecutionProfile.AGENT,
        timeout_seconds=20,
    )
    result = await adapter.execute(
        request,
        cancel_event=asyncio.Event(),
        max_output_bytes=4096,
        deadline=asyncio.get_running_loop().time() + 25,
    )
    assert result.exit_code == 0, result.stderr
    assert result.cancelled is False
    assert result.timed_out is False
    assert not adapter._active


def test_docker_create_pins_bind_source_before_host_replacement(tmp_path: Path):
    """Document Docker's create-time bind identity independently of sandboxd.

    The adapter's workspace lease prevents a cooperative Phase 4 manager from
    doing this replacement.  An external root process that ignores the lease
    is outside the threat model; this opt-in helper only records the real
    Docker semantic that a source replacement after ``create`` does not change
    the already configured container mount.
    """

    config, _key = _integration_config(tmp_path)
    docker = shutil.which("docker")
    assert docker is not None
    workspace = tmp_path / "workplace" / "owner" / "repo" / "worktrees" / "42-integration"
    if "," in str(workspace):
        pytest.skip("Docker mount source contains a comma")
    original = workspace / "marker"
    moved = workspace.with_name("42-integration-original")
    original.write_text("original\n", encoding="utf-8")
    name_suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    container_name = f"sakura-mount-identity-{name_suffix}"
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "DOCKER_CONFIG": "/nonexistent/docker-config",
    }
    created = False
    try:
        create = subprocess.run(
            [
                docker,
                "create",
                "--pull",
                "never",
                "--name",
                container_name,
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace,readonly,bind-propagation=rprivate",
                "--entrypoint",
                "/bin/sh",
                config.runner_image_digest or "",
                "-c",
                "cat /workspace/marker",
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        if create.returncode != 0:
            pytest.fail("Docker create failed in the opt-in mount identity test")
        created = True

        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "marker").write_text("replacement\n", encoding="utf-8")

        start = subprocess.run(
            [docker, "start", container_name],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        if start.returncode != 0:
            pytest.fail("Docker start failed in the opt-in mount identity test")
        wait = subprocess.run(
            [docker, "wait", container_name],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        if wait.returncode != 0 or wait.stdout.strip() != b"0":
            pytest.fail("Docker wait failed in the opt-in mount identity test")
        logs = subprocess.run(
            [docker, "logs", container_name],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        assert logs.returncode == 0
        assert logs.stdout == b"original\n"
    finally:
        if created:
            subprocess.run(
                [docker, "rm", "--force", "--volumes", container_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=20,
            )
        if workspace.exists():
            shutil.rmtree(workspace)
        if moved.exists():
            moved.rename(workspace)


@pytest.mark.asyncio
async def test_real_docker_timeout_removes_one_shot_container(tmp_path: Path):
    config, key = _integration_config(tmp_path)
    adapter = DockerRuntimeAdapter(config)
    request = ExecutionRequest(
        request_id="integration-timeout",
        workspace_key=key,
        command="sleep 60",
        profile=ExecutionProfile.AGENT,
        timeout_seconds=1,
    )
    result = await adapter.execute(
        request,
        cancel_event=asyncio.Event(),
        max_output_bytes=4096,
        deadline=asyncio.get_running_loop().time() + 5,
    )
    assert result.timed_out is True
    assert not adapter._active
