"""Worker-level cancellation contracts for dependency admission."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.models.agent_team_models import AgentTeamTaskStatus
from backend.workers import agent_team_worker as worker_module
from backend.workers.agent_team_worker import AgentTeamWorker


class _BlockingDependencyService:
    """A dependency installer that only completes after its event is set."""

    workspace_service = object()

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_events: list[asyncio.Event | None] = []

    async def install_workspace_dependencies(
        self,
        workspace: Path,
        runner: object,
        *,
        cancel_event: asyncio.Event | None,
    ) -> None:
        del workspace, runner
        self.cancel_events.append(cancel_event)
        self.started.set()
        assert cancel_event is not None
        await cancel_event.wait()


@pytest.mark.asyncio
async def test_admission_forwards_cancel_event_to_dependency_installer(tmp_path: Path):
    """Admission must not orphan a dependency bootstrap when cancellation fires."""

    service = _BlockingDependencyService()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cancel_event = asyncio.Event()
    worker = AgentTeamWorker()

    async def create_runner(_workspace: Path, _workspace_service: object) -> object:
        return object()

    worker._create_agent_execution_runner = create_runner
    admission = asyncio.create_task(
        worker._admit_workspace_runner(
            service,
            workspace,
            cancel_event=cancel_event,
        )
    )

    await asyncio.wait_for(service.started.wait(), timeout=0.5)
    cancel_event.set()

    runner = await asyncio.wait_for(admission, timeout=0.5)

    assert runner is not None
    assert service.cancel_events == [cancel_event]


@pytest.mark.asyncio
async def test_process_task_cancels_during_dependency_admission_without_failure(
    monkeypatch,
    tmp_path: Path,
):
    """A cancellation during bootstrap/pip admission reaches the cancelled state."""

    task_id = 1701
    task = SimpleNamespace(
        id=task_id,
        source_type="manual_issue",
        source_id=1,
        source_issue_number=1,
        repo_owner="owner",
        repo_name="repo",
        title="dependency cancellation",
        summary="cancel while installing",
        status=AgentTeamTaskStatus.QUEUED.value,
        workspace_path=None,
        branch_name=None,
        base_branch="develop",
        base_commit_sha="base-sha",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    updates: list[dict[str, object]] = []
    service = _BlockingDependencyService()

    class _FakeGitService(_BlockingDependencyService):
        async def prepare_workspace(self, *args, **kwargs):
            del args, kwargs
            return SimpleNamespace(
                branch_name="feature/dependency-cancel",
                default_branch="develop",
                commit_sha="base-sha",
                workspace=workspace,
            )

    service = _FakeGitService()
    runner = object()

    async def load_task(_self, _task_id: int):
        return task

    async def update_task(_self, _task_id: int, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(task, key, value)

    async def create_runner(_self, _workspace: Path, _workspace_service: object):
        return runner

    async def load_skills():
        return "", {}, {}

    async def expire_prompts(_self, _task_id: int):
        return None

    monkeypatch.setattr(worker_module, "load_skills_context", load_skills)
    monkeypatch.setattr(AgentTeamWorker, "_load_task", load_task)
    monkeypatch.setattr(AgentTeamWorker, "_update_task", update_task)
    monkeypatch.setattr(AgentTeamWorker, "_create_agent_execution_runner", create_runner)
    monkeypatch.setattr(
        AgentTeamWorker,
        "_expire_pending_prompts_if_terminal",
        expire_prompts,
    )
    monkeypatch.setattr(worker_module, "AgentTeamGitWorkspaceService", lambda: service)

    processing = asyncio.create_task(AgentTeamWorker().process_task(task_id))
    await asyncio.wait_for(service.started.wait(), timeout=0.5)
    worker_module.request_task_cancel(task_id)

    assert await asyncio.wait_for(processing, timeout=0.5) == task_id
    assert len(service.cancel_events) == 1
    assert service.cancel_events[0] is not None
    assert service.cancel_events[0].is_set()
    assert any(
        update.get("status") == AgentTeamTaskStatus.CANCELLED.value
        for update in updates
    )
    assert not any(
        update.get("status") == AgentTeamTaskStatus.FAILED.value
        for update in updates
    )


@pytest.mark.parametrize(
    "method_name",
    [
        "process_task",
        "process_external_review_iteration",
        "process_human_followup_iteration",
    ],
)
def test_all_worker_admission_calls_forward_cancel_event(method_name: str):
    """Keep every worker resume path wired to the same task cancellation event."""

    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(getattr(AgentTeamWorker, method_name)))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_admit_workspace_runner"
    ]

    assert len(calls) == 1
    assert any(
        keyword.arg == "cancel_event"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "cancel_event"
        for keyword in calls[0].keywords
    )
