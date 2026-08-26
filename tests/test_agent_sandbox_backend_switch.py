"""Backend admission and dependency-isolation contracts for Agent sandboxing."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
    execution_workspace_key,
    resolve_execution_runner,
)
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.sandbox_client import (
    SandboxExecutionRunner,
    create_execution_runner,
    resolve_execution_backend,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.workers.agent_team_worker import AgentTeamWorker


def test_missing_runner_injection_fails_closed(tmp_path: Path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")

    with pytest.raises(ExecutionError, match="explicitly injected"):
        resolve_execution_runner(None, workspace, service)


def test_backend_selection_requires_explicit_value_and_local_is_source_only():
    with pytest.raises(ValueError, match="explicitly configured"):
        resolve_execution_backend(None, deploy_mode="source")
    assert resolve_execution_backend("local", deploy_mode="source") == "local"
    assert resolve_execution_backend("sandbox", deploy_mode="image") == "sandbox"
    with pytest.raises(ValueError, match="requires deploy_mode='source'"):
        resolve_execution_backend("local", deploy_mode="image")
    for deploy_mode in ("unknown", "", "SOURCE", " source ", "production"):
        with pytest.raises(ValueError, match="requires deploy_mode='source'"):
            resolve_execution_backend("local", deploy_mode=deploy_mode)


def test_runner_factory_creates_only_explicit_local_or_sandbox(tmp_path: Path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")

    local = create_execution_runner(
        str(workspace),
        service,
        backend="local",
        deploy_mode="source",
    )
    assert isinstance(local, LocalExecutionRunner)

    sandbox = create_execution_runner(
        str(workspace),
        service,
        backend="sandbox",
        deploy_mode="source",
        socket_path=str(tmp_path / "sandboxd.sock"),
    )
    assert isinstance(sandbox, SandboxExecutionRunner)


@pytest.mark.asyncio
async def test_local_explicit_runner_honors_workspace_cancel_event(tmp_path: Path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    runner = LocalExecutionRunner(workspace, service)
    cancel_event = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.05)
        cancel_event.set()

    task = asyncio.create_task(cancel_soon())
    try:
        result = await runner.execute(
            ExecutionRequest(
                workspace_key=execution_workspace_key(workspace, service),
                command="python -c \"import time; time.sleep(2)\"",
                cwd=PurePosixPath("."),
                timeout_seconds=5,
                cancel_event=cancel_event,
            )
        )
        assert result.cancelled
    finally:
        await task


@pytest.mark.asyncio
async def test_worker_admission_creates_runner_before_dependency_install(
    monkeypatch,
    tmp_path: Path,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    calls: list[tuple[Path, object]] = []
    runner = SimpleNamespace(
        supports_profile=lambda profile: profile
        in {ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY}
    )

    class FakeGitService:
        workspace_service = service

        async def install_workspace_dependencies(self, path, execution_runner):
            calls.append((Path(path), execution_runner))

    worker = AgentTeamWorker()

    async def fake_create(path, workspace_service):
        assert Path(path) == workspace
        assert workspace_service is service
        return runner

    monkeypatch.setattr(worker, "_create_agent_execution_runner", fake_create)
    admitted = await worker._admit_workspace_runner(FakeGitService(), workspace)

    assert admitted is runner
    assert calls == [(workspace, runner)]


@pytest.mark.asyncio
async def test_dependency_installation_uses_dependency_profile_only(
    monkeypatch,
    tmp_path: Path,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    requests = []

    class FakeSandboxRunner:
        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request):
            requests.append(request)
            return ExecutionResult(
                command=request.command or " ".join(request.argv or ()),
                cwd=".",
                exit_code=0,
            )

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: SimpleNamespace(agent_team_auto_install_deps=True),
    )

    async def install_enabled(_key):
        return True

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        install_enabled,
    )

    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    await git_service.install_workspace_dependencies(workspace, FakeSandboxRunner())

    assert len(requests) == 2
    assert all(item.profile is ExecutionProfile.DEPENDENCY for item in requests)
    assert requests[0].command == "python -m venv .venv"
    assert requests[1].command == ".venv/bin/pip install -r requirements.txt --quiet"
def test_all_production_worker_loops_inject_the_admitted_runner():
    source = Path("backend/workers/agent_team_worker.py").read_text(encoding="utf-8")
    assert source.count("execution_runner=execution_runner") == 3
    assert source.count("_admit_workspace_runner(") - 1 == 3
