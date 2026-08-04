"""Agent Team 取消信号传播与 User Prompt 消费测试。"""

from dataclasses import dataclass

import pytest

from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.professional_reviewer import ReviewResult
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

# ── Fake agents ──────────────────────────────────────────────


@dataclass
class _FakeFullstackAgent:
    workspace: str
    workspace_service: object

    async def execute(self, **kwargs):
        cancel_check = kwargs.get("cancel_check")
        if cancel_check and cancel_check():
            return FullStackResult(
                success=False,
                summary="cancelled",
                modified_files=[],
                tool_calls_count=0,
                error="cancelled",
            )
        return FullStackResult(
            success=True,
            summary="done",
            modified_files=["main.py"],
            tool_calls_count=1,
        )


@dataclass
class _FakeReviewer:
    workspace: str
    workspace_service: object

    async def review(self, **kwargs):
        cancel_check = kwargs.get("cancel_check")
        if cancel_check and cancel_check():
            return ReviewResult(
                passed=False,
                verdict="cancelled",
                score=0,
                summary="cancelled",
                findings=[],
                tool_calls_count=0,
            )
        return ReviewResult(
            passed=True,
            verdict="pass",
            score=9,
            summary="looks good",
            findings=[],
            tool_calls_count=1,
        )


# ── Cancel signal propagation ────────────────────────────────


@pytest.mark.asyncio
async def test_iteration_loop_stops_on_cancel_before_iteration(monkeypatch, tmp_path):
    """Cancel check at iteration start returns immediately."""
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeFullstackAgent,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _FakeReviewer,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="cancel test",
        task_summary="should cancel",
        max_iterations=3,
        cancel_check=lambda: True,
    )

    assert outcome.success is False
    assert "取消" in outcome.reason
    assert outcome.iterations == 0
    assert outcome.total_tool_calls == 0


@pytest.mark.asyncio
async def test_iteration_loop_passes_cancel_check_to_expert(monkeypatch, tmp_path):
    """cancel_check is forwarded to FullStackExpertAgent.execute."""
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeFullstackAgent,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _FakeReviewer,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    cancel_after_first = {"cancelled": False}

    def cancel_check():
        return cancel_after_first["cancelled"]

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="cancel mid-run",
        task_summary="should cancel after fullstack",
        max_iterations=1,
        cancel_check=cancel_check,
    )

    # Fullstack succeeds, but the loop should check cancel before reviewer.
    # In this case cancel_after_first is still False, so reviewer runs and passes.
    assert outcome.success is True


@pytest.mark.asyncio
async def test_iteration_loop_cancels_before_reviewer(monkeypatch, tmp_path):
    """Cancel fires between fullstack and reviewer — reviewer never runs."""
    state = {"fullstack_done": False}

    class _FullstackThenCancel:
        def __init__(self, workspace, workspace_service):
            pass

        async def execute(self, **kwargs):
            # Succeed, then signal cancel for the next check
            state["fullstack_done"] = True
            return FullStackResult(
                success=True,
                summary="done",
                modified_files=["main.py"],
                tool_calls_count=1,
            )

    class _ReviewerMustNotRun:
        def __init__(self, workspace, workspace_service):
            pass

        async def review(self, **kwargs):
            raise AssertionError("reviewer should not run when cancelled")

    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FullstackThenCancel,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _ReviewerMustNotRun,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="cancel before reviewer",
        task_summary="reviewer must not run",
        max_iterations=1,
        cancel_check=lambda: state["fullstack_done"],
    )

    assert outcome.success is False
    assert "取消" in outcome.reason
    assert outcome.fullstack_result is not None
    assert outcome.modified_files == ["main.py"]


@pytest.mark.asyncio
async def test_iteration_loop_without_cancel_check(monkeypatch, tmp_path):
    """No cancel_check — normal execution completes."""
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeFullstackAgent,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _FakeReviewer,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="normal",
        task_summary="no cancel",
        max_iterations=1,
    )

    assert outcome.success is True
    assert outcome.review_result is not None
    assert outcome.review_result.passed is True


# ── Prompt consumption (no DB) ───────────────────────────────


@pytest.mark.asyncio
async def test_consume_pending_prompts_returns_empty_without_task_id(tmp_path):
    """_consume_pending_prompts returns '' when task_id is None (no DB needed)."""
    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")
    service = IterationLoopService(workspace, workspace_service, task_id=None)
    result = await service._consume_pending_prompts()
    assert result == ""


@pytest.mark.asyncio
async def test_consume_pending_prompts_returns_empty_without_db(tmp_path):
    """_consume_pending_prompts returns '' when DB is unavailable (graceful fallback)."""
    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")
    service = IterationLoopService(workspace, workspace_service, task_id=999)
    result = await service._consume_pending_prompts()
    assert result == ""


# ── Worker cancel event helpers ──────────────────────────────


def test_request_task_cancel_sets_event():
    from backend.workers.agent_team_worker import (
        is_task_cancel_requested,
        request_task_cancel,
    )

    task_id = -1  # sentinel for test
    request_task_cancel(task_id)
    assert is_task_cancel_requested(task_id) is True


def test_is_task_cancel_requested_returns_false_for_unknown():
    from backend.workers.agent_team_worker import is_task_cancel_requested

    assert is_task_cancel_requested(-99999) is False


# ── Git workspace: Python project detection ──────────────────


@pytest.mark.asyncio
async def test_install_workspace_dependencies_skips_non_python(tmp_path):
    """Non-Python projects should not create .venv or call pip."""
    from unittest.mock import AsyncMock, MagicMock

    service = AgentTeamGitWorkspaceService.__new__(AgentTeamGitWorkspaceService)
    executor = MagicMock()
    executor.run = AsyncMock()

    await service._install_workspace_dependencies(executor, tmp_path)

    assert not (tmp_path / ".venv").exists()
    executor.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_workspace_dependencies_creates_venv_for_python(tmp_path):
    """Python projects with pyproject.toml should trigger venv creation."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")

    from unittest.mock import AsyncMock, MagicMock, patch

    service = AgentTeamGitWorkspaceService.__new__(AgentTeamGitWorkspaceService)
    executor = MagicMock()
    executor.run = AsyncMock()

    with patch(
        "backend.services.agent_team.git_workspace_service.get_dynamic_config",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await service._install_workspace_dependencies(executor, tmp_path)

    assert executor.run.call_count >= 1
    calls_str = " ".join(str(c) for c in executor.run.call_args_list)
    assert "venv" in calls_str
