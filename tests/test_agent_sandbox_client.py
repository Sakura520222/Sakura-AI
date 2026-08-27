"""Backend sandboxd client contract tests; no Docker or UDS is required."""

from __future__ import annotations

import asyncio
import math
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.services.agent_team import sandbox_client
from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    execution_workspace_key,
)
from backend.services.agent_team.network_policy import (
    AgentTeamNetworkPolicy,
    AgentTeamNetworkPolicyState,
)
from backend.services.agent_team.sandbox_client import (
    PROTOCOL_VERSION,
    SandboxCleanupError,
    SandboxExecutionConfig,
    SandboxExecutionRunner,
    SandboxPolicyError,
    SandboxProtocolError,
    SandboxUnavailableError,
    resolve_execution_backend,
    validate_execution_backend,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _runner(tmp_path: Path) -> tuple[SandboxExecutionRunner, Path, AgentTeamWorkspaceService]:
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    runner = SandboxExecutionRunner(
        workspace,
        service,
        socket_path=str(tmp_path / "sandboxd.sock"),
        timeout_seconds=10,
        max_output_bytes=100,
    )
    return runner, workspace, service


@pytest.mark.asyncio
async def test_runner_serializes_only_execution_contract(tmp_path: Path, monkeypatch):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)
    requests: list[tuple[str, str, dict | None]] = []

    async def fake_request(method: str, path: str, json_body=None, **kwargs):
        requests.append((method, path, json_body))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "request_id": json_body["request_id"],
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    result = await runner.execute(
        ExecutionRequest(
            workspace_key=key,
            argv=("python", "-c", "print('ok')"),
            profile=ExecutionProfile.AGENT,
            timeout_seconds=20,
        )
    )
    assert result.stdout == "ok"
    payload = requests[0][2]
    assert payload is not None
    assert set(payload) == {
        "request_id",
        "workspace_key",
        "argv",
        "cwd",
        "profile",
        "timeout_seconds",
        "env",
        "network_mode",
    }
    assert "image" not in payload
    assert "mount" not in payload
    assert "network" not in payload
    assert payload["network_mode"] == "none"
    assert "runtime" not in payload
    assert payload["timeout_seconds"] == 10


@pytest.mark.asyncio
async def test_runner_reads_network_policy_before_each_execution(
    tmp_path: Path,
    monkeypatch,
):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)
    policies = iter(
        [
            AgentTeamNetworkPolicy.WEB_TOOLS,
            AgentTeamNetworkPolicy.FULL_ACCESS,
            AgentTeamNetworkPolicy.OFFLINE,
        ]
    )
    payloads: list[dict] = []

    async def read_policy():
        return next(policies)

    async def fake_request(method: str, path: str, json_body=None, **kwargs):
        del method, path, kwargs
        assert json_body is not None
        payloads.append(json_body)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "request_id": json_body["request_id"],
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            },
        }

    async def read_state():
        policy = await read_policy()
        return AgentTeamNetworkPolicyState(
            policy=policy,
            revision=f"revision-{policy.value}",
        )

    monkeypatch.setattr(sandbox_client, "get_agent_team_network_policy_state", read_state)
    monkeypatch.setattr(runner, "_request", fake_request)
    request = ExecutionRequest(
        workspace_key=key,
        command="printf secret-command",
        profile=ExecutionProfile.AGENT,
        timeout_seconds=1,
    )

    await runner.execute(request)
    await runner.execute(request)
    await runner.execute(request)

    assert [payload["network_mode"] for payload in payloads] == [
        "none",
        "egress",
        "none",
    ]


@pytest.mark.asyncio
async def test_runner_maps_unavailable_and_does_not_fallback(tmp_path: Path, monkeypatch):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)

    async def unavailable(*args, **kwargs):
        raise SandboxUnavailableError("socket down")

    monkeypatch.setattr(runner, "_request", unavailable)
    with pytest.raises(SandboxUnavailableError):
        await runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="echo no-local-fallback",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
            )
        )


@pytest.mark.asyncio
async def test_runner_rejects_wrong_workspace_and_malformed_response(tmp_path: Path, monkeypatch):
    runner, _workspace, _service = _runner(tmp_path)
    with pytest.raises(SandboxPolicyError):
        await runner.execute(
            ExecutionRequest(
                workspace_key="other-task",
                command="echo blocked",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
            )
        )

    async def malformed(*args, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {"request_id": "not-enough-fields", "unexpected": True},
        }

    monkeypatch.setattr(runner, "_request", malformed)
    key = runner.workspace_key
    with pytest.raises(SandboxProtocolError):
        await runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="echo blocked",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
            )
        )


def test_backend_selection_is_fail_closed_for_deployment_mode():
    assert validate_execution_backend("sandbox", "image") == "sandbox"
    assert resolve_execution_backend("sandbox", deploy_mode="image") == "sandbox"
    assert resolve_execution_backend("local", deploy_mode="source") == "local"
    with pytest.raises(ValueError, match="explicitly configured"):
        resolve_execution_backend(None, deploy_mode="image")
    with pytest.raises(ValueError, match="explicitly configured"):
        resolve_execution_backend(None, deploy_mode="source")
    with pytest.raises(ValueError, match="requires deploy_mode='source'"):
        validate_execution_backend("local", "image")
    for deploy_mode in ("unknown", "", "SOURCE", " source ", "production"):
        with pytest.raises(ValueError, match="requires deploy_mode='source'"):
            validate_execution_backend("local", deploy_mode)
    with pytest.raises(ValueError):
        validate_execution_backend("docker", "source")


def test_sandbox_client_rejects_updater_socket_and_invalid_limits(tmp_path: Path):
    with pytest.raises(ValueError):
        SandboxExecutionConfig(socket_path=r"\run\sakura-ai\updater.sock")
    with pytest.raises(ValueError):
        SandboxExecutionConfig(max_output_bytes=0)
    with pytest.raises(ValueError):
        SandboxExecutionConfig(max_output_bytes=65 * 1024 * 1024)
    with pytest.raises(ValueError):
        SandboxExecutionConfig(timeout_seconds=math.inf)
    with pytest.raises(ValueError):
        SandboxExecutionConfig(request_timeout_seconds=math.nan)


def test_digest_syntax_is_deploy_mode_specific(tmp_path: Path):
    local_id = "sha256:" + "a" * 64
    image_digest = "registry.example/runner@sha256:" + "b" * 64
    source_config = SandboxExecutionConfig(
        socket_path=str(tmp_path / "source.sock"),
        deploy_mode="source",
        expected_runner_image_digest=local_id,
    )
    assert source_config.expected_runner_image_digest == local_id

    with pytest.raises(ValueError, match="invalid for the deploy mode"):
        SandboxExecutionConfig(
            socket_path=str(tmp_path / "image.sock"),
            deploy_mode="image",
            expected_runner_image_digest=local_id,
        )
    for mode_index, deploy_mode in enumerate(("source", "image")):
        for value_index, value in enumerate(
            ("registry.example/runner:latest", "runner:tag", "sha256:short")
        ):
            with pytest.raises(ValueError, match="invalid for the deploy mode"):
                SandboxExecutionConfig(
                    socket_path=str(tmp_path / f"invalid-{mode_index}-{value_index}.sock"),
                    deploy_mode=deploy_mode,
                    expected_runner_image_digest=value,
                )
    image_config = SandboxExecutionConfig(
        socket_path=str(tmp_path / "full-image.sock"),
        deploy_mode="image",
        expected_runner_image_digest=image_digest,
    )
    assert image_config.expected_runner_image_digest == image_digest


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.0, True, False])
def test_sandbox_client_rejects_non_integer_byte_limits(tmp_path: Path, value):
    socket_path = str(tmp_path / "sandboxd.sock")
    with pytest.raises(ValueError):
        SandboxExecutionConfig(socket_path=socket_path, max_output_bytes=value)
    with pytest.raises(ValueError):
        SandboxExecutionConfig(
            socket_path=socket_path,
            max_output_bytes=1,
            max_response_bytes=value,
        )


def test_sandbox_client_accepts_integer_byte_limit_boundaries(tmp_path: Path):
    config = SandboxExecutionConfig(
        socket_path=str(tmp_path / "sandboxd.sock"),
        max_output_bytes=64 * 1024 * 1024,
        max_response_bytes=128 * 1024 * 1024,
    )
    assert config.max_output_bytes == 64 * 1024 * 1024
    assert config.max_response_bytes == 128 * 1024 * 1024


def test_client_requires_http_timeout_to_cover_execution_and_cleanup():
    with pytest.raises(ValueError, match="cleanup margin"):
        SandboxExecutionConfig(
            socket_path="/tmp/sandbox.sock",
            timeout_seconds=2,
            cleanup_margin_seconds=1,
            request_timeout_seconds=2.5,
        )


def test_client_default_http_timeout_tracks_a_large_cleanup_margin():
    config = SandboxExecutionConfig(
        socket_path="/tmp/sandbox.sock",
        timeout_seconds=2,
        cleanup_margin_seconds=60,
    )
    assert config.http_timeout >= 62


def test_client_rejects_socket_and_parent_symlink_or_windows_reparse(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "sandbox-root"
    nested = root / "nested"
    socket_path = nested / "sandboxd.sock"
    root.mkdir()
    nested.mkdir()

    monkeypatch.setattr(
        sandbox_client,
        "_is_link_or_reparse",
        lambda path: path == nested,
    )
    with pytest.raises(ValueError, match="symlink or reparse"):
        SandboxExecutionConfig(socket_path=str(socket_path), socket_root=str(root))

    monkeypatch.setattr(
        sandbox_client,
        "_is_link_or_reparse",
        lambda path: path == socket_path,
    )
    with pytest.raises(ValueError, match="symlink or reparse"):
        SandboxExecutionConfig(socket_path=str(socket_path), socket_root=str(root))


def test_client_rejects_real_path_escape_from_independent_socket_root(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "sandbox-root"
    socket_path = root / "sandboxd.sock"
    outside = tmp_path / "outside" / "sandboxd.sock"
    root.mkdir()
    realpath = os.path.realpath

    def fake_realpath(path):
        if Path(path) == socket_path:
            return str(outside)
        return realpath(path)

    monkeypatch.setattr(sandbox_client.os.path, "realpath", fake_realpath)
    with pytest.raises(ValueError, match="real path"):
        SandboxExecutionConfig(socket_path=str(socket_path), socket_root=str(root))


def test_client_link_detector_covers_posix_symlink_and_windows_reparse(
    monkeypatch,
    tmp_path: Path,
):
    metadata = SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
    monkeypatch.setattr(sandbox_client.os, "lstat", lambda path: metadata)
    assert sandbox_client._is_link_or_reparse(tmp_path / "posix-link") is True

    metadata.st_mode = 0
    metadata.st_file_attributes = sandbox_client._REPARSE_POINT
    assert sandbox_client._is_link_or_reparse(tmp_path / "windows-junction") is True


@pytest.mark.asyncio
async def test_client_transport_failure_best_effort_cancels_request(
    tmp_path: Path,
    monkeypatch,
):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)
    cancelled: list[str] = []

    async def fake_request(*args, **kwargs):
        raise httpx.ReadTimeout("read timeout")

    async def fake_cancel(request_id: str):
        cancelled.append(request_id)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(runner, "_best_effort_cancel", fake_cancel)
    with pytest.raises(SandboxUnavailableError):
        await runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="echo timeout",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
            )
        )
    assert len(cancelled) == 1
    assert len(cancelled[0]) == 32


@pytest.mark.asyncio
async def test_client_async_cancel_best_effort_cancels_request(tmp_path: Path, monkeypatch):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)
    cancelled: list[str] = []

    async def fake_request(*args, **kwargs):
        raise asyncio.CancelledError()

    async def fake_cancel(request_id: str):
        cancelled.append(request_id)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(runner, "_best_effort_cancel", fake_cancel)
    with pytest.raises(asyncio.CancelledError):
        await runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="echo cancelled",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
            )
        )
    assert len(cancelled) == 1


@pytest.mark.asyncio
async def test_client_truncates_oversized_utf8_response_data(tmp_path: Path, monkeypatch):
    runner, workspace, service = _runner(tmp_path)
    key = execution_workspace_key(workspace, service)

    async def fake_request(method: str, path: str, json_body=None, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "request_id": json_body["request_id"],
                "exit_code": 0,
                "stdout": "é" * 100,
                "stderr": "尾部",
                "timed_out": False,
                "cancelled": False,
                "output_truncated": False,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    result = await runner.execute(
        ExecutionRequest(
            workspace_key=key,
            command="echo utf8",
            profile=ExecutionProfile.AGENT,
            timeout_seconds=1,
        )
    )
    assert result.output_truncated is True
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 100


@pytest.mark.asyncio
async def test_client_rejects_oversized_http_envelope(tmp_path: Path, monkeypatch):
    runner, _workspace, _service = _runner(tmp_path)
    runner.config = SandboxExecutionConfig(
        socket_path=str(tmp_path / "sandboxd.sock"),
        timeout_seconds=1,
        max_output_bytes=10,
        max_response_bytes=100,
    )

    async def fake_request(*args, **kwargs):
        return httpx.Response(
            200,
            json={
            "protocol_version": PROTOCOL_VERSION,
                "sandboxd_version": "test",
                "data": {"stdout": "x" * 1000},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(SandboxProtocolError, match="byte limit"):
        await runner.health()


@pytest.mark.asyncio
async def test_client_rejects_unknown_error_fields_and_codes(tmp_path: Path, monkeypatch):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_request(*args, **kwargs):
        return httpx.Response(
            500,
            json={
            "protocol_version": PROTOCOL_VERSION,
                "sandboxd_version": "test",
                "error": "INTERNAL_ERROR",
                "data": {},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(SandboxProtocolError, match="unknown fields"):
        await runner.health()

    async def fake_unknown_error(*args, **kwargs):
        return httpx.Response(
            500,
            json={
            "protocol_version": PROTOCOL_VERSION,
                "sandboxd_version": "test",
                "error": "FUTURE_ERROR",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_unknown_error)
    with pytest.raises(SandboxProtocolError, match="malformed"):
        await runner.health()


@pytest.mark.asyncio
async def test_client_rejects_legacy_v1_response_without_implicit_compatibility(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def legacy_response(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "sandboxd_version": "legacy",
                "data": {},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", legacy_response)
    with pytest.raises(SandboxProtocolError, match="protocol version"):
        await runner.health()


@pytest.mark.asyncio
async def test_ensure_ready_validates_full_daemon_identity_contract(tmp_path: Path, monkeypatch):
    runner, _workspace, _service = _runner(tmp_path)
    health = SimpleNamespace(
        ready=True,
        runtime="runc",
        profiles=["agent", "dependency"],
        egress_capability="none",
        instance_id="sandbox-instance-1",
        workspace_root="/app/workplace/",
        runner_image_digest=(
            "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "0" * 64
        ),
    )

    async def fake_health():
        return health

    monkeypatch.setattr(runner, "health", fake_health)
    result = await runner.ensure_ready(
        expected_runtime="runc",
        expected_instance_id="sandbox-instance-1",
        expected_workspace_root="/app/workplace",
        expected_digest=health.runner_image_digest,
        require_digest=True,
    )
    assert result is health
    assert runner.egress_capability == "none"


@pytest.mark.asyncio
async def test_ensure_ready_exposes_only_server_advertised_egress_capability(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_health():
        return SimpleNamespace(
            ready=True,
            runtime="runc",
            profiles=["agent", "dependency"],
            egress_capability="egress",
            instance_id="sandbox-instance-1",
            workspace_root="/app/workplace",
            runner_image_digest=None,
        )

    monkeypatch.setattr(runner, "health", fake_health)
    await runner.ensure_ready()
    assert runner.egress_capability == "egress"


@pytest.mark.asyncio
@pytest.mark.parametrize("egress_capability", ["host", "bridge", "container:x", "ns:/tmp/x", "bad network"])
async def test_ensure_ready_rejects_uncontrolled_egress_capability(
    tmp_path: Path,
    monkeypatch,
    egress_capability: str,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_health():
        return SimpleNamespace(
            ready=True,
            runtime="runc",
            profiles=["agent", "dependency"],
            egress_capability=egress_capability,
            instance_id="sandbox-instance-1",
            workspace_root="/app/workplace",
            runner_image_digest=None,
        )

    monkeypatch.setattr(runner, "health", fake_health)
    with pytest.raises(SandboxProtocolError, match="egress capability"):
        await runner.ensure_ready()


@pytest.mark.asyncio
async def test_ensure_ready_image_requires_all_expected_identities(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_health():
        return SimpleNamespace(
            ready=True,
            runtime="runc",
            profiles=["agent", "dependency"],
            egress_capability="none",
            instance_id="sandbox-instance-1",
            workspace_root="/app/workplace",
            runner_image_digest="registry.example/runner@sha256:" + "1" * 64,
        )

    monkeypatch.setattr(runner, "health", fake_health)
    with pytest.raises(SandboxProtocolError, match="expected identities"):
        await runner.ensure_ready(require_digest=True)


@pytest.mark.asyncio
async def test_health_envelope_ready_true_admits_matching_identity_and_digest(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)
    digest = "registry.example/runner@sha256:" + "a" * 64

    async def fake_request(*args, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "ready": True,
                "runtime": "runc",
                "profiles": ["agent", "dependency"],
                "egress_capability": "none",
                "instance_id": "sandbox-instance-1",
                "workspace_root": "/app/workplace",
                "runner_image_digest": digest,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    result = await runner.ensure_ready(
        expected_runtime="runc",
        expected_instance_id="sandbox-instance-1",
        expected_workspace_root="/app/workplace",
        expected_digest=digest,
        require_digest=True,
    )
    assert result.ready is True
    assert result.instance_id == "sandbox-instance-1"
    assert result.runner_image_digest == digest


@pytest.mark.asyncio
async def test_source_health_compares_bare_content_id_and_injected_workspace_identity(
    tmp_path: Path,
    monkeypatch,
):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    runner = SandboxExecutionRunner(
        workspace,
        service,
        socket_path=str(tmp_path / "sandboxd.sock"),
        deploy_mode="source",
    )
    digest = "sha256:" + "c" * 64

    async def fake_request(*args, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "ready": True,
                "runtime": "runc",
                "profiles": ["agent", "dependency"],
                "egress_capability": "none",
                "instance_id": "sandbox-instance-source",
                "workspace_root": "C:\\deploy\\sakura\\workspaces\\",
                "runner_image_digest": digest,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    result = await runner.ensure_ready(
        expected_runtime="runc",
        expected_instance_id="sandbox-instance-source",
        expected_workspace_root="C:/deploy/sakura/workspaces",
        expected_digest=digest,
        require_digest=True,
    )
    assert result.runner_image_digest == digest


@pytest.mark.asyncio
async def test_health_envelope_ready_false_fails_admission(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_request(*args, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "ready": False,
                "runtime": "runc",
                "profiles": ["agent", "dependency"],
                "egress_capability": "none",
                "instance_id": "sandbox-instance-1",
                "workspace_root": "/app/workplace",
                "runner_image_digest": "registry.example/runner@sha256:" + "b" * 64,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    with pytest.raises(SandboxUnavailableError, match="not ready"):
        await runner.ensure_ready()


@pytest.mark.asyncio
async def test_health_envelope_rejects_legacy_ok_and_unknown_fields(
    tmp_path: Path,
    monkeypatch,
):
    runner, _workspace, _service = _runner(tmp_path)

    async def fake_request(*args, **kwargs):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": "test",
            "data": {
                "ok": True,
                "runtime": "runc",
                "profiles": ["agent", "dependency"],
                "egress_capability": "none",
                "instance_id": "sandbox-instance-1",
                "workspace_root": "/app/workplace",
                "runner_image_digest": None,
            },
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    with pytest.raises(SandboxProtocolError, match="contract"):
        await runner.health()


@pytest.mark.asyncio
async def test_outer_cancel_does_not_interrupt_independent_cancel_delivery(
    tmp_path: Path,
    monkeypatch,
):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    runner = SandboxExecutionRunner(
        workspace,
        service,
        socket_path=str(tmp_path / "sandboxd.sock"),
        timeout_seconds=1,
        cleanup_margin_seconds=0.2,
    )
    key = execution_workspace_key(workspace, service)
    cancel_event = asyncio.Event()
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    cancel_finished = asyncio.Event()

    async def fake_request(method: str, path: str, json_body=None, **kwargs):
        if path.endswith("/cancel"):
            cancel_started.set()
            await cancel_release.wait()
            cancel_finished.set()
            return {
                "protocol_version": PROTOCOL_VERSION,
                "sandboxd_version": "test",
                "data": {
                    "request_id": path.split("/")[-2],
                    "cancelled": True,
                    "state": "cancelled",
                },
            }
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_request", fake_request)
    task = asyncio.create_task(
        runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="sleep 60",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
                cancel_event=cancel_event,
            )
        )
    )
    cancel_event.set()
    await asyncio.wait_for(cancel_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cancel_release.set()
    await asyncio.wait_for(cancel_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancel_delivery_failure_is_not_reported_as_cancelled(
    tmp_path: Path,
    monkeypatch,
):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    runner = SandboxExecutionRunner(
        workspace,
        service,
        socket_path=str(tmp_path / "sandboxd.sock"),
        timeout_seconds=1,
        cleanup_margin_seconds=0.05,
    )
    key = execution_workspace_key(workspace, service)
    cancel_event = asyncio.Event()

    async def fake_request(method: str, path: str, json_body=None, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_request", fake_request)
    task = asyncio.create_task(
        runner.execute(
            ExecutionRequest(
                workspace_key=key,
                command="sleep 60",
                profile=ExecutionProfile.AGENT,
                timeout_seconds=1,
                cancel_event=cancel_event,
            )
        )
    )
    cancel_event.set()
    with pytest.raises(SandboxCleanupError, match="cancellation delivery"):
        await task
