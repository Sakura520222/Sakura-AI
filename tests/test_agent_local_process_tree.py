"""Regression tests for LocalExecutionRunner process-tree cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.services.agent_team.execution as execution_module
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionRequest,
    LocalExecutionRunner,
    execution_workspace_key,
)
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@pytest.mark.asyncio
async def test_cancel_terminates_child_that_inherits_output_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancelling a parent must also close a child holding stdout/stderr."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")

    async def full_access_policy() -> AgentTeamNetworkPolicy:
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )

    child_started_marker = workspace / "child-started.txt"
    survived_marker = workspace / "child-survived.txt"
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        "started = pathlib.Path(sys.argv[1])\n"
        "survived = pathlib.Path(sys.argv[2])\n"
        "child_code = (\n"
        "    'import pathlib,sys,time; '\n"
        "    'time.sleep(1.0); '\n"
        "    \"pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\"\n"
        ")\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code, str(survived)])\n"
        "started.write_text('started', encoding='utf-8')\n"
    )
    runner = LocalExecutionRunner(workspace, workspace_service)
    cancel_event = asyncio.Event()
    request = ExecutionRequest(
        workspace_key=execution_workspace_key(workspace, workspace_service),
        argv=(
            "python",
            "-c",
            parent_code,
            "child-started.txt",
            "child-survived.txt",
        ),
        cancel_event=cancel_event,
        timeout_seconds=5,
    )

    async def cancel_after_child_starts() -> None:
        for _ in range(100):
            if child_started_marker.exists():
                cancel_event.set()
                return
            await asyncio.sleep(0.01)
        pytest.fail("child process did not start")

    cancel_task = asyncio.create_task(cancel_after_child_starts())
    try:
        result = await asyncio.wait_for(runner.execute(request), timeout=4)
    finally:
        await cancel_task

    assert result.cancelled
    assert not survived_marker.exists()


@pytest.mark.asyncio
async def test_timeout_terminates_child_after_parent_exits_with_pipes_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Timeout cleanup must kill a child that outlives its pipe owner."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")

    async def full_access_policy() -> AgentTeamNetworkPolicy:
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )

    child_started_marker = workspace / "timeout-child-started.txt"
    survived_marker = workspace / "timeout-child-survived.txt"
    parent_code = (
        "import pathlib, subprocess, sys\n"
        "started = pathlib.Path(sys.argv[1])\n"
        "survived = pathlib.Path(sys.argv[2])\n"
        "child_code = (\n"
        "    'import pathlib,sys,time; '\n"
        "    'time.sleep(10.0); '\n"
        "    \"pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\"\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', child_code, str(survived)])\n"
        "started.write_text('started', encoding='utf-8')\n"
    )
    runner = LocalExecutionRunner(workspace, workspace_service)
    request = ExecutionRequest(
        workspace_key=execution_workspace_key(workspace, workspace_service),
        argv=(
            "python",
            "-c",
            parent_code,
            "timeout-child-started.txt",
            "timeout-child-survived.txt",
        ),
        timeout_seconds=0.2,
    )

    result = await asyncio.wait_for(runner.execute(request), timeout=4)

    assert child_started_marker.exists()
    assert result.timed_out
    assert not survived_marker.exists()


@pytest.mark.asyncio
async def test_posix_group_is_killed_after_parent_has_been_reaped(
    monkeypatch: pytest.MonkeyPatch,
):
    """A reaped parent must not make its still-live process group invisible."""

    if execution_module.os.name == "nt":
        pytest.skip("POSIX process groups are not available on Windows")

    controller = execution_module._ProcessTreeController.__new__(
        execution_module._ProcessTreeController
    )
    controller._process = SimpleNamespace(pid=12345, returncode=0)
    controller._pid = 12345
    controller._process_group_id = 12345
    controller._kernel32 = None
    controller._job_handle = None
    controller._setup_error = None
    kill_calls: list[tuple[int, int]] = []

    def parent_was_reaped(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(execution_module.os, "getpgid", parent_was_reaped)
    monkeypatch.setattr(
        execution_module.os,
        "killpg",
        lambda process_group, signum: kill_calls.append(
            (process_group, signum)
        ),
    )

    assert await controller.terminate() is None
    assert kill_calls == [(12345, execution_module.signal.SIGKILL)]


def test_windows_process_creation_is_suspended(monkeypatch: pytest.MonkeyPatch):
    """The Windows launch contract must suspend before Job admission."""

    monkeypatch.setattr(execution_module.os, "name", "nt")

    kwargs = LocalExecutionRunner._process_group_kwargs()
    flags = int(kwargs["creationflags"])

    assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert flags & 0x00000004  # CREATE_SUSPENDED
    assert "start_new_session" not in kwargs


@pytest.mark.asyncio
async def test_windows_job_admission_precedes_process_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A suspended payload resumes only after the tree boundary is ready."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    events: list[str] = []
    captured_kwargs: dict[str, object] = {}

    async def full_access_policy() -> AgentTeamNetworkPolicy:
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    monkeypatch.setattr(execution_module.os, "name", "nt")

    class FakeProcess:
        pid = 12345
        returncode = 0

        async def communicate(self):
            events.append("payload")
            return b"", b""

        def kill(self):
            self.returncode = -9

    async def fake_shell(_command: str, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeProcess()

    def fake_attach(_controller, _pid: int) -> None:
        events.append("attach")

    def fake_resume(_controller) -> None:
        events.append("resume")

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_shell",
        fake_shell,
    )
    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "_attach_windows_job",
        fake_attach,
    )
    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "resume",
        fake_resume,
    )

    runner = LocalExecutionRunner(workspace, workspace_service)
    result = await runner.run("echo payload")

    assert result.returncode == 0
    assert events == ["attach", "resume", "payload"]
    assert int(captured_kwargs["creationflags"]) & 0x00000004


@pytest.mark.asyncio
async def test_windows_job_admission_failure_does_not_resume_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Attach failure must clean up a suspended child before raising."""

    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = workspace_service.ensure_workspace("owner", "repo")
    events: list[str] = []

    async def full_access_policy() -> AgentTeamNetworkPolicy:
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    monkeypatch.setattr(execution_module.os, "name", "nt")

    class FakeProcess:
        pid = 12345
        returncode = None

        async def communicate(self):
            events.append("communicate")
            return b"", b""

        def kill(self):
            self.returncode = -9

    async def fake_shell(_command: str, **_kwargs):
        return FakeProcess()

    def failed_attach(_controller, _pid: int) -> None:
        events.append("attach")
        raise RuntimeError("simulated Job Object attach failure")

    def unexpected_resume(_controller) -> None:
        events.append("resume")
        raise AssertionError("payload must remain suspended")

    async def fake_taskkill(_controller) -> None:
        events.append("taskkill")

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_shell",
        fake_shell,
    )
    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "_attach_windows_job",
        failed_attach,
    )
    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "resume",
        unexpected_resume,
    )
    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "_terminate_windows_with_taskkill",
        fake_taskkill,
    )

    runner = LocalExecutionRunner(workspace, workspace_service)
    with pytest.raises(ExecutionError, match="隔离初始化失败"):
        await runner.run("echo payload")

    assert events == ["attach", "taskkill", "communicate"]
    assert "resume" not in events
