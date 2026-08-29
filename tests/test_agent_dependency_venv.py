"""Regression contracts for Agent dependency venv isolation."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
    execution_workspace_key,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


async def _async_value(value):
    return value


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv launchers are symlinks")
def test_local_dependency_validation_preserves_posix_venv_launcher_symlink(
    tmp_path: Path,
):
    """A normal venv/bin/python symlink to the host interpreter is accepted."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    runner = LocalExecutionRunner(workspace, workspace_service)
    launcher = workspace / ".venv" / "local" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(Path(sys.executable).resolve())
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    request = ExecutionRequest(
        workspace_key=execution_workspace_key(workspace, workspace_service),
        argv=(
            str(launcher),
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            "--quiet",
        ),
        profile=ExecutionProfile.DEPENDENCY,
    )

    # This is deliberately the runner's validation seam: resolving the
    # launcher entry itself aliases it to sys.executable and rejects it as a
    # bootstrap request before pip can run.
    runner._validate_dependency_request(request)


@pytest.mark.asyncio
async def test_dependency_venvs_are_backend_specific_and_cancel_is_forwarded(
    monkeypatch,
    tmp_path: Path,
):
    """Local and sandbox installs use separate venvs and preserve cancellation."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text(
        "example-package\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        lambda _key: _async_value(True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: type("Settings", (), {"agent_team_auto_install_deps": True})(),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        lambda: _async_value(AgentTeamNetworkPolicy.FULL_ACCESS),
    )

    local_requests: list[ExecutionRequest] = []
    local_runner = LocalExecutionRunner(workspace, workspace_service)

    async def local_execute(request: ExecutionRequest):
        local_requests.append(request)
        if len(local_requests) == 1:
            (workspace / ".venv" / "local").mkdir(parents=True)
        return ExecutionResult(exit_code=0)

    monkeypatch.setattr(local_runner, "execute", local_execute)
    local_cancel = asyncio.Event()
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    await service.install_workspace_dependencies(
        workspace,
        local_runner,
        cancel_event=local_cancel,
    )

    assert local_requests[0].argv[-1] == ".venv/local"
    assert (
        Path(local_requests[1].argv[0]).relative_to(workspace).parts[:2]
        == (".venv", "local")
    )
    assert all(request.cancel_event is local_cancel for request in local_requests)

    sandbox_requests: list[ExecutionRequest] = []

    class FakeSandboxRunner:
        egress_capability = "egress"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request: ExecutionRequest):
            sandbox_requests.append(request)
            return ExecutionResult(exit_code=0)

    sandbox_cancel = asyncio.Event()
    await service.install_workspace_dependencies(
        workspace,
        FakeSandboxRunner(),
        cancel_event=sandbox_cancel,
    )

    assert sandbox_requests[0].command == "python -m venv /workspace/.venv/sandbox"
    assert sandbox_requests[1].command.startswith(
        "/workspace/.venv/sandbox/bin/pip "
    )
    assert all(request.cancel_event is sandbox_cancel for request in sandbox_requests)


@pytest.mark.asyncio
async def test_cancelled_dependency_result_stops_before_pip(
    monkeypatch,
    tmp_path: Path,
):
    """A cancelled bootstrap is not reported as a generic installation error."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text(
        "example-package\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        lambda _key: _async_value(True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: type("Settings", (), {"agent_team_auto_install_deps": True})(),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        lambda: _async_value(AgentTeamNetworkPolicy.FULL_ACCESS),
    )

    class CancelledSandboxRunner:
        egress_capability = "egress"
        requests: list[ExecutionRequest] = []

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request: ExecutionRequest):
            self.requests.append(request)
            return ExecutionResult(exit_code=-9, cancelled=True)

    cancel_event = asyncio.Event()
    cancel_event.set()
    runner = CancelledSandboxRunner()
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)

    await service.install_workspace_dependencies(
        workspace,
        runner,
        cancel_event=cancel_event,
    )

    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_unannounced_cancelled_dependency_result_fails_closed(
    monkeypatch,
    tmp_path: Path,
):
    """A cancelled result without a task cancellation signal is not success."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text(
        "example-package\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        lambda _key: _async_value(True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: type("Settings", (), {"agent_team_auto_install_deps": True})(),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        lambda: _async_value(AgentTeamNetworkPolicy.FULL_ACCESS),
    )

    class UnexpectedlyCancelledRunner:
        egress_capability = "egress"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, _request: ExecutionRequest):
            return ExecutionResult(exit_code=-9, cancelled=True)

    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    with pytest.raises(RuntimeError, match="取消|cancel"):
        await service.install_workspace_dependencies(
            workspace,
            UnexpectedlyCancelledRunner(),
        )


@pytest.mark.asyncio
async def test_cancel_event_wins_over_nonzero_dependency_result(
    monkeypatch,
    tmp_path: Path,
):
    """An event set during execution maps a late nonzero result to cancel."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text(
        "example-package\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        lambda _key: _async_value(True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: type("Settings", (), {"agent_team_auto_install_deps": True})(),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        lambda: _async_value(AgentTeamNetworkPolicy.FULL_ACCESS),
    )

    cancel_event = asyncio.Event()

    class LateFailureRunner:
        egress_capability = "egress"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, _request: ExecutionRequest):
            cancel_event.set()
            return ExecutionResult(exit_code=1, stderr="late failure")

    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    await service.install_workspace_dependencies(
        workspace,
        LateFailureRunner(),
        cancel_event=cancel_event,
    )
