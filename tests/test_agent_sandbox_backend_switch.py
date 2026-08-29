"""Backend admission and dependency-isolation contracts for Agent sandboxing."""

from __future__ import annotations

import asyncio
import os
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
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.sandbox_client import (
    SandboxExecutionRunner,
    create_execution_runner,
    resolve_execution_backend,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.workers.agent_team_worker import AgentTeamWorker


async def _async_value(value):
    return value


def _write_complete_sandbox_venv(workspace: Path) -> None:
    venv = workspace / ".venv" / "sandbox"
    scripts = venv / "bin"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "python").write_text("fake interpreter", encoding="utf-8")
    (scripts / "pip").write_text("fake pip", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    (venv / "lib64").mkdir(exist_ok=True)


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
async def test_local_explicit_runner_honors_workspace_cancel_event(
    tmp_path: Path,
    monkeypatch,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    runner = LocalExecutionRunner(workspace, service)
    cancel_event = asyncio.Event()

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )

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
    prepared: list[tuple[Path, str]] = []
    runner = SimpleNamespace(
        supports_profile=lambda profile: profile
        in {ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY}
    )

    class FakeGitService:
        workspace_service = service

        async def prepare_workspace_for_execution_backend(self, path, backend):
            prepared.append((Path(path), backend))

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
    assert prepared == [(workspace, "sandbox")]
    assert calls == [(workspace, runner)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency_file", "auto_install_enabled"),
    [
        (None, True),
        ("requirements.txt", False),
    ],
    ids=["non-python-workspace", "auto-install-disabled"],
)
async def test_local_worker_admission_skips_inapplicable_dependency_install(
    monkeypatch,
    tmp_path: Path,
    dependency_file: str | None,
    auto_install_enabled: bool,
):
    """Local source development must admit workspaces without dependency work."""

    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    if dependency_file:
        (workspace / dependency_file).write_text(
            "example-package\n", encoding="utf-8"
        )

    async def backend_config(key: str):
        assert key == "agent_team_execution_backend"
        return "local"

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_dynamic_config_fresh",
        backend_config,
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_settings",
        lambda: SimpleNamespace(sakura_deploy_mode="source"),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: SimpleNamespace(
            agent_team_auto_install_deps=auto_install_enabled,
        ),
    )

    async def configured_auto_install(_key: str):
        return auto_install_enabled

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        configured_auto_install,
    )

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        full_access_policy,
    )

    worker = AgentTeamWorker()
    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    prepared: list[tuple[Path, str]] = []

    async def prepare(path, backend):
        prepared.append((Path(path), backend))

    monkeypatch.setattr(
        git_service,
        "prepare_workspace_for_execution_backend",
        prepare,
    )
    admitted = await worker._admit_workspace_runner(git_service, workspace)

    assert isinstance(admitted, LocalExecutionRunner)
    assert prepared == [(workspace, "local")]


@pytest.mark.asyncio
async def test_sandbox_worker_prepares_before_dependency_policy_skip(
    monkeypatch,
    tmp_path: Path,
):
    """Policy skip still requires backend preparation before first execution."""

    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    prepared: list[tuple[Path, str]] = []

    async def prepare(path, backend):
        prepared.append((Path(path), backend))

    monkeypatch.setattr(
        git_service,
        "prepare_workspace_for_execution_backend",
        prepare,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: SimpleNamespace(agent_team_auto_install_deps=True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        lambda _key: _async_value(True),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        lambda: _async_value(AgentTeamNetworkPolicy.WEB_TOOLS),
    )
    runner = SimpleNamespace(
        egress_capability="none",
        supports_profile=lambda profile: profile is ExecutionProfile.DEPENDENCY,
    )

    async def create_runner(_workspace, _workspace_service):
        return runner

    worker = AgentTeamWorker()
    monkeypatch.setattr(worker, "_create_agent_execution_runner", create_runner)

    assert await worker._admit_workspace_runner(git_service, workspace) is runner
    assert prepared == [(workspace, "sandbox")]
    assert not (workspace / ".venv").exists()


@pytest.mark.asyncio
async def test_local_worker_admission_installs_python_dependencies_with_local_runner(
    monkeypatch,
    tmp_path: Path,
):
    """Local/full-access source mode must run dependency setup through the local runner."""

    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text(
        "example-package\n", encoding="utf-8"
    )

    async def backend_config(key: str):
        assert key == "agent_team_execution_backend"
        return "local"

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_dynamic_config_fresh",
        backend_config,
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_settings",
        lambda: SimpleNamespace(sakura_deploy_mode="source"),
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_settings",
        lambda: SimpleNamespace(agent_team_auto_install_deps=True),
    )

    async def install_enabled(_key: str):
        return True

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        install_enabled,
    )

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        full_access_policy,
    )

    commands: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            self.returncode = -9

    async def fake_shell(command, **_kwargs):
        commands.append(command)
        return FakeProcess()

    async def fake_exec(*args, **_kwargs):
        commands.append(" ".join(str(arg) for arg in args))
        if "venv" in args:
            venv_root = workspace / ".venv" / "local"
            launcher = venv_root / (
                "Scripts" if os.name == "nt" else "bin"
            ) / ("python.exe" if os.name == "nt" else "python")
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("fake interpreter", encoding="utf-8")
            (launcher.parent / ("pip.exe" if os.name == "nt" else "pip")).write_text(
                "fake pip", encoding="utf-8"
            )
            (venv_root / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(
        "backend.services.agent_team.execution.asyncio.create_subprocess_shell",
        fake_shell,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.execution.asyncio.create_subprocess_exec",
        fake_exec,
    )

    requests: list[ExecutionRequest] = []
    real_execute = LocalExecutionRunner.execute

    async def observed_execute(self, request):
        requests.append(request)
        return await real_execute(self, request)

    monkeypatch.setattr(LocalExecutionRunner, "execute", observed_execute)

    worker = AgentTeamWorker()
    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    admitted = await worker._admit_workspace_runner(git_service, workspace)

    assert isinstance(admitted, LocalExecutionRunner)
    assert [request.profile for request in requests] == [
        ExecutionProfile.DEPENDENCY,
        ExecutionProfile.DEPENDENCY,
    ]
    assert len(commands) == 2
    assert "venv" in commands[0]
    assert "requirements.txt" in commands[1]


@pytest.mark.asyncio
async def test_worker_runner_factory_reads_backend_fresh_after_cross_worker_switch(
    monkeypatch,
    tmp_path: Path,
):
    """A long-lived worker must not reuse a stale local backend setting."""

    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    selected_backends: list[str] = []
    backend_values = iter(("local", "sandbox"))

    async def fresh_config(key: str):
        assert key == "agent_team_execution_backend"
        return next(backend_values)

    async def fake_create_ready(_path: str, _workspace_service, **kwargs):
        selected_backends.append(kwargs["backend"])
        return object()

    # The process snapshot intentionally remains local while the simulated
    # AppConfig value changes on the second task admission.
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_dynamic_config_fresh",
        fresh_config,
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_settings",
        lambda: SimpleNamespace(
            agent_team_execution_backend="local",
            sakura_deploy_mode="source",
        ),
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.create_ready_execution_runner",
        fake_create_ready,
    )

    worker = AgentTeamWorker()
    await worker._create_agent_execution_runner(workspace, service)
    await worker._create_agent_execution_runner(workspace, service)

    assert selected_backends == ["local", "sandbox"]


@pytest.mark.asyncio
async def test_worker_runner_factory_fails_closed_when_backend_db_read_fails(
    monkeypatch,
    tmp_path: Path,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    factory_called = False

    async def broken_config(_key: str):
        raise RuntimeError("database connection details must not escape")

    async def fake_create_ready(*_args, **_kwargs):
        nonlocal factory_called
        factory_called = True
        return object()

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.get_dynamic_config_fresh",
        broken_config,
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.create_ready_execution_runner",
        fake_create_ready,
    )

    with pytest.raises(RuntimeError, match="configuration is unavailable"):
        await AgentTeamWorker()._create_agent_execution_runner(workspace, service)
    assert factory_called is False


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
        egress_capability = "egress"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request):
            requests.append(request)
            if request.command and "-m venv" in request.command:
                _write_complete_sandbox_venv(workspace)
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

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        full_access_policy,
    )

    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    await git_service.install_workspace_dependencies(workspace, FakeSandboxRunner())

    assert len(requests) == 2
    assert all(item.profile is ExecutionProfile.DEPENDENCY for item in requests)
    assert requests[0].command == "python -m venv --copies /workspace/.venv/sandbox"
    assert requests[1].command == (
        "/workspace/.venv/sandbox/bin/pip install -r requirements.txt --quiet"
    )


@pytest.mark.asyncio
async def test_dependency_installation_is_explicitly_skipped_for_offline_policy(
    monkeypatch,
    tmp_path: Path,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    requests = []

    class OfflineSandboxRunner:
        egress_capability = "none"

        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        async def execute(self, request):
            requests.append(request)
            return ExecutionResult(exit_code=0)

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
    await git_service.install_workspace_dependencies(workspace, OfflineSandboxRunner())

    assert requests == []
    assert not (workspace / ".venv").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("egress_capability", ["host", "bridge", "container:other", "bad network"])
async def test_dependency_installation_rejects_uncontrolled_network_capability(
    monkeypatch,
    tmp_path: Path,
    egress_capability: str,
):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "requirements.txt").write_text("example-package\n", encoding="utf-8")

    class UnsafeSandboxRunner:
        def supports_profile(self, profile):
            return profile is ExecutionProfile.DEPENDENCY

        @property
        def egress_capability(self):
            return egress_capability

        async def execute(self, request):
            raise AssertionError(f"unsafe capability must not execute: {request}")

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

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.git_workspace_service.get_agent_team_network_policy",
        full_access_policy,
    )

    git_service = AgentTeamGitWorkspaceService(workspace_service=service)
    with pytest.raises(RuntimeError, match="sandboxd egress capability"):
        await git_service.install_workspace_dependencies(
            workspace,
            UnsafeSandboxRunner(),
        )


def test_all_production_worker_loops_inject_the_admitted_runner():
    source = Path("backend/workers/agent_team_worker.py").read_text(encoding="utf-8")
    assert source.count("execution_runner=execution_runner") == 3
    assert source.count("_admit_workspace_runner(") - 1 == 3
