"""Issue Worker cancellation and observability cleanup tests."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issue_service import IssueService
from backend.workers import issue_worker as issue_worker_module
from backend.workers.issue_worker import IssueWorker


@pytest.mark.asyncio
async def test_issue_cancel_registry_is_per_task_and_repeated_cancel_is_safe():
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    task_key = "owner/repo#7"

    first = worker._register_task(task_key, "task-1")
    second = worker._register_task(task_key, "task-2")

    assert worker._cancel_events == {
        task_key: {"task-1": first, "task-2": second}
    }
    assert await worker.cancel_task(task_key) is True
    assert first.is_set() and second.is_set()
    assert await worker.cancel_task(task_key) is False

    worker._unregister_task(task_key, "task-1")
    assert worker._cancel_events[task_key] == {"task-2": second}
    worker._unregister_task(task_key, "task-2")
    assert task_key not in worker._cancel_events

    fresh = worker._register_task(task_key, "task-3")
    assert fresh is not first
    assert not fresh.is_set()


@pytest.mark.asyncio
async def test_duplicate_cancel_waits_for_first_cancellation_to_finish():
    """A duplicate lifecycle event must await an already-cancelling worker."""
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    task_key = "owner/repo#7"
    worker._register_task(task_key, "task-1")
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def _slow_cancelled_worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            raise

    task = asyncio.create_task(_slow_cancelled_worker())
    worker._bind_task_handle(task_key, "task-1", task)

    first_cancel = asyncio.create_task(worker.cancel_task(task_key))
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)

    second_cancel = asyncio.create_task(worker.cancel_task(task_key))
    await asyncio.sleep(0)
    assert not second_cancel.done()

    release.set()
    assert await first_cancel is True
    assert await second_cancel is False
    assert task.cancelled()


@pytest.mark.asyncio
async def test_cancel_request_cancellation_still_drains_worker():
    """Cancelling the first webhook request must not abandon its worker."""
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    task_key = "owner/repo#7"
    worker._register_task(task_key, "task-1")
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def _slow_cancelled_worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            raise

    task = asyncio.create_task(_slow_cancelled_worker())
    worker._bind_task_handle(task_key, "task-1", task)
    cancel_request = asyncio.create_task(worker.cancel_task(task_key))
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)

    cancel_request.cancel()
    await asyncio.sleep(0)
    assert not cancel_request.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancel_request
    assert task.cancelled()
    assert worker._cancel_events == {}


@pytest.mark.asyncio
async def test_cancel_waits_for_thread_backed_external_write():
    """Cancellation must wait until an already-started thread write returns."""
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    worker._task_external_writes = {}
    task_key = "owner/repo#7"
    cancel_event = worker._register_task(task_key, "task-1")
    write_started = threading.Event()
    release_write = threading.Event()

    def _blocking_write():
        write_started.set()
        release_write.wait(timeout=2)
        return "written"

    async def _run_write():
        return await worker._run_external_write(
            task_key,
            "task-1",
            cancel_event,
            lambda: asyncio.to_thread(_blocking_write),
        )

    task = asyncio.create_task(_run_write())
    worker._bind_task_handle(task_key, "task-1", task)
    await asyncio.wait_for(asyncio.to_thread(write_started.wait), timeout=1)

    cancel_call = asyncio.create_task(worker.cancel_task(task_key))
    await asyncio.sleep(0)
    assert not cancel_call.done()

    release_write.set()
    assert await cancel_call is True
    assert task.cancelled()


@pytest.mark.asyncio
async def test_labels_checkpoint_blocks_next_mutation_after_cancellation(monkeypatch):
    """A cancellation after one label write must prevent the next write."""
    import backend.services.issue_service as issue_service_module
    import backend.services.label_service as label_service_module

    first_write_started = threading.Event()
    release_first_write = threading.Event()
    applied: list[str] = []

    class _GithubApp:
        def add_labels_to_issue(
            self, _owner, _repo, _number, labels: list[str]
        ) -> bool:
            applied.append(labels[0])
            if labels == ["first"]:
                first_write_started.set()
                release_first_write.wait(timeout=2)
            return True

    class _LabelService:
        DEFAULT_LABELS = {}

        async def get_repo_labels(self, _owner, _repo):
            return {
                "first": {"color": "000000", "description": ""},
                "second": {"color": "000000", "description": ""},
            }

    class _Recommendation:
        def get_recommendation_settings(self):
            return {
                "enabled": True,
                "confidence_threshold": 0.5,
                "auto_create": False,
            }

    monkeypatch.setattr(issue_service_module, "get_label_config", _Recommendation)
    monkeypatch.setattr(label_service_module, "label_service", _LabelService())

    service = IssueService.__new__(IssueService)
    service.github_app = _GithubApp()
    cancel_event = asyncio.Event()

    def checkpoint() -> None:
        if cancel_event.is_set():
            raise ReviewCancelledError("Issue 分析已被取消")

    operation = asyncio.create_task(
        service.apply_suggested_labels(
            "owner",
            "repo",
            7,
            [
                {"name": "first", "confidence": 0.9},
                {"name": "second", "confidence": 0.9},
            ],
            db=None,
            cancellation_checkpoint=checkpoint,
        )
    )
    await asyncio.wait_for(asyncio.to_thread(first_write_started.wait), timeout=1)

    cancel_event.set()
    release_first_write.set()
    with pytest.raises(ReviewCancelledError):
        await operation

    assert applied == ["first"]


@pytest.mark.asyncio
async def test_assignee_checkpoint_after_collaborator_read_skips_mutation(monkeypatch):
    """Cancellation during collaborator discovery must not add an assignee."""
    import backend.services.issue_service as issue_service_module

    collaborators_started = threading.Event()
    release_collaborators = threading.Event()
    assigned: list[list[str]] = []

    class _GithubApp:
        def get_repo_collaborators(self, _owner, _repo):
            collaborators_started.set()
            release_collaborators.wait(timeout=2)
            return ["alice"]

        def add_assignees_to_issue(self, _owner, _repo, _number, assignees):
            assigned.append(assignees)
            return True

    async def dynamic_config(name: str):
        return {
            "issue_assignee_confidence_threshold": 0.5,
            "issue_auto_assign_max": 2,
        }[name]

    monkeypatch.setattr(issue_service_module, "get_dynamic_config", dynamic_config)

    service = IssueService.__new__(IssueService)
    service.github_app = _GithubApp()
    cancel_event = asyncio.Event()

    def checkpoint() -> None:
        if cancel_event.is_set():
            raise ReviewCancelledError("Issue 分析已被取消")

    operation = asyncio.create_task(
        service.apply_suggested_assignees(
            "owner",
            "repo",
            7,
            [{"username": "alice", "confidence": 0.9}],
            cancellation_checkpoint=checkpoint,
        )
    )
    await asyncio.wait_for(asyncio.to_thread(collaborators_started.wait), timeout=1)

    cancel_event.set()
    release_collaborators.set()
    with pytest.raises(ReviewCancelledError):
        await operation

    assert assigned == []


@pytest.mark.asyncio
async def test_cancel_waits_for_observability_admission_and_finishes_bound_execution():
    """Admission persistence must not strand a queued work unit on cancel."""
    execution = SimpleNamespace(merged=False, finish=AsyncMock())
    admission_started = asyncio.Event()
    release_admission = asyncio.Event()

    async def _start_execution(**_kwargs):
        admission_started.set()
        await release_admission.wait()
        return execution

    integration = SimpleNamespace(
        admit_issue=AsyncMock(
            return_value=SimpleNamespace(session_id=1, trigger_id=2)
        ),
        start_execution=AsyncMock(side_effect=_start_execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.analyzer = SimpleNamespace(api_client=None)
    worker._background_tasks = set()

    task = asyncio.create_task(
        worker.process_issue_analysis(
            {
                "task_id": "issue-admission-cancelled",
                "repo_owner": "owner",
                "repo_name": "repo",
                "repo_full_name": "owner/repo",
                "issue_number": 7,
            }
        )
    )
    await asyncio.wait_for(admission_started.wait(), timeout=1)

    cancel_call = asyncio.create_task(worker.cancel_task("owner/repo#7"))
    await asyncio.sleep(0)
    assert not cancel_call.done()
    execution.finish.assert_not_awaited()

    release_admission.set()
    assert await cancel_call is True
    assert task.cancelled()
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)


def test_issue_task_key_supports_full_name_or_owner_and_repo():
    assert IssueWorker._make_task_key(
        {"repo_full_name": "owner/repo", "issue_number": 7}
    ) == "owner/repo#7"
    assert IssueWorker._make_task_key(
        {"repo_owner": "owner", "repo_name": "repo", "issue_number": 7}
    ) == "owner/repo#7"


@pytest.mark.asyncio
async def test_issue_submission_registers_before_coroutine_runs_and_cleans_own_event(
    monkeypatch,
):
    worker = IssueWorker.__new__(IssueWorker)
    worker._cancel_events = {}
    worker._background_tasks = set()
    release = asyncio.Event()
    started = asyncio.Event()
    observed = {}

    async def _run_issue_analysis(
        issue_info,
        *,
        deadline,
        cancel_event,
        task_id,
    ):
        observed["event"] = cancel_event
        started.set()
        await release.wait()
        return task_id

    worker._run_issue_analysis = _run_issue_analysis
    monkeypatch.setattr(issue_worker_module, "get_issue_worker", lambda: worker)
    monkeypatch.setattr(issue_worker_module, "ensure_background_admission", lambda _: None)
    monkeypatch.setattr(issue_worker_module, "register_background_task", lambda *_: None)
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    issue_info = {
        "repo_owner": "owner",
        "repo_name": "repo",
        "issue_number": 7,
    }
    task_id = await issue_worker_module.submit_issue_analysis_task(issue_info)
    task_key = "owner/repo#7"
    registered = worker._cancel_events[task_key][task_id]
    assert not started.is_set()

    assert await worker.cancel_task(task_key) is True
    assert registered.is_set()
    await asyncio.sleep(0)

    # The task handle is cancelled before its coroutine gets a chance to run;
    # this is required for lifecycle webhooks to avoid post-delete work.
    assert not started.is_set()
    assert task_key not in worker._cancel_events


@pytest.mark.asyncio
async def test_review_cancelled_error_marks_active_issue_cancelled_and_skips_save(
    monkeypatch,
):
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(
            return_value=SimpleNamespace(session_id=1, trigger_id=2)
        ),
        start_execution=AsyncMock(return_value=execution),
    )
    record = SimpleNamespace(
        id=11,
        status=issue_worker_module.IssueAnalysisStatus.ANALYZING.value,
        error_message=None,
    )

    class _Result:
        def scalar_one_or_none(self):
            return record

    class _Db:
        def add(self, value):
            value.id = record.id

        async def scalar(self, _query):
            return 0

        async def execute(self, _query):
            return _Result()

        async def commit(self):
            return None

        async def refresh(self, _value):
            return None

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    db = _Db()
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.github_app = SimpleNamespace(get_repo_client=lambda *_: None)
    worker.analyzer = SimpleNamespace(
        api_client=None,
        analyze_issue=AsyncMock(side_effect=ReviewCancelledError()),
    )
    worker._background_tasks = set()

    monkeypatch.setattr(issue_worker_module, "async_session", lambda: _Session())
    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )
    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        lambda: _ready_semaphore(),
    )
    save_result = AsyncMock()
    monkeypatch.setattr(issue_worker_module.issue_service, "save_analysis_result", save_result)

    async def _ready_semaphore():
        return asyncio.Semaphore(1)

    task_id = await worker.process_issue_analysis(
        {
            "task_id": "issue-cancelled",
            "repo_owner": "owner",
            "repo_name": "repo",
            "repo_full_name": "owner/repo",
            "issue_number": 7,
        }
    )

    assert task_id == "issue-cancelled"
    assert record.status == issue_worker_module.IssueAnalysisStatus.CANCELLED.value
    assert isinstance(record.error_message, str)
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
    save_result.assert_not_awaited()
    assert worker._cancel_events == {}


@pytest.mark.asyncio
async def test_issue_cancellation_while_waiting_for_semaphore_finishes_execution(
    monkeypatch,
):
    """取消排队中的 Issue Worker 也必须终结已启动的 observability execution。"""
    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
    )
    integration = SimpleNamespace(
        admit_issue=AsyncMock(
            return_value=SimpleNamespace(session_id=1, trigger_id=2)
        ),
        start_execution=AsyncMock(return_value=execution),
    )
    worker = IssueWorker.__new__(IssueWorker)
    worker.activity_integration = integration
    worker.analyzer = SimpleNamespace(api_client=None)

    monkeypatch.setattr(
        issue_worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )

    blocked_semaphore = asyncio.Semaphore(0)

    async def _get_blocked_semaphore():
        return blocked_semaphore

    monkeypatch.setattr(
        issue_worker_module,
        "_get_issue_semaphore",
        _get_blocked_semaphore,
    )

    task = asyncio.create_task(
        worker.process_issue_analysis(
            {
                "task_id": "issue-cancelled",
                "repo_owner": "owner",
                "repo_name": "repo",
                "repo_full_name": "owner/repo",
                "issue_number": 7,
            }
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    integration.start_execution.assert_awaited_once()
    execution.finish.assert_awaited_once_with("cancelled", error_message=None)
