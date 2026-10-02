"""Opt-in Linux Docker quality gates for the real sandbox runtime.

The default Windows/source test job skips these tests.  A release runner must
set ``SAKURA_SANDBOX_DOCKER_INTEGRATION=1`` and provide an immutable
``SAKURA_AGENT_RUNNER_IMAGE_DIGEST`` to obtain runtime evidence.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sakura_ai_sandboxer.app import create_app
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
_DOCKER_PROBE_ATTEMPTS = 5


async def _docker_output(config: SandboxdConfig, tmp_path: Path, *args: str) -> bytes:
    """Observe the real daemon; a failed probe must never prove removal."""

    process = await asyncio.create_subprocess_exec(
        config.docker_binary,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, _redact_docker_stderr(stderr, tmp_path=tmp_path)
    return stdout


async def _assert_request_container_removed(
    config: SandboxdConfig,
    tmp_path: Path,
    request_id: str,
    *,
    container_id: str | None = None,
) -> None:
    filters = (
        ("--filter", f"id={container_id}")
        if container_id is not None
        else (
            "--filter",
            f"label=ai.sakura.instance-id={config.instance_id}",
            "--filter",
            f"label=ai.sakura.request-id={request_id}",
        )
    )
    remaining = await _docker_output(
        config,
        tmp_path,
        "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        *filters,
    )
    assert not remaining.strip(), "one-shot request container remains in Docker"


@asynccontextmanager
async def _observe_created_containers(adapter: DockerRuntimeAdapter, tmp_path: Path):
    """Inspect actual created containers without faking any Docker result."""

    original_run_command = adapter._run_command
    observed = []
    created_ids: list[str] = []
    primary_error: BaseException | None = None

    async def observe(argv: tuple[str, ...], deadline: float):
        result = await original_run_command(argv, deadline)
        if len(argv) > 1 and argv[1] == "create" and result.returncode == 0:
            container_id = result.stdout.decode().strip()
            assert re.fullmatch(r"[0-9a-f]{64}", container_id)
            # Record the exact daemon-returned ID before inspect can fail.
            created_ids.append(container_id)
            inspected = await _docker_output(
                adapter.config, tmp_path, "inspect", container_id
            )
            observed.append(json.loads(inspected)[0])
        return result

    adapter._run_command = observe
    try:
        yield observed
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        adapter._run_command = original_run_command
        try:
            # The runtime-removal assertion runs before this safety net.  A
            # runtime regression must fail the test, but cannot leave a child
            # behind after the disposable test controller exits.
            async with asyncio.timeout(adapter.config.cleanup_margin_seconds):
                for container_id in dict.fromkeys(created_ids):
                    remaining = await _docker_output(
                        adapter.config,
                        tmp_path,
                        "ps",
                        "--all",
                        "--quiet",
                        "--no-trunc",
                        "--filter",
                        f"id={container_id}",
                    )
                    if remaining.strip():
                        await _docker_output(
                            adapter.config,
                            tmp_path,
                            "rm",
                            "--force",
                            "--volumes",
                            container_id,
                        )
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "test-owned Docker teardown failed "
                f"({type(cleanup_error).__name__}); original failure preserved"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["inspect", "assertion"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_container_observer_teardown_preserves_primary_failure(
    monkeypatch, tmp_path: Path, failure_stage: str, cleanup_fails: bool
):
    """Test only the observer safety net, including failure before yield returns."""

    container_id = "a" * 64
    primary_error = AssertionError("original isolation failure")
    cleanup_calls = []

    async def create(*_args):
        return SimpleNamespace(returncode=0, stdout=container_id.encode())

    async def docker_output(_config, _tmp_path, *args):
        if args[0] == "inspect":
            if failure_stage == "inspect":
                raise primary_error
            return json.dumps([{"Id": container_id}]).encode()
        cleanup_calls.append(args)
        if args[0] == "ps":
            return container_id.encode()
        if cleanup_fails:
            raise RuntimeError("teardown failure")
        return b""

    monkeypatch.setattr(f"{__name__}._docker_output", docker_output)
    adapter = SimpleNamespace(config=SandboxdConfig(), _run_command=create)
    with pytest.raises(AssertionError) as caught:
        async with _observe_created_containers(adapter, tmp_path):
            await adapter._run_command(("docker", "create"), 0)
            raise primary_error

    assert caught.value is primary_error
    assert adapter._run_command is create
    assert cleanup_calls == [
        ("ps", "--all", "--quiet", "--no-trunc", "--filter", f"id={container_id}"),
        ("rm", "--force", "--volumes", container_id),
    ]
    if cleanup_fails:
        assert primary_error.__notes__ == [
            "test-owned Docker teardown failed (RuntimeError); original failure preserved"
        ]
    else:
        assert not getattr(primary_error, "__notes__", [])


@pytest.mark.asyncio
async def test_container_observer_teardown_failure_does_not_pass_successful_body(
    monkeypatch, tmp_path: Path
):
    container_id = "b" * 64

    async def create(*_args):
        return SimpleNamespace(returncode=0, stdout=container_id.encode())

    async def docker_output(_config, _tmp_path, *args):
        if args[0] == "inspect":
            return json.dumps([{"Id": container_id}]).encode()
        await asyncio.Event().wait()

    monkeypatch.setattr(f"{__name__}._docker_output", docker_output)
    adapter = SimpleNamespace(
        config=SandboxdConfig(cleanup_margin_seconds=0.05), _run_command=create
    )
    outer_deadline = asyncio.timeout(1)
    with pytest.raises(TimeoutError):
        async with outer_deadline, _observe_created_containers(adapter, tmp_path):
            await adapter._run_command(("docker", "create"), 0)
    assert not outer_deadline.expired()
    assert adapter._run_command is create


@pytest.mark.asyncio
async def test_real_docker_observer_teardown_removes_untracked_child_on_assertion_failure(
    tmp_path: Path,
):
    """Even a child absent from _active is removed without hiding a failed gate."""

    config, _key = _integration_config(tmp_path)
    adapter = DockerRuntimeAdapter(config)
    container_name = "sakura-observer-teardown-" + hashlib.sha256(
        str(tmp_path).encode()
    ).hexdigest()[:12]
    with pytest.raises(AssertionError, match="one-shot request container remains"):
        async with _observe_created_containers(adapter, tmp_path) as observed:
            result = await adapter._run_command(
                (
                    config.docker_binary,
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
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    config.runner_image_digest or "",
                    "true",
                ),
                asyncio.get_running_loop().time() + 15,
            )
            assert result.returncode == 0
            assert len(observed) == 1
            container_id = observed[0]["Id"]
            assert not adapter._active
            # This must fail before the observer's safety net runs.
            await _assert_request_container_removed(
                config, tmp_path, "observer-teardown", container_id=container_id
            )
    await _assert_request_container_removed(
        config, tmp_path, "observer-teardown", container_id=container_id
    )


def _assert_egress_container_is_hardened(
    container: dict, config: SandboxdConfig, workspace: Path
) -> None:
    """Egress must not relax the server-owned OCI isolation/resource policy."""

    host = container["HostConfig"]
    assert host["NetworkMode"] == "bridge"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["PidMode"] != "host"
    assert host["IpcMode"] != "host"
    assert host["PidsLimit"] == config.pids_limit
    assert host["Memory"] == config.memory_bytes
    assert host["MemorySwap"] == config.memory_bytes
    assert host["NanoCpus"] == int(config.cpus * 1_000_000_000)
    assert container["Config"]["User"] == "65532:65532"
    assert container["Config"]["Image"] == config.runner_image_digest
    assert host["Ulimits"] == [
        {"Name": "nofile", "Soft": config.nofile_soft, "Hard": config.nofile_hard}
    ]
    binds = [mount for mount in container["Mounts"] if mount["Type"] == "bind"]
    assert len(binds) == 1
    assert binds[0]["Source"] == str(workspace)
    assert binds[0]["Destination"] == "/workspace"
    assert binds[0]["RW"] is True
    assert binds[0]["Propagation"] == "rprivate"
    assert "noexec" in host["Tmpfs"]["/tmp"].split(",")
    assert f"size={config.tmpfs_bytes}" in host["Tmpfs"]["/tmp"].split(",")


@pytest.mark.asyncio
async def test_real_docker_wire_egress_is_one_shot_across_execution_profiles(
    tmp_path: Path,
):
    """Existing v2 modes isolate successive requests; Backend mapping is separate.

    Catch accidentally sticky networking, skipped rm, or relaxed hardening on
    either Dependency or Agent egress without relying on Backend helpers.
    """

    config, key = _integration_config(tmp_path)
    workspace = tmp_path / "workplace/owner/repo/worktrees/42-integration"
    sibling = workspace.with_name("43-other-task")
    sibling.mkdir()
    (sibling / "private-marker").write_text("other-task-secret", encoding="utf-8")
    adapter = DockerRuntimeAdapter(config)
    app = create_app(config, runtime=adapter)
    hardening_probe = (
        "set -eu; "
        'test "$(id -u)" = 65532; '
        'test "$(pwd)" = /workspace; '
        "test ! -w /etc/passwd; "
        "test ! -e /var/run/docker.sock; "
        "test ! -e /run/sakura-ai-sandbox/sandboxd.sock; "
        "test ! -e /workspace/../43-other-task/private-marker; "
        "python -c 'from pathlib import Path; "
        'status = dict(line.split(":", 1) for line in '
        'Path("/proc/self/status").read_text().splitlines() if ":" in line); '
        'assert int(status["CapEff"].strip(), 16) == 0; '
        'assert status["NoNewPrivs"].strip() == "1"\'; '
    )
    cases = (
        (
            "integration-agent-before",
            "agent",
            "none",
            "if getent hosts example.com; then exit 1; fi",
        ),
        (
            "integration-dependency-egress",
            "dependency",
            "egress",
            "getent hosts example.com",
        ),
        ("integration-agent-egress", "agent", "egress", "getent hosts example.com"),
        (
            "integration-agent-after",
            "agent",
            "none",
            "if getent hosts example.com; then exit 1; fi",
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        _observe_created_containers(adapter, tmp_path) as observed,
        httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as client,
    ):
        for index, (request_id, profile, mode, network_probe) in enumerate(cases):
            response = await client.post(
                "/v1/executions",
                json={
                    "request_id": request_id,
                    "workspace_key": key,
                    "command": hardening_probe + network_probe + "; echo isolation-ok",
                    "profile": profile,
                    "network_mode": mode,
                    "timeout_seconds": 20,
                },
            )
            assert response.status_code == 200, response.json()
            result = response.json()["data"]
            assert result["exit_code"] == 0, result["stderr"]
            assert "isolation-ok" in result["stdout"]
            assert result["cancelled"] is False
            assert result["timed_out"] is False
            assert len(observed) == index + 1
            container = observed[-1]
            assert container["HostConfig"]["NetworkMode"] == (
                "bridge" if mode == "egress" else "none"
            )
            if mode == "egress":
                assert result["stdout"].strip() != "isolation-ok"
                _assert_egress_container_is_hardened(container, config, workspace)
            await _assert_request_container_removed(
                config, tmp_path, request_id, container_id=container["Id"]
            )
            assert not adapter._active
    assert len({container["Id"] for container in observed}) == 4
    assert (sibling / "private-marker").read_text(
        encoding="utf-8"
    ) == "other-task-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["timeout", "cancel", "requester_cancel"])
async def test_real_docker_egress_terminal_paths_remove_started_container(
    tmp_path: Path, terminal: str
):
    """A running egress container must disappear before a terminal response."""

    config, key = _integration_config(tmp_path)
    workspace = tmp_path / "workplace/owner/repo/worktrees/42-integration"
    ready = workspace / "started"
    request_id = f"integration-egress-{terminal}"
    adapter = DockerRuntimeAdapter(config)
    app = create_app(config, runtime=adapter)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        _observe_created_containers(adapter, tmp_path) as observed,
        httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as client,
    ):
        execution = asyncio.create_task(
            client.post(
                "/v1/executions",
                json={
                    "request_id": request_id,
                    "workspace_key": key,
                    "command": "printf started > /workspace/started; sleep 60",
                    "profile": "agent",
                    "network_mode": "egress",
                    "timeout_seconds": 8 if terminal == "timeout" else 20,
                },
            )
        )
        try:
            async with asyncio.timeout(12):
                while not ready.exists():
                    assert not execution.done(), (
                        "execution ended before command startup"
                    )
                    await asyncio.sleep(0.05)
            assert len(observed) == 1
            container = observed[0]
            _assert_egress_container_is_hardened(container, config, workspace)
            current = json.loads(
                await _docker_output(config, tmp_path, "inspect", container["Id"])
            )[0]
            assert current["State"]["Running"] is True
            if terminal == "cancel":
                cancellation = await client.post(f"/v1/executions/{request_id}/cancel")
                assert cancellation.status_code == 200
                assert cancellation.json()["data"]["cancelled"] is True
            elif terminal == "requester_cancel":
                execution.cancel()
            if terminal == "requester_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(execution, timeout=15)
            else:
                response = await asyncio.wait_for(execution, timeout=15)
                assert response.status_code == 200, response.json()
                result = response.json()["data"]
                assert result["timed_out"] is (terminal == "timeout")
                assert result["cancelled"] is (terminal == "cancel")
                assert result["exit_code"] is None
            await _assert_request_container_removed(
                config, tmp_path, request_id, container_id=container["Id"]
            )
            assert not adapter._active
        finally:
            if not execution.done():
                execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_docker_rejects_request_runtime_overrides_before_container_creation(
    tmp_path: Path,
):
    """Strict HTTP rejection must leave no real container or workspace side effect."""

    config, key = _integration_config(tmp_path)
    workspace = tmp_path / "workplace/owner/repo/worktrees/42-integration"
    adapter = DockerRuntimeAdapter(config)
    app = create_app(config, runtime=adapter)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        _observe_created_containers(adapter, tmp_path) as observed,
        httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as client,
    ):
        for index, override in enumerate(
            (
                {"runtime_options": {"privileged": True, "network": "host"}},
                {"network_mode": "host"},
                {"mounts": [{"source": "/", "target": "/host"}]},
            )
        ):
            request_id = f"integration-runtime-override-{index}"
            response = await client.post(
                "/v1/executions",
                json={
                    "request_id": request_id,
                    "workspace_key": key,
                    "command": "touch /workspace/should-not-execute",
                    "profile": "agent",
                    "network_mode": "egress",
                    "timeout_seconds": 10,
                    **override,
                },
            )
            assert response.status_code == 422
            assert response.json()["error"] == "INVALID_REQUEST"
            await _assert_request_container_removed(config, tmp_path, request_id)
        assert observed == []
        assert not (workspace / "should-not-execute").exists()
        assert not adapter._active


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
        pytest.fail("Docker CLI is unavailable for the required integration gate")
    # The daemon can briefly reject probes just after the runner image builds.
    # Do not turn one transient probe into a silent skip in the mandatory gate.
    for attempt in range(_DOCKER_PROBE_ATTEMPTS):
        try:
            probe = subprocess.run(
                [docker, "info"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if probe.returncode == 0:
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt + 1 < _DOCKER_PROBE_ATTEMPTS:
            time.sleep(1)
    else:
        pytest.fail("Docker daemon is unavailable after repeated probes")
    digest = os.environ.get("SAKURA_AGENT_RUNNER_IMAGE_DIGEST")
    if not digest:
        pytest.fail("immutable runner digest is not configured for the required gate")
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


def test_docker_preflight_retries_transient_daemon_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAKURA_SANDBOX_DOCKER_INTEGRATION", "1")
    monkeypatch.setenv("SAKURA_AGENT_RUNNER_IMAGE_DIGEST", f"sha256:{'a' * 64}")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    calls: list[list[str]] = []
    delays: list[int] = []

    def probe(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0)

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(time, "sleep", delays.append)

    config, key = _integration_config(tmp_path)

    assert config.runner_image_digest == f"sha256:{'a' * 64}"
    assert key
    assert calls == [["/usr/bin/docker", "info"]] * 2
    assert delays == [1]


def test_docker_preflight_fails_instead_of_skipping_unavailable_daemon(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("SAKURA_SANDBOX_DOCKER_INTEGRATION", "1")
    monkeypatch.setenv("SAKURA_AGENT_RUNNER_IMAGE_DIGEST", f"sha256:{'a' * 64}")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    attempts = []

    def probe(argv, **_kwargs):
        attempts.append(argv)
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(pytest.fail.Exception, match="after repeated probes"):
        _integration_config(tmp_path)
    assert len(attempts) == _DOCKER_PROBE_ATTEMPTS


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


@pytest.mark.asyncio
async def test_real_docker_go_toolchain_runs_from_home_tmpfs_within_default_limits(
    tmp_path: Path,
):
    """Go's scratch binaries must execute from HOME without polluting /workspace.

    ``go run``/``go test`` compile into ``GOTMPDIR`` and exec the result, so
    this gate fails unless the daemon mounts the agent HOME as an
    exec-permitted tmpfs and the runner image redirects ``GOTMPDIR`` there.
    It runs cold under the default tmpfs/memory limits and additionally
    asserts that ``/tmp`` stays non-executable and that no ``go-build*``
    scratch directories leak into the task workspace.
    """

    config, key = _integration_config(tmp_path)
    # Cold stdlib compilation takes ~13s on two CPUs; leave generous slack
    # for slower CI runners (the shared helper pins 30s).
    config = dataclasses.replace(
        config, timeout_seconds=90.0, max_timeout_seconds=90.0
    )
    adapter = DockerRuntimeAdapter(config)
    request = ExecutionRequest(
        request_id="integration-go-toolchain",
        workspace_key=key,
        command=(
            "set -eu; "
            "printf 'module integration\\n\\ngo 1.27\\n' > go.mod; "
            "printf 'package main\\n"
            "import \"fmt\"\\n"
            "func main() { fmt.Println(\"go-run-ok\") }\\n' > main.go; "
            "test \"$(go run .)\" = \"go-run-ok\"; "
            "printf 'package main\\n"
            "import \"testing\"\\n"
            "func TestAdd(t *testing.T) { if 1+1 != 2 { t.Fatal(\"bad\") } }\\n"
            "' > main_test.go; "
            "go test -count=1 .; "
            "test \"$(stat -c '%u:%g:%a' \"$HOME\")\" = \"65532:65532:700\"; "
            "printf 'int main(void) { return 0; }\\n' > nx.c; "
            "cc -o /tmp/nx nx.c; "
            "if /tmp/nx 2>/dev/null; then "
            "echo 'unexpected: /tmp is executable'; exit 1; "
            "fi; "
            "if ls /workspace | grep -q '^go-build'; then "
            "echo 'unexpected: go scratch dirs leaked into workspace'; exit 1; "
            "fi; "
            "echo go-toolchain-ok"
        ),
        profile=ExecutionProfile.AGENT,
        network_mode=NetworkMode.NONE,
        timeout_seconds=85,
    )
    result = await _execute_with_docker_diagnostics(
        adapter,
        request,
        tmp_path=tmp_path,
        deadline=asyncio.get_running_loop().time() + 90,
    )
    assert result.exit_code == 0, result.stderr
    assert "go-toolchain-ok" in result.stdout, result.stderr
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
                "test \"$(git rev-parse --git-dir)\" = /sakura-git/common/worktrees/42-linked; "
                "test \"$(git rev-parse --git-common-dir)\" = /sakura-git/common; "
                "test \"$(stat -c '%u:%a' /workspace/.git)\" = 0:444; "
                "test \"$(stat -c '%u:%a' /sakura-git/common/worktrees/42-linked/gitdir)\" = 0:444; "
                "test \"$(stat -c '%u:%a' /sakura-git/common/worktrees/42-linked/commondir)\" = 0:444; "
                "test ! -w /workspace/.git; "
                "test ! -w /sakura-git/common/worktrees/42-linked/gitdir; "
                "test ! -w /sakura-git/common/worktrees/42-linked/commondir; "
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
