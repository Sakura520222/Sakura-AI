"""Worker entry points must enter service capacity before Git or AI work."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.agent_team_models import AgentTeamTaskStatus
from backend.workers import agent_team_worker as agent_module


@pytest.fixture
def real_capacity(tmp_path, monkeypatch):
    from tests.test_service_execution_capacity import capacity_db

    runtime = capacity_db.__wrapped__(tmp_path)
    module, limiter, engine, path = next(runtime)
    monkeypatch.setattr(module, "service_execution_slot", limiter.slot)
    monkeypatch.setattr(agent_module, "service_execution_slot", limiter.slot)
    try:
        yield module, limiter, engine, path
    finally:
        runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("process_task", {}),
        ("process_task", {"resume": True}),
        ("process_external_review_iteration", {"review_id": 9}),
        ("process_human_followup_iteration", {}),
    ],
)
async def test_every_agent_entry_enters_capacity_before_workspace(
    monkeypatch, method, kwargs
):
    from backend.models import database

    monkeypatch.setattr(database, "async_session", None)
    admissions = []
    task_id = 840
    task = SimpleNamespace(
        id=task_id,
        status=AgentTeamTaskStatus.ITERATING.value,
        source_type="issue",
        repo_owner="owner",
        repo_name="repo",
        workspace_path="/test/workspace",
        branch_name="feature/test",
        base_branch="develop",
        base_commit_sha="base",
        source_issue_number=1,
        source_id="test",
        pr_number=3,
    )

    @asynccontextmanager
    async def slot(feature, cancel_event=None):
        assert cancel_event is agent_module._cancel_events[task_id]
        admissions.append(feature)
        yield

    # Before the regression fix this hook is unused by every Agent entry point.
    monkeypatch.setattr(agent_module, "service_execution_slot", slot, raising=False)
    worker = agent_module.AgentTeamWorker()

    async def load_task(_id):
        return task

    async def update(_id, **values):
        for key, value in values.items():
            setattr(task, key, value)

    async def skills():
        return "", {}, {}

    workspace_calls = []

    class Git:
        async def prepare_workspace(self, *args, **kwargs):
            workspace_calls.append("prepare")
            assert admissions == ["agent"]
            raise RuntimeError("test stop at external workspace boundary")

        async def resume_workspace(self, *args, **kwargs):
            workspace_calls.append("resume")
            assert admissions == ["agent"]
            raise RuntimeError("test stop at external workspace boundary")

    async def expire(_id):
        return None

    async def feedback(*args):
        return "review feedback"

    monkeypatch.setattr(worker, "_load_task", load_task)
    monkeypatch.setattr(worker, "_update_task", update)
    monkeypatch.setattr(worker, "_expire_pending_prompts_if_terminal", expire)
    monkeypatch.setattr(worker, "_load_sakura_pr_review_feedback", feedback)
    monkeypatch.setattr(agent_module, "load_skills_context", skills)
    monkeypatch.setattr(agent_module, "AgentTeamGitWorkspaceService", Git)
    agent_module._cancel_events.pop(task_id, None)
    try:
        assert await getattr(worker, method)(task_id, **kwargs) == task_id
        assert admissions == ["agent"]
        assert workspace_calls == [
            "resume" if kwargs or method != "process_task" else "prepare"
        ]
        assert task_id not in agent_module._cancel_events
    finally:
        agent_module._cancel_events.pop(task_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("process_task", {}),
        ("process_task", {"resume": True}),
        ("process_external_review_iteration", {"review_id": 9}),
        ("process_human_followup_iteration", {}),
    ],
)
async def test_agent_queue_cancellation_releases_registry_without_starting_work(
    real_capacity, method, kwargs
):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    module, limiter, engine, _ = real_capacity
    worker = agent_module.AgentTeamWorker()
    worker._load_task = AsyncMock(side_effect=AssertionError("queued work started"))
    worker._update_task = AsyncMock()
    worker._expire_pending_prompts_if_terminal = AsyncMock()
    task_id = 845
    # Remove only the billing decorator, retaining the actual worker capacity
    # wrapper. Financial queue admission has its own service integration tests.
    entry = getattr(type(worker), method).__wrapped__
    async with limiter.slot("agent"):
        queued = asyncio.create_task(entry(worker, task_id, **kwargs))
        await asyncio.sleep(0.02)
        assert task_id in agent_module._cancel_events
        worker._load_task.assert_not_awaited()
        agent_module.request_task_cancel(task_id)
        assert await asyncio.wait_for(queued, 1) == task_id
        worker._load_task.assert_not_awaited()
        worker._update_task.assert_awaited_once_with(
            task_id,
            status="cancelled",
            current_phase="cancelled",
            error_message="Agent execution cancelled",
        )
        assert task_id not in agent_module._cancel_events
        with Session(engine) as session:
            assert len(session.scalars(select(module.ServiceExecutionLease)).all()) == 1
    with Session(engine) as session:
        assert not session.scalars(select(module.ServiceExecutionLease)).all()


@pytest.mark.asyncio
async def test_agent_external_task_cancellation_preserves_asyncio_semantics(
    real_capacity,
):
    _, limiter, _, _ = real_capacity
    worker = agent_module.AgentTeamWorker()
    worker._load_task = AsyncMock()
    worker._update_task = AsyncMock()
    worker._expire_pending_prompts_if_terminal = AsyncMock()
    task_id = 846
    async with limiter.slot("agent"):
        queued = asyncio.create_task(
            type(worker).process_task.__wrapped__(worker, task_id)
        )
        await asyncio.sleep(0.02)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    worker._load_task.assert_not_awaited()
    assert worker._update_task.await_args.kwargs["status"] == "cancelled"
    assert task_id not in agent_module._cancel_events


@pytest.mark.asyncio
async def test_pr_waiting_cancellation_finishes_and_removes_event(
    real_capacity, monkeypatch
):
    from backend.workers import review_worker

    _, limiter, _, _ = real_capacity
    worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
    worker._cancel_events = {}
    worker._task_stages = {}
    worker.analyzer = SimpleNamespace(analyze_pr=AsyncMock())
    worker._cancel_and_cleanup = AsyncMock(return_value="cancelled-pr")
    finish = AsyncMock()
    monkeypatch.setattr(review_worker, "finish_billing_operation", finish)
    payload = {"repo_full_name": "owner/repo", "pr_number": 7}
    async with limiter.slot("pr_review"):
        queued = asyncio.create_task(
            type(worker).process_review_task.__wrapped__(worker, payload)
        )
        await asyncio.sleep(0.02)
        assert worker.is_task_active("owner/repo#7")
        assert worker.cancel_task("owner/repo#7")
        assert await asyncio.wait_for(queued, 1) == "cancelled-pr"
    finish.assert_awaited_once_with("cancelled")
    worker.analyzer.analyze_pr.assert_not_awaited()
    worker._cancel_and_cleanup.assert_awaited_once()
    assert not worker.is_task_active("owner/repo#7")


@pytest.mark.asyncio
async def test_issue_waiting_cancellation_finishes_execution_without_ai(real_capacity):
    from backend.workers import issue_worker

    _, limiter, _, _ = real_capacity
    worker = issue_worker.IssueWorker.__new__(issue_worker.IssueWorker)
    worker._cancel_events = {}
    worker._task_handles = {}
    worker.analyzer = SimpleNamespace(api_client=None, analyze=AsyncMock())
    execution = SimpleNamespace(merged=False, finish=AsyncMock())
    worker.activity_integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=1)),
        start_execution=AsyncMock(return_value=execution),
    )
    payload = {
        "repo_owner": "owner",
        "repo_name": "repo",
        "repo_full_name": "owner/repo",
        "issue_number": 7,
        "task_id": "queued-issue",
    }
    async with limiter.slot("issue_analysis"):
        queued = asyncio.create_task(
            type(worker).process_issue_analysis.__wrapped__(worker, payload)
        )
        await asyncio.sleep(0.02)
        assert await worker.cancel_task("owner/repo#7")
        assert queued.cancelled()
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
    worker.analyzer.analyze.assert_not_awaited()
    assert not worker._cancel_events
    assert not worker._task_handles


@pytest.mark.asyncio
async def test_reset_hooks_keep_one_shared_limiter():
    from backend.workers import issue_worker, review_worker

    pr_limiter = await review_worker._get_review_semaphore()
    issue_limiter = await issue_worker._get_issue_semaphore()
    review_worker.reset_review_semaphore()
    issue_worker.reset_issue_semaphore()
    assert await review_worker._get_review_semaphore() is pr_limiter
    assert await issue_worker._get_issue_semaphore() is issue_limiter
    assert pr_limiter.feature == "pr_review"
    assert issue_limiter.feature == "issue_analysis"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["analyzing", "completed", "cancelled"])
async def test_issue_lease_loss_converges_exact_record_and_preserves_terminal_rows(
    real_capacity, monkeypatch, previous
):
    from sqlalchemy.orm import Session

    from backend.models.database import IssueAnalysis
    from backend.workers import issue_worker
    from tests.test_billing_usage_attribution import DurableSQLSession

    module, limiter, engine, _ = real_capacity
    limiter.lease_seconds = 0.12
    limiter.heartbeat_seconds = 0.025
    with Session(engine) as session:
        session.add_all(
            [
                IssueAnalysis(
                    id=901,
                    repo_owner="owner",
                    repo_name="repo",
                    issue_number=7,
                    status=previous,
                ),
                IssueAnalysis(
                    id=902,
                    repo_owner="owner",
                    repo_name="repo",
                    issue_number=7,
                    status="analyzing",
                ),
            ]
        )
        session.commit()
    monkeypatch.setattr(
        issue_worker,
        "async_session",
        lambda: DurableSQLSession(Session(engine, expire_on_commit=False)),
    )

    async def lost(_lease):
        return False

    monkeypatch.setattr(limiter, "renew", lost)
    worker = issue_worker.IssueWorker.__new__(issue_worker.IssueWorker)
    worker._cancel_events = {}
    execution = SimpleNamespace(finish=AsyncMock())

    async def running(payload, *, deadline, cancel_event, task_id):
        key = worker._make_task_key(payload)
        worker._bind_analysis_record(
            key, task_id, SimpleNamespace(id=901, analysis_version=1)
        )
        worker._bind_execution(key, task_id, execution)
        async with limiter.slot("issue_analysis", cancel_event):
            await asyncio.sleep(1)

    monkeypatch.setattr(worker, "_run_issue_analysis", running)
    payload = {
        "repo_full_name": "owner/repo",
        "issue_number": 7,
        "task_id": "lease-loss",
    }
    with pytest.raises(module.ServiceExecutionLeaseLost):
        await type(worker).process_issue_analysis.__wrapped__(worker, payload)
    with Session(engine) as session:
        assert session.get(IssueAnalysis, 901).status == (
            "failed" if previous == "analyzing" else previous
        )
        assert session.get(IssueAnalysis, 902).status == "analyzing"
    execution.finish.assert_awaited_once_with(
        "failed" if previous == "analyzing" else previous,
        error_message="Service execution capacity lease lost",
    )
    assert not worker._cancel_events


@pytest.mark.asyncio
async def test_pr_lease_loss_is_failed_before_financial_terminal_write(
    real_capacity, monkeypatch
):
    from backend.workers import review_worker

    module, limiter, _, _ = real_capacity
    limiter.lease_seconds = 0.12
    limiter.heartbeat_seconds = 0.025

    async def lost(_lease):
        return False

    async def analyze(_payload):
        await asyncio.sleep(1)

    monkeypatch.setattr(limiter, "renew", lost)
    worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
    worker._cancel_events = {}
    worker._task_stages = {}
    worker.analyzer = SimpleNamespace(analyze_pr=analyze)
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    execution = SimpleNamespace(merged=False, finish=AsyncMock())
    worker.activity_integration = SimpleNamespace(
        admit=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=1)),
        start_execution=AsyncMock(return_value=execution),
    )
    finish = AsyncMock()
    monkeypatch.setattr(review_worker, "finish_billing_operation", finish)
    payload = {
        "repo_full_name": "owner/repo",
        "pr_number": 7,
        "repo_owner": "owner",
        "repo_name": "repo",
    }
    with pytest.raises(module.ServiceExecutionLeaseLost):
        await type(worker).process_review_task.__wrapped__(worker, payload)
    finish.assert_awaited_once_with("failed")
    execution.finish.assert_awaited_once_with("failed", error_message=None)
    assert not worker._cancel_events


@pytest.mark.asyncio
async def test_agent_lease_loss_marks_failure_and_cleans_event(
    real_capacity, monkeypatch
):
    module, limiter, _, _ = real_capacity
    limiter.lease_seconds = 0.12
    limiter.heartbeat_seconds = 0.025

    async def lost(_lease):
        return False

    async def load_task(_task_id):
        await asyncio.sleep(1)

    monkeypatch.setattr(limiter, "renew", lost)
    worker = agent_module.AgentTeamWorker()
    worker._load_task = load_task
    worker._update_task = AsyncMock()
    worker._expire_pending_prompts_if_terminal = AsyncMock()
    with pytest.raises(module.ServiceExecutionLeaseLost):
        await type(worker).process_task.__wrapped__(worker, 848)
    assert worker._update_task.await_args.kwargs["status"] == "failed"
    assert 848 not in agent_module._cancel_events


@pytest.mark.asyncio
async def test_agent_capacity_admission_failure_never_starts_work_and_remains_error(
    monkeypatch,
):
    from backend.services.service_execution_capacity import (
        ServiceExecutionCapacityError,
    )

    @asynccontextmanager
    async def unavailable(feature, cancel_event):
        raise ServiceExecutionCapacityError("test database unavailable")
        yield

    monkeypatch.setattr(agent_module, "service_execution_slot", unavailable)
    worker = agent_module.AgentTeamWorker()
    worker._load_task = AsyncMock()
    worker._update_task = AsyncMock()
    worker._expire_pending_prompts_if_terminal = AsyncMock()
    with pytest.raises(
        ServiceExecutionCapacityError, match="test database unavailable"
    ):
        await type(worker).process_task.__wrapped__(worker, 849)
    worker._load_task.assert_not_awaited()
    assert worker._update_task.await_args.kwargs["status"] == "failed"
    assert 849 not in agent_module._cancel_events
