"""Agent Team PR closed-loop worker lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.models.agent_team_models import AgentTeamTaskStatus
from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.agent_team.iteration_loop import IterationOutcome
from backend.workers import agent_team_worker as worker_module
from backend.workers.agent_team_worker import AgentTeamWorker


async def _fake_skills_context():
    return "skills summary", {"skills": []}, {"snapshot": []}


async def _fake_sakura_memory(repo_owner, repo_name):
    return {"text": "sakura memory", "github_repo": None, "sakura_ref": None}


async def _fake_expire_pending_prompts(self, task_id):
    return None


async def _fake_max_files_config(self, key):
    return "30"


def _fake_settings():
    return SimpleNamespace(
        agent_team_branch_index_delay=0,
        agent_team_draft_pr=True,
        agent_team_pr_closed_loop_enabled=True,
        review_price_per_1k_prompt=0.001,
        review_price_per_1k_completion=0.002,
    )


@pytest.mark.asyncio
async def test_guidance_admission_failure_keeps_pending_prompts_for_retry(
    monkeypatch,
):
    worker = AgentTeamWorker()
    task = SimpleNamespace(
        status=AgentTeamTaskStatus.FAILED.value,
        error_message="Agent 执行失败: guidance_admission_failed",
    )
    expired: list[int] = []

    async def load_task(task_id):
        return task

    async def expire_pending(task_id):
        expired.append(task_id)

    monkeypatch.setattr(worker, "_load_task", load_task)
    monkeypatch.setattr(worker, "_expire_pending_prompts", expire_pending)

    await worker._expire_pending_prompts_if_terminal(7)

    assert expired == []
    assert (
        worker_module._format_failure_reason(
            "Agent 执行失败: guidance_admission_failed", []
        )
        == "Agent 执行失败: guidance_admission_failed"
    )


def _passing_outcome(
    *, modified_files=None, iterations=1, prompt_tokens=100, completion_tokens=50
):
    files = modified_files or ["backend/example.py"]
    return IterationOutcome(
        success=True,
        reason="passed",
        iterations=iterations,
        fullstack_result=FullStackResult(
            success=True,
            summary="implemented",
            modified_files=files,
            tool_calls_count=1,
        ),
        review_result=None,
        modified_files=files,
        total_tool_calls=1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _make_task(**overrides):
    data = {
        "id": 101,
        "source_type": "manual_issue",
        "source_id": 123,
        "source_issue_number": 12,
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "title": "Fix closed loop",
        "summary": "Task summary",
        "status": AgentTeamTaskStatus.QUEUED.value,
        "current_phase": None,
        "branch_name": None,
        "workspace_path": None,
        "base_branch": "develop",
        "base_commit_sha": "base-sha",
        "resume_count": 0,
        "pr_number": None,
        "pr_url": None,
        "pr_head_sha": None,
        "iteration_count": 0,
        "max_iterations": 3,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost": 0,
        "error_message": "old error",
        "failed_phase": "old phase",
        "failed_role": "old role",
        "rate_limit_reset_at": "old reset",
        "completed_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_agent_worker_leaves_draft_pr_opened_without_submitting_review(
    monkeypatch, tmp_path
):
    task = _make_task()
    updates = []
    saved_iterations = []
    pushed = []
    created_prs = []
    submitted_reviews = []

    class FakeGitWorkspaceService:
        workspace_service = SimpleNamespace()  # IterationLoopService 需要此属性

        async def prepare_workspace(
            self,
            repo_owner,
            repo_name,
            issue_number,
            source_id,
            base_branch,
            task_id,
            source_type=None,
        ):
            assert task_id == 101
            return SimpleNamespace(
                branch_name="feature/agent-101",
                default_branch="develop",
                commit_sha="base-sha",
                workspace=tmp_path,
            )

        async def get_diff_summary(self, workspace):
            return "diff summary"

    class FakeLoopService:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, **kwargs):
            return _passing_outcome()

    class FakePRService:
        def build_pr_body(self, **kwargs):
            return "fallback body"

        async def generate_pr_body(self, **kwargs):
            return kwargs["fallback_body"]

        async def generate_pr_title(self, **kwargs):
            return "feat(agent): fix closed loop"

        async def generate_commit_message(self, **kwargs):
            return kwargs.get("fallback_message") or "feat(agent): auto"

        async def commit_and_push(self, **kwargs):
            pushed.append(kwargs)
            return "commit-sha"

        async def create_pull_request(self, **kwargs):
            created_prs.append(kwargs)
            return SimpleNamespace(
                pr_number=42,
                pr_url="https://github.example/owner/repo/pull/42",
                commit_sha="",
                branch_name=kwargs["head_branch"],
                head_sha="head-sha",
            )

    async def fake_load_task(self, task_id):
        return task

    async def fake_update_task(self, task_id, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(task, key, value)

    async def fake_save_iteration(self, **kwargs):
        saved_iterations.append(kwargs)

    async def fake_resolve_bool_config(self, key, fallback):
        return True

    async def fake_submit_review(pr_info):
        submitted_reviews.append(pr_info)
        raise AssertionError("direct Sakura review submission should not be called")

    monkeypatch.setattr(worker_module, "load_skills_context", _fake_skills_context)
    monkeypatch.setattr(worker_module, "load_sakura_memory", _fake_sakura_memory)
    monkeypatch.setattr(worker_module, "get_settings", _fake_settings)
    monkeypatch.setattr(
        worker_module, "AgentTeamGitWorkspaceService", FakeGitWorkspaceService
    )
    monkeypatch.setattr(worker_module, "IterationLoopService", FakeLoopService)
    monkeypatch.setattr(worker_module, "AgentTeamPRService", FakePRService)
    monkeypatch.setattr(worker_module.AgentTeamWorker, "_load_task", fake_load_task)
    monkeypatch.setattr(worker_module.AgentTeamWorker, "_update_task", fake_update_task)
    monkeypatch.setattr(
        worker_module.AgentTeamWorker, "_save_iteration", fake_save_iteration
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker,
        "_expire_pending_prompts",
        _fake_expire_pending_prompts,
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker, "_resolve_bool_config", fake_resolve_bool_config
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker, "_get_config", _fake_max_files_config
    )
    monkeypatch.setattr(
        "backend.workers.review_worker.submit_review_task",
        fake_submit_review,
    )

    worker = worker_module.AgentTeamWorker()
    await worker.process_task(task.id)

    assert saved_iterations
    assert pushed and pushed[0]["branch_name"] == "feature/agent-101"
    assert created_prs and created_prs[0]["draft"] is True
    assert submitted_reviews == []

    final_update = updates[-1]
    assert final_update["status"] == AgentTeamTaskStatus.PR_OPENED.value
    assert final_update["current_phase"] == "pr_opened"
    assert final_update["pr_number"] == 42
    assert final_update["pr_url"] == "https://github.example/owner/repo/pull/42"
    assert final_update["pr_head_sha"] == "head-sha"
    assert final_update["estimated_cost"] >= 0
    assert final_update["error_message"] is None
    assert final_update["failed_phase"] is None
    assert final_update["failed_role"] is None
    assert final_update["rate_limit_reset_at"] is None
    assert "completed_at" not in final_update


@pytest.mark.asyncio
async def test_external_review_iteration_pushes_same_branch_and_waits_for_synchronize_webhook(
    monkeypatch, tmp_path
):
    task = _make_task(
        status=AgentTeamTaskStatus.ITERATING.value,
        current_phase=AgentTeamTaskStatus.ITERATING.value,
        workspace_path=str(tmp_path),
        branch_name="feature/agent-101",
        base_branch="develop",
        base_commit_sha="base-sha",
        pr_number=42,
        pr_url="https://github.example/owner/repo/pull/42",
        pr_head_sha="old-sha",
        iteration_count=1,
        max_iterations=3,
        prompt_tokens=10,
        completion_tokens=5,
    )
    updates = []
    saved_iterations = []
    resumes = []
    pushed = []
    updated_bodies = []
    created_prs = []
    submitted_reviews = []
    run_kwargs = []

    class FakeGitWorkspaceService:
        workspace_service = SimpleNamespace()  # IterationLoopService 需要此属性

        async def resume_workspace(
            self,
            repo_owner,
            repo_name,
            workspace_path,
            branch_name,
            base_branch,
            base_commit_sha,
        ):
            resumes.append(
                (
                    repo_owner,
                    repo_name,
                    workspace_path,
                    branch_name,
                    base_branch,
                    base_commit_sha,
                )
            )
            return SimpleNamespace(
                branch_name=branch_name,
                default_branch=base_branch,
                commit_sha=base_commit_sha,
                workspace=tmp_path,
            )

    class FakeLoopService:
        def __init__(self, workspace, *args, **kwargs):
            self.workspace = workspace

        async def run(self, **kwargs):
            run_kwargs.append(kwargs)
            return _passing_outcome(modified_files=["backend/example.py"])

    class FakePRService:
        def build_pr_body(self, **kwargs):
            return "updated body"

        async def generate_pr_body(self, **kwargs):
            return kwargs.get("fallback_body") or "ai body"

        async def generate_commit_message(self, **kwargs):
            return kwargs.get("fallback_message") or "feat(agent): auto"

        async def commit_and_push(self, **kwargs):
            pushed.append(kwargs)
            return "new-sha"

        async def create_pull_request(self, **kwargs):
            created_prs.append(kwargs)
            raise AssertionError("external review iteration must not create a new PR")

        async def update_pull_request_body(self, **kwargs):
            updated_bodies.append(kwargs)

    async def fake_load_task(self, task_id):
        return task

    async def fake_update_task(self, task_id, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(task, key, value)

    async def fake_save_iteration(self, **kwargs):
        saved_iterations.append(kwargs)

    async def fake_load_feedback(self, task_id, review_id):
        return "Sakura review feedback"

    async def fake_submit_review(pr_info):
        submitted_reviews.append(pr_info)
        raise AssertionError("direct Sakura review submission should not be called")

    monkeypatch.setattr(worker_module, "load_skills_context", _fake_skills_context)
    monkeypatch.setattr(worker_module, "load_sakura_memory", _fake_sakura_memory)
    monkeypatch.setattr(worker_module, "get_settings", _fake_settings)
    monkeypatch.setattr(
        worker_module, "AgentTeamGitWorkspaceService", FakeGitWorkspaceService
    )
    monkeypatch.setattr(worker_module, "IterationLoopService", FakeLoopService)
    monkeypatch.setattr(worker_module, "AgentTeamPRService", FakePRService)
    monkeypatch.setattr(worker_module.AgentTeamWorker, "_load_task", fake_load_task)
    monkeypatch.setattr(worker_module.AgentTeamWorker, "_update_task", fake_update_task)
    monkeypatch.setattr(
        worker_module.AgentTeamWorker, "_save_iteration", fake_save_iteration
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker,
        "_expire_pending_prompts",
        _fake_expire_pending_prompts,
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker,
        "_load_sakura_pr_review_feedback",
        fake_load_feedback,
    )
    monkeypatch.setattr(
        worker_module.AgentTeamWorker, "_get_config", _fake_max_files_config
    )
    monkeypatch.setattr(
        "backend.workers.review_worker.submit_review_task",
        fake_submit_review,
    )

    worker = worker_module.AgentTeamWorker()
    await worker.process_external_review_iteration(task.id, review_id=555)

    assert resumes == [
        ("owner", "repo", str(tmp_path), "feature/agent-101", "develop", "base-sha")
    ]
    assert run_kwargs and run_kwargs[0]["initial_feedback"] == "Sakura review feedback"
    assert "max_iterations" not in run_kwargs[0]
    assert saved_iterations
    assert pushed and pushed[0]["branch_name"] == "feature/agent-101"
    assert pushed[0]["repo_owner"] == "owner"
    assert pushed[0]["repo_name"] == "repo"
    assert updated_bodies and updated_bodies[0]["pr_number"] == 42
    assert created_prs == []
    assert submitted_reviews == []

    final_update = updates[-1]
    assert final_update["status"] == AgentTeamTaskStatus.EXTERNAL_REVIEWING.value
    assert final_update["current_phase"] == "external_reviewing"
    assert final_update["pr_head_sha"] == "new-sha"
    assert final_update["prompt_tokens"] == 110
    assert final_update["completion_tokens"] == 55
    assert final_update["error_message"] is None
    assert final_update["failed_phase"] is None
    assert final_update["failed_role"] is None
    assert final_update["rate_limit_reset_at"] is None
