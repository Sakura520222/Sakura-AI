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


def _write_complete_local_venv(workspace: Path) -> Path:
    """Create the minimal launcher/pip shape a completed local venv exposes."""

    venv = workspace / ".venv" / "local"
    script_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    script_dir.mkdir(parents=True, exist_ok=True)
    launcher = script_dir / ("python.exe" if os.name == "nt" else "python")
    if os.name == "nt":
        launcher.write_text("fake interpreter", encoding="utf-8")
    else:
        try:
            launcher.symlink_to(Path(sys.executable).resolve())
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
    (script_dir / ("pip.exe" if os.name == "nt" else "pip")).write_text(
        "fake pip", encoding="utf-8"
    )
    (venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    return venv


def _write_complete_sandbox_venv(workspace: Path) -> Path:
    """Create a link-free Linux sandbox venv fixture."""

    venv = workspace / ".venv" / "sandbox"
    scripts = venv / "bin"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "python").write_text("fake interpreter", encoding="utf-8")
    (scripts / "pip").write_text("fake pip", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    (venv / "lib64").mkdir(exist_ok=True)
    return venv


@pytest.mark.asyncio
async def test_backend_switch_removes_inactive_local_venv_before_admission(
    tmp_path: Path,
):
    """Sandbox handoff must not scan a host venv's POSIX launcher symlink."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    local_venv = _write_complete_local_venv(workspace)

    await service.prepare_workspace_for_execution_backend(workspace, "sandbox")

    assert not local_venv.exists()
    assert not (workspace / ".venv" / "local").is_symlink()


@pytest.mark.asyncio
async def test_backend_switch_removes_sandbox_venv_before_local_admission(
    tmp_path: Path,
):
    """Switching back to local must not retain container-specific venv files."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    sandbox_venv = _write_complete_sandbox_venv(workspace)

    await service.prepare_workspace_for_execution_backend(workspace, "local")

    assert not sandbox_venv.exists()


@pytest.mark.asyncio
async def test_backend_switch_removes_only_the_reserved_inactive_directory(
    tmp_path: Path,
):
    """The reserved venv is disposable while sibling data stays untouched."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    local_venv = workspace / ".venv" / "local"
    local_venv.mkdir(parents=True)
    sibling = workspace / ".venv" / "keep.txt"
    sibling.write_text("keep me", encoding="utf-8")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)

    await service.prepare_workspace_for_execution_backend(workspace, "sandbox")

    assert not local_venv.exists()
    assert sibling.read_text(encoding="utf-8") == "keep me"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink cleanup contract")
@pytest.mark.asyncio
async def test_reserved_venv_symlink_is_unlinked_without_touching_target(
    tmp_path: Path,
):
    """Cleanup removes the reserved link entry and never traverses its target."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    venv_root = workspace / ".venv"
    venv_root.mkdir()
    try:
        (venv_root / "local").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    await service.prepare_workspace_for_execution_backend(workspace, "sandbox")

    assert not os.path.lexists(venv_root / "local")
    assert sentinel.read_text(encoding="utf-8") == "must survive"


@pytest.mark.asyncio
async def test_incomplete_local_venv_is_rebuilt_before_pip(
    monkeypatch,
    tmp_path: Path,
):
    """A leftover directory from a cancelled bootstrap must not skip venv setup."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    (workspace / ".venv" / "local").mkdir(parents=True)
    executor = LocalExecutionRunner(workspace, workspace_service)
    requests: list[ExecutionRequest] = []

    async def execute(request: ExecutionRequest):
        requests.append(request)
        if len(requests) == 1:
            _write_complete_local_venv(workspace)
        return ExecutionResult(exit_code=0)

    monkeypatch.setattr(executor, "execute", execute)
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

    await service.install_workspace_dependencies(workspace, executor)

    assert len(requests) == 2
    assert requests[0].argv is not None and requests[0].argv[-1] == ".venv/local"
    assert requests[1].argv is not None and requests[1].argv[1:4] == (
        "-m",
        "pip",
        "install",
    )


@pytest.mark.asyncio
async def test_failed_local_bootstrap_is_retried_on_next_admission(
    monkeypatch,
    tmp_path: Path,
):
    """A nonzero bootstrap must leave no reusable incomplete venv contract."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    service = AgentTeamGitWorkspaceService(workspace_service=workspace_service)
    executor = LocalExecutionRunner(workspace, workspace_service)
    requests: list[ExecutionRequest] = []
    attempt = 0

    async def execute(request: ExecutionRequest):
        nonlocal attempt
        requests.append(request)
        if request.argv and request.argv[-1] == ".venv/local":
            attempt += 1
            if attempt == 1:
                (workspace / ".venv" / "local").mkdir(parents=True, exist_ok=True)
                return ExecutionResult(exit_code=1, stderr="bootstrap failed")
            _write_complete_local_venv(workspace)
        return ExecutionResult(exit_code=0)

    monkeypatch.setattr(executor, "execute", execute)
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

    with pytest.raises(RuntimeError, match="创建 Agent 本地依赖 venv 失败"):
        await service.install_workspace_dependencies(workspace, executor)
    await service.install_workspace_dependencies(workspace, executor)

    assert [request.argv[-1] for request in requests if request.argv is not None].count(
        ".venv/local"
    ) == 2


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
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
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
            _write_complete_local_venv(workspace)
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
    assert Path(local_requests[1].argv[0]).relative_to(workspace).parts[:2] == (
        ".venv",
        "local",
    )
    assert all(request.cancel_event is local_cancel for request in local_requests)

    sandbox_requests: list[ExecutionRequest] = []

    class FakeSandboxRunner:
        egress_capability = "egress"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request: ExecutionRequest):
            sandbox_requests.append(request)
            if len(sandbox_requests) == 1:
                _write_complete_sandbox_venv(workspace)
            return ExecutionResult(exit_code=0)

    sandbox_cancel = asyncio.Event()
    await service.install_workspace_dependencies(
        workspace,
        FakeSandboxRunner(),
        cancel_event=sandbox_cancel,
    )

    assert sandbox_requests[0].command == (
        "python -m venv --copies /workspace/.venv/sandbox"
    )
    assert sandbox_requests[1].command.startswith("/workspace/.venv/sandbox/bin/pip ")
    assert all(request.cancel_event is sandbox_cancel for request in sandbox_requests)


@pytest.mark.asyncio
async def test_cancelled_dependency_result_stops_before_pip(
    monkeypatch,
    tmp_path: Path,
):
    """A cancelled bootstrap is not reported as a generic installation error."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
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
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
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
async def test_dependency_cleanup_error_wins_over_task_cancellation(
    tmp_path: Path,
):
    """A runner cleanup failure must not be downgraded to CANCELLED."""

    service = AgentTeamGitWorkspaceService(
        workspace_service=AgentTeamWorkspaceService(tmp_path / "workplace")
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    result = ExecutionResult(
        exit_code=-9,
        cancelled=True,
        infrastructure_error="process tree cleanup failed",
    )

    with pytest.raises(RuntimeError, match="清理失败|cleanup"):
        service._dependency_result_was_cancelled(result, cancel_event)


@pytest.mark.asyncio
async def test_cancel_event_wins_over_nonzero_dependency_result(
    monkeypatch,
    tmp_path: Path,
):
    """An event set during execution maps a late nonzero result to cancel."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
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
