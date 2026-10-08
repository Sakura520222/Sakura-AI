"""All worker entry points preserve Harness terminal reasons without publishing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.workers import agent_team_worker as module


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["initial", "review", "followup"])
@pytest.mark.parametrize(
    ("outcome_state", "reason", "expected_status"),
    [
        ("blocked", "no_progress", "failed"),
        ("cancelled", "cancelled", "cancelled"),
        ("unrecoverable_error", "empty_response", "failed"),
        ("exception", "runtime_failure", "failed"),
        ("cancel_signal", "cancelled", "cancelled"),
    ],
)
@pytest.mark.parametrize("files", [[], ["src/changed.py"]])
async def test_runtime_outcome_survives_worker_and_cannot_publish(
    monkeypatch, tmp_path, entry, outcome_state, reason, expected_status, files
):
    task = SimpleNamespace(
        id=912,
        source_type="manual_issue",
        source_id=1,
        source_issue_number=1,
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        title="Harness terminal state",
        summary="implementation task",
        status="queued" if entry == "initial" else "iterating",
        workspace_path=str(tmp_path),
        branch_name="feature/agent-912",
        base_branch="develop",
        base_commit_sha="base",
        resume_count=0,
        iteration_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        estimated_cost=0,
        pr_number=42,
        error_message=None,
    )
    outcome = SimpleNamespace(
        success=False,
        outcome=outcome_state,
        reason=reason,
        iterations=1,
        prompt_tokens=17,
        completion_tokens=3,
        total_tool_calls=2,
        fullstack_result=None,
        review_result=None,
        modified_files=files,
    )
    workspace_info = SimpleNamespace(
        workspace=tmp_path,
        branch_name=task.branch_name,
        commit_sha="base",
        default_branch="develop",
    )

    class GitService:
        workspace_service = object()

        async def prepare_workspace(self, *_args, **_kwargs):
            return workspace_info

        async def resume_workspace(self, *_args, **_kwargs):
            return workspace_info

        async def prepare_workspace_for_execution_backend(self, *_args):
            return None

        async def install_workspace_dependencies(self, *_args, **_kwargs):
            return None

    class Loop:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, **_kwargs):
            if outcome_state == "exception":
                raise RuntimeError(reason)
            if outcome_state == "cancel_signal":
                raise asyncio.CancelledError
            return outcome

    worker = module.AgentTeamWorker()
    records = []

    async def load(_task_id):
        return task

    async def update(_task_id, **values):
        records.append(values)
        for key, value in values.items():
            setattr(task, key, value)

    async def no_op(*_args, **_kwargs):
        return None

    async def text_context(*_args, **_kwargs):
        return ""

    async def memory(*_args):
        return {"text": "", "github_repo": None, "sakura_ref": None}

    async def skills():
        return "", {}, {}

    def forbid_publication():
        pytest.fail("terminal failure reached GitHub publication")

    monkeypatch.setattr(worker, "_load_task", load)
    monkeypatch.setattr(worker, "_update_task", update)
    monkeypatch.setattr(worker, "_save_iteration", no_op)
    monkeypatch.setattr(worker, "_expire_pending_prompts_if_terminal", no_op)
    monkeypatch.setattr(worker, "_create_agent_execution_runner", no_op)
    monkeypatch.setattr(worker, "_load_task_reference_context", text_context)
    monkeypatch.setattr(worker, "_load_sakura_pr_review_feedback", text_context)
    monkeypatch.setattr(module, "load_skills_context", skills)
    monkeypatch.setattr(module, "load_sakura_memory", memory)
    monkeypatch.setattr(module, "AgentTeamGitWorkspaceService", GitService)
    monkeypatch.setattr(module, "IterationLoopService", Loop)
    monkeypatch.setattr(module, "AgentTeamPRService", forbid_publication)

    if entry == "initial":
        processing = worker.process_task(task.id)
    elif entry == "review":
        processing = worker.process_external_review_iteration(task.id, 123)
    else:
        processing = worker.process_human_followup_iteration(task.id)
    if outcome_state == "cancel_signal":
        with pytest.raises(asyncio.CancelledError):
            await processing
    else:
        await processing

    assert task.status == expected_status
    expected_phase = {
        "exception": "unrecoverable_error",
        "cancel_signal": "cancelled",
    }.get(outcome_state, outcome_state)
    assert task.current_phase == expected_phase
    assert reason in task.error_message
    assert not any(item.get("current_phase") == "pushing" for item in records)
    assert task.id not in module._cancel_events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "phase", "owner", "allowed"),
    [
        ("failed", "blocked", "owner", True),
        ("waiting_human", "blocked", "owner", False),
        ("waiting_human", "waiting_human", "owner", False),
        ("waiting_human", "blocked", "someone_else", False),
        ("failed", "unrecoverable_error", "owner", True),
        ("cancelled", "cancelled", "owner", True),
        ("queued", "resuming", "owner", False),
    ],
)
async def test_blocked_checkpoint_resume_preserves_owner_and_status_checks(
    monkeypatch, status, phase, owner, allowed
):
    from fastapi import BackgroundTasks

    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )
    from backend.webui.routes import agent_team as routes

    task = SimpleNamespace(
        id=912,
        status=status,
        current_phase=phase,
        started_by=owner,
        workspace_path="/workspace/owner/repo",
        branch_name="feature/agent-912",
        resume_count=0,
    )
    commits = []

    class DB:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: task)

        async def commit(self):
            commits.append(task.status)

    async def has_checkpoint(_self):
        return True

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        ConversationCheckpointService, "has_resume_state", has_checkpoint
    )
    monkeypatch.setattr(routes, "log_admin_action", audit)
    background = BackgroundTasks()
    response = await routes.resume_task(
        task.id,
        background,
        db=DB(),
        user={"sub": "owner", "user_id": 1, "role": "user"},
        csrf_token="valid",
    )
    assert json.loads(response.body)["success"] is allowed
    assert commits == (["queued"] if allowed else [])
    assert len(background.tasks) == int(allowed)
    if allowed:
        assert task.current_phase == "resuming"
        assert task.resume_count == 1
    if owner != "owner":
        assert response.status_code == 403


@pytest.mark.parametrize("status", ["failed", "waiting_human"])
def test_blocked_task_uses_existing_failure_actions_in_rendered_console(status):
    from backend.webui.deps import get_templates

    task = SimpleNamespace(
        id=912,
        status=status,
        current_phase="blocked",
        title="Blocked task",
        workspace_path="/workspace/owner/repo",
        branch_name="feature/agent-912",
        last_checkpoint_at=True,
        pr_url=None,
    )
    template = get_templates().env.get_template(
        "components/agent_team_task_list_fragment.html"
    )
    rendered = template.render(
        tasks=[task], page=1, total_pages=1, total=1, sort="updated"
    )
    assert ('data-task-action="resume"' in rendered) is (status == "failed")
    assert ('data-task-action="retry"' in rendered) is (status == "failed")


@pytest.fixture(autouse=True)
def worker_control_audit_store(monkeypatch):
    """Worker orchestration uses fake tasks; persistence has separate SQLite tests."""
    from unittest.mock import AsyncMock

    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )

    monkeypatch.setattr(
        ConversationCheckpointService, "record_control_event", AsyncMock()
    )
