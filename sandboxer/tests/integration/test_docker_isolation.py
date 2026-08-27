"""Opt-in Linux Docker quality gates for the real sandbox runtime.

The default Windows/source test job skips these tests.  A release runner must
set ``SAKURA_SANDBOX_DOCKER_INTEGRATION=1`` and provide an immutable
``SAKURA_AGENT_RUNNER_IMAGE_DIGEST`` to obtain runtime evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.docker_runtime import (
    DockerRuntimeAdapter,
    _workspace_key_for_relative_identity,
)
from sakura_ai_sandboxer.models import (
    ExecutionProfile,
    ExecutionRequest,
    NetworkMode,
)

_DOCKER_DIAGNOSTIC_LIMIT = 2048
_DOCKER_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_DOCKER_SECRET_RE = re.compile(
    r"(?i)(?P<key>authorization|cookie|password|secret|token|api[_-]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_DOCKER_DIGEST_RE = re.compile(r"(?i)sha256:[0-9a-f]{64}")


def _redact_docker_stderr(value: bytes | str, *, tmp_path: Path) -> str:
    """Return bounded Docker stderr safe to include in CI failure output.

    Docker errors may echo bind sources, image IDs, or request/environment
    fragments.  This helper is intentionally test-only: the production
    adapter continues to collapse these failures to its typed generic error.
    Keep the small amount of text that is useful for classifying a runner
    failure, while removing paths, digests, and credential-shaped values.
    """

    if isinstance(value, bytes):
        text = value[: _DOCKER_DIAGNOSTIC_LIMIT * 4].decode(
            "utf-8", errors="replace"
        )
    else:
        text = value[: _DOCKER_DIAGNOSTIC_LIMIT * 4]
    text = _DOCKER_ANSI_RE.sub("", text).replace("\x00", "")
    text = text.replace(str(tmp_path), "<tmp-path>")
    text = _DOCKER_SECRET_RE.sub(r"\g<key>=<redacted>", text)
    text = _DOCKER_DIGEST_RE.sub("<image-digest>", text)
    # A Docker daemon can report a path after the exact pytest directory has
    # already been normalized (for example after resolving a symlink).  Do
    # not print arbitrary absolute host paths from that fallback text.
    text = re.sub(
        r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:[\\/]|/)(?:[^\s'\"]+)",
        "<host-path>",
        text,
    )
    text = " ".join(text.split())
    return text[:_DOCKER_DIAGNOSTIC_LIMIT] or "<empty>"


async def _execute_with_docker_diagnostics(
    adapter: DockerRuntimeAdapter,
    request: ExecutionRequest,
    *,
    tmp_path: Path,
    deadline: float,
):
    """Run the real adapter and attach sanitized create stderr on failure.

    The runtime intentionally exposes only generic typed errors to callers.
    For this opt-in CI gate, wrap the command seam locally so a failed create
    still leaves actionable, bounded evidence in the pytest failure without
    changing the production error contract.
    """

    original_run_command = adapter._run_command
    observed: list[tuple[int, bytes]] = []

    async def capture_create_result(argv: tuple[str, ...], command_deadline: float):
        result = await original_run_command(argv, command_deadline)
        if len(argv) > 1 and argv[1] == "create" and result.returncode != 0:
            observed.append((result.returncode, result.stderr))
        return result

    adapter._run_command = capture_create_result
    try:
        return await adapter.execute(
            request,
            cancel_event=asyncio.Event(),
            max_output_bytes=4096,
            deadline=deadline,
        )
    except Exception:
        if observed:
            return pytest.fail(
                "Docker adapter create failed "
                f"(returncode={observed[0][0]}): "
                f"{_redact_docker_stderr(observed[0][1], tmp_path=tmp_path)}"
            )
        raise
    finally:
        adapter._run_command = original_run_command


def test_docker_stderr_diagnostic_is_bounded_and_redacted(tmp_path: Path):
    raw = (
        f"invalid mount source {tmp_path / 'workplace'} "
        "token=secret-value sha256="
        + "a" * 64
        + " "
        + "x" * (_DOCKER_DIAGNOSTIC_LIMIT * 2)
    )
    diagnostic = _redact_docker_stderr(raw, tmp_path=tmp_path)

    assert len(diagnostic) <= _DOCKER_DIAGNOSTIC_LIMIT
    assert str(tmp_path) not in diagnostic
    assert "secret-value" not in diagnostic
    assert "sha256:" not in diagnostic
    assert "invalid mount source" in diagnostic


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
        network_mode=NetworkMode.NONE,
        timeout_seconds=20,
    )
    result = await _execute_with_docker_diagnostics(
        adapter,
        request,
        tmp_path=tmp_path,
        deadline=asyncio.get_running_loop().time() + 25,
    )
    assert result.exit_code == 0, result.stderr
    assert result.cancelled is False
    assert result.timed_out is False
    assert not adapter._active


def test_docker_create_defers_bind_source_resolution_until_start(tmp_path: Path):
    """Document Docker's start-time bind source resolution independently.

    The adapter's workspace lease prevents a cooperative Phase 4 manager from
    doing this replacement.  An external root process that ignores the lease
    is outside the threat model; this opt-in helper only records the real
    Docker semantic that a source replacement after ``create`` is observed at
    ``start`` time.  The adapter must therefore verify the workspace after
    ``create`` and before ``start``; the unit TOCTOU tests cover that gate.
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
            pytest.fail(
                "Docker create failed in the opt-in mount identity test "
                f"(returncode={create.returncode}): "
                f"{_redact_docker_stderr(create.stderr, tmp_path=tmp_path)}"
            )
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
            pytest.fail(
                "Docker start failed in the opt-in mount identity test "
                f"(returncode={start.returncode}): "
                f"{_redact_docker_stderr(start.stderr, tmp_path=tmp_path)}"
            )
        wait = subprocess.run(
            [docker, "wait", container_name],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        if wait.returncode != 0 or wait.stdout.strip() != b"0":
            pytest.fail(
                "Docker wait failed in the opt-in mount identity test "
                f"(returncode={wait.returncode}): "
                f"{_redact_docker_stderr(wait.stderr, tmp_path=tmp_path)}"
            )
        logs = subprocess.run(
            [docker, "logs", container_name],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
        assert logs.returncode == 0, _redact_docker_stderr(
            logs.stderr, tmp_path=tmp_path
        )
        assert logs.stdout == b"replacement\n"
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
        network_mode=NetworkMode.NONE,
        timeout_seconds=1,
    )
    result = await _execute_with_docker_diagnostics(
        adapter,
        request,
        tmp_path=tmp_path,
        deadline=asyncio.get_running_loop().time() + 5,
    )
    assert result.timed_out is True
    assert not adapter._active


@pytest.mark.asyncio
async def test_real_docker_egress_capability_uses_default_bridge_and_reaches_dns(
    tmp_path: Path,
):
    """The opt-in integration gate proves full_access is usable by default."""

    config, key = _integration_config(tmp_path)
    adapter = DockerRuntimeAdapter(config)
    request = ExecutionRequest(
        request_id="integration-egress",
        workspace_key=key,
        command="getent hosts example.com",
        profile=ExecutionProfile.AGENT,
        network_mode=NetworkMode.EGRESS,
        timeout_seconds=10,
    )
    result = await _execute_with_docker_diagnostics(
        adapter,
        request,
        tmp_path=tmp_path,
        deadline=asyncio.get_running_loop().time() + 15,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()
    assert not adapter._active


@pytest.mark.asyncio
async def test_real_docker_linked_worktree_git_metadata_is_scoped_and_usable(
    tmp_path: Path,
):
    """A linked worktree must not depend on the Web container's /app path."""

    config, _ = _integration_config(tmp_path)
    root = tmp_path / "workplace"
    base = root / "owner" / "repo" / "base"
    workspace = root / "owner" / "repo" / "worktrees" / "42-linked"
    key = _workspace_key_for_relative_identity("owner/repo/worktrees/42-linked")
    base.mkdir(parents=True)
    workspace.mkdir(parents=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    async def run_git(*args: str):
        return await asyncio.to_thread(
            subprocess.run,
            list(args),
            cwd=base,
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )

    try:
        for args in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.name", "sandbox-test"),
            ("git", "config", "user.email", "sandbox@example.invalid"),
        ):
            result = await run_git(*args)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
        (base / "README.md").write_text("linked\n", encoding="utf-8")
        for args in (("git", "add", "README.md"), ("git", "commit", "-m", "init")):
            result = await run_git(*args)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
        result = await run_git(
            "git", "worktree", "add", "-B", "task-linked", str(workspace), "main"
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

        adapter = DockerRuntimeAdapter(config)
        request = ExecutionRequest(
            request_id="integration-linked-worktree",
            workspace_key=key,
            command=(
                "set -eu; "
                "test \"$(git rev-parse --show-toplevel)\" = /workspace; "
                "test \"$(stat -c '%u:%a' /workspace/.git)\" = 0:444; "
                "test \"$(stat -c '%u:%a' /sakura-git/worktree/gitdir)\" = 0:444; "
                "test \"$(stat -c '%u:%a' /sakura-git/worktree/commondir)\" = 0:444; "
                "test ! -w /workspace/.git; "
                "test ! -w /sakura-git/worktree/gitdir; "
                "test ! -w /sakura-git/worktree/commondir; "
                "test ! -w /sakura-git/common/HEAD; "
                "git status --short; "
                "test \"$(git show HEAD:README.md)\" = linked"
            ),
            profile=ExecutionProfile.AGENT,
            network_mode=NetworkMode.NONE,
            timeout_seconds=20,
        )
        result = await _execute_with_docker_diagnostics(
            adapter,
            request,
            tmp_path=tmp_path,
            deadline=asyncio.get_running_loop().time() + 25,
        )
        assert result.exit_code == 0, result.stderr
        assert not adapter._active
    finally:
        await asyncio.to_thread(
            subprocess.run,
            ["git", "worktree", "remove", "--force", str(workspace)],
            cwd=base,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=20,
        )
