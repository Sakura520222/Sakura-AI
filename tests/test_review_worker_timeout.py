"""Review worker dynamic timeout coverage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from builtins import BaseExceptionGroup

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.config import get_settings
from backend.services.comment_service import CommentService
from backend.services.activity_observability import integration_service as _activity_integration_module
from backend.services.activity_observability.integration_service import (
    ObservedExecutionBundle,
)
from backend.workers import review_worker
from backend.workers.review_worker import (
    ReviewDecision,
    ReviewWorker,
    _run_review_task_with_timeout,
    submit_review_task,
)


class _NoOpToolService:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def load_conversation_messages(self, _thread_id):
        return list(self.messages)

    async def append_conversation_message(self, **_kwargs):
        return SimpleNamespace(id=1)

    async def mark_tool_execution_completed(self, *_args, **_kwargs):
        return None

    async def mark_tool_execution_running(self, *_args, **_kwargs):
        return None


class _NoOpExecutionBundle:
    """Test double for ObservedExecutionBundle used by non-observability tests."""

    def __init__(self):
        self.invocation_context = None
        self.observer = None
        self.publication_coordinator = None
        self.session = SimpleNamespace(id=0)
        self.work_unit = SimpleNamespace(id=0)
        self.thread = None
        self.tool_service = _NoOpToolService()

    async def finish(self, _status, *, _error_message=None, **_kwargs):
        return None


class _NoOpActivityIntegration:
    """Test double that neutralizes observability admission for legacy tests."""

    async def admit(self, *args, **kwargs):
        return SimpleNamespace(session_id=0, trigger_id=0, duplicate=False)

    async def start_execution(self, *args, **kwargs):
        return _NoOpExecutionBundle()

    async def start_scan_execution(self, *args, **kwargs):
        return _NoOpExecutionBundle()


@pytest.fixture
def stub_review_worker_dependencies(monkeypatch):
    monkeypatch.setattr(review_worker, "GitHubAppClient", lambda: object())
    monkeypatch.setattr(review_worker, "PRAnalyzer", lambda: object())
    monkeypatch.setattr(review_worker, "AIReviewer", lambda: object())
    monkeypatch.setattr(review_worker, "CommentService", lambda: object())
    monkeypatch.setattr(
        _activity_integration_module, "ActivityIntegrationService", _NoOpActivityIntegration
    )
    monkeypatch.setattr(review_worker, "_worker_instance", None)
    yield
    monkeypatch.setattr(review_worker, "_worker_instance", None)


class _RecordingToolService:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def load_conversation_messages(self, _thread_id):
        return list(self.messages)

    async def append_conversation_message(self, **_kwargs):
        return SimpleNamespace(id=1)

    async def mark_tool_execution_completed(self, *_args, **_kwargs):
        return None

    async def mark_tool_execution_running(self, *_args, **_kwargs):
        return None


class _RecordingExecutionBundle:
    def __init__(self, messages=None):
        self.invocation_context = None
        self.observer = None
        self.publication_coordinator = None
        self.session = SimpleNamespace(id=0)
        self.work_unit = SimpleNamespace(id=0)
        self.thread = SimpleNamespace(id=0)
        self.tool_service = _RecordingToolService(messages)
        self.finish_calls = []

    async def finish(self, status, *, error_message=None):
        self.finish_calls.append((status, error_message))


class _RecordingActivityIntegration:
    def __init__(self, execution=None):
        self.execution = execution or _RecordingExecutionBundle()
        self.admitted = asyncio.Event()

    async def admit(self, *args, **kwargs):
        self.admitted.set()
        return SimpleNamespace(session_id=0, trigger_id=0, duplicate=False)

    async def start_execution(self, *args, **kwargs):
        return self.execution


@pytest.mark.asyncio
async def test_create_review_record_persists_global_id_and_repository_number(
    monkeypatch,
):
    stored = []

    class RecordingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def add(self, row):
            stored.append(row)

        async def commit(self):
            return None

        async def refresh(self, row):
            row.id = 7

    monkeypatch.setattr(
        review_worker,
        "get_async_session",
        lambda: RecordingSession,
    )
    worker = ReviewWorker.__new__(ReviewWorker)
    analysis = SimpleNamespace(
        pr_id=987654321,
        pr_number=15,
        total_files=1,
        total_changes=3,
        code_file_count=1,
        strategy="quick",
    )
    pr_info = {
        "repo_name": "repo",
        "repo_owner": "owner",
        "author": "alice",
        "title": "Fix",
        "branch": "feature/fix",
        "head_sha": "a" * 40,
    }

    review_id = await worker._create_review_record(analysis, pr_info, "task")

    assert review_id == 7
    assert len(stored) == 1
    assert stored[0].pr_id == 987654321
    assert stored[0].pr_number == 15


@pytest.mark.asyncio
async def test_timeout_finishes_started_execution_as_cancelled(monkeypatch):
    """wait_for 超时后，已启动的 execution 必须且只能取消收尾一次。"""
    settings = get_settings()
    old_timeout = settings.review_timeout_seconds
    execution = _RecordingExecutionBundle()
    integration = _RecordingActivityIntegration(execution)

    class BlockingAnalyzer:
        async def analyze_pr(self, _pr_info):
            await asyncio.Event().wait()

    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = integration
    worker.analyzer = BlockingAnalyzer()
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker._cancel_events = {}
    worker._save_error_record = AsyncMock()
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )
    settings.review_timeout_seconds = 0.01

    pr_info = {
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_number": 1,
        "action": "opened",
    }
    try:
        with pytest.raises(RuntimeError, match="审查任务超时"):
            await _run_review_task_with_timeout(worker, pr_info, "owner/repo#1")
    finally:
        settings.review_timeout_seconds = old_timeout

    assert execution.finish_calls == [("cancelled", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_check_count", [2, 3, 4])
async def test_early_cancel_finishes_execution_once(
    monkeypatch, cancel_check_count
):
    """三个 execution 创建后的取消检查点都必须只做一次 cancelled 收尾。"""
    execution = _RecordingExecutionBundle()
    integration = _RecordingActivityIntegration(execution)

    class Analyzer:
        async def analyze_pr(self, pr_info):
            return SimpleNamespace(
                pr_id=pr_info["pr_id"],
                pr_number=pr_info["pr_number"],
                repo_full_name=pr_info["repo_full_name"],
                total_files=0,
                total_changes=0,
                code_file_count=0,
                code_files=[],
                strategy="standard",
                should_skip=False,
                is_incremental=False,
                changed_lines_map={},
                hunk_boundaries={},
            )

        async def prepare_review_context(self, _analysis, _pr_info):
            return {"files": []}

    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = integration
    worker.analyzer = Analyzer()
    worker._cancel_events = {}
    worker.comment_service = SimpleNamespace(
        create_placeholder_comment=AsyncMock(return_value=SimpleNamespace(id=1)),
        delete_placeholder_comment=AsyncMock(),
    )
    worker.check_run_service = SimpleNamespace(
        report_queued=AsyncMock(),
        report_stage_progress=AsyncMock(),
        cancel_active_runs_by_sha=AsyncMock(),
    )
    worker.ai_reviewer = SimpleNamespace(_refresh_ai_clients=lambda: None)
    worker.github_app = SimpleNamespace(
        get_repo_client=lambda *_args: SimpleNamespace(
            get_repo=lambda *_args: SimpleNamespace(
                get_pull=lambda *_args: SimpleNamespace()
            )
        )
    )
    worker._create_review_record = AsyncMock(return_value=1)
    worker._update_review_status = AsyncMock()
    worker._log_activity = AsyncMock()
    worker._save_error_record = AsyncMock()
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )
    monkeypatch.setattr(review_worker, "get_user_dynamic_config", AsyncMock(return_value=None))
    monkeypatch.setattr(review_worker, "_get_label_rec_setting", lambda *_args: False)
    monkeypatch.setattr(review_worker.settings, "auto_index_pr_changes", False)
    monkeypatch.setattr(review_worker.settings, "enable_code_index", False)
    monkeypatch.setattr(review_worker.settings, "enable_rag", False)
    monkeypatch.setattr(review_worker.settings, "enable_pr_summary", False)
    monkeypatch.setattr(review_worker.settings, "enable_pr_dependency_graph", False)
    monkeypatch.setattr(review_worker.settings, "sakura_memory_enabled", False)
    monkeypatch.setattr(review_worker.settings, "sakura_reflection_enabled", False)
    monkeypatch.setattr(review_worker.settings, "enable_pr_issue_linking", False)
    monkeypatch.setattr(review_worker.settings, "enable_semantic_issue_linking", False)
    checks = 0

    def check_cancelled(_task_key):
        nonlocal checks
        checks += 1
        return checks == cancel_check_count

    worker._check_cancelled = check_cancelled
    pr_info = {
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_id": 1,
        "pr_number": 1,
        "action": "opened",
    }

    result = await worker.process_review_task(pr_info)

    assert result
    assert execution.finish_calls == [("cancelled", None)]


@pytest.mark.asyncio
async def test_skip_path_finishes_execution_as_completed_once(monkeypatch):
    """无需审查的分析结果仍应完成已创建的 observability execution。"""
    execution = _RecordingExecutionBundle()
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = _RecordingActivityIntegration(execution)
    worker._cancel_events = {}
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker.analyzer = SimpleNamespace(
        analyze_pr=AsyncMock(
            return_value=SimpleNamespace(should_skip=True, skip_reason="no changes")
        )
    )
    worker._save_skip_record = AsyncMock()
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )
    monkeypatch.setattr(review_worker, "get_user_dynamic_config", AsyncMock(return_value=None))

    pr_info = {
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_id": 1,
        "pr_number": 1,
        "action": "opened",
    }

    result = await worker.process_review_task(pr_info)

    assert result
    assert execution.finish_calls == [("completed", None)]


@pytest.mark.asyncio
async def test_cancelled_error_finishes_execution_and_finalizes_check_run(monkeypatch):
    """CancelledError 保留原有 Check Run 故障收尾，并完成 execution。"""
    execution = _RecordingExecutionBundle()
    integration = _RecordingActivityIntegration(execution)
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = integration
    worker._cancel_events = {}
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker.check_run_service = SimpleNamespace(finalize_review_run=AsyncMock())
    worker._update_review_status = AsyncMock()
    worker._persist_error_reference = AsyncMock()
    worker._unregister_task = lambda _task_key: None
    monkeypatch.setattr(review_worker, "_get_review_semaphore", lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)))

    class CancellingAnalyzer:
        async def analyze_pr(self, _pr_info):
            raise asyncio.CancelledError

    worker.analyzer = CancellingAnalyzer()
    pr_info = {
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_id": 1,
        "pr_number": 1,
        "action": "opened",
        "head_sha": "head-sha",
    }

    with pytest.raises(asyncio.CancelledError):
        await worker.process_review_task(pr_info)

    assert execution.finish_calls == [("cancelled", None)]
    worker.check_run_service.finalize_review_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_cleanup_survives_execution_finish_failure_and_retries(
    monkeypatch,
):
    """辅助链路 finish 失败不得阻断取消业务收尾，且 finally 应重试一次。"""
    execution = _RecordingExecutionBundle()
    finish_attempts = 0

    async def flaky_finish(status, *, error_message=None):
        nonlocal finish_attempts
        finish_attempts += 1
        execution.finish_calls.append((status, error_message))
        if finish_attempts == 1:
            raise RuntimeError("observability database unavailable")

    execution.finish = flaky_finish
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = _RecordingActivityIntegration(execution)
    worker._cancel_events = {}
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker.analyzer = SimpleNamespace(
        analyze_pr=AsyncMock(
            return_value=SimpleNamespace(
                should_skip=False,
                pr_id=1,
                pr_number=1,
                repo_full_name="owner/repo",
                total_files=0,
                total_changes=0,
                code_file_count=0,
                code_files=[],
                strategy="standard",
                is_incremental=False,
                changed_lines_map={},
                hunk_boundaries={},
            )
        )
    )
    worker.comment_service = SimpleNamespace(delete_placeholder_comment=AsyncMock())
    worker.check_run_service = SimpleNamespace(cancel_active_runs_by_sha=AsyncMock())
    worker._update_review_status = AsyncMock()
    worker._create_review_record = AsyncMock(return_value=1)
    worker._save_error_record = AsyncMock()
    worker._log_activity = AsyncMock()
    cancel_checks = 0

    def check_cancelled(_task_key):
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks == 2

    worker._check_cancelled = check_cancelled
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )

    result = await worker.process_review_task(
        {
            "repo_full_name": "owner/repo",
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 1,
            "action": "opened",
        }
    )

    assert result
    worker._update_review_status.assert_not_awaited()
    assert execution.finish_calls == [("cancelled", None), ("cancelled", None)]
    worker.comment_service.delete_placeholder_comment.assert_not_awaited()
@pytest.mark.asyncio
async def test_bundle_finish_releases_lease_after_work_unit_failure():
    """底层 bundle 在 WorkUnit 写入失败时仍尝试释放 lease，并保留异常。"""
    errors = []

    class FailingObservability:
        async def finish_work_unit(self, *_args, **_kwargs):
            errors.append("work_unit")
            raise RuntimeError("finish work unit failed")

    class ReleasingContext:
        async def release_lease(self, token, terminal_status=None):
            errors.append(("lease", token, terminal_status))

    bundle = SimpleNamespace(
        observability=FailingObservability(),
        context_service=ReleasingContext(),
        lease="lease-token",
        work_unit_id=1,
    )
    bundle.finish = ObservedExecutionBundle.finish.__get__(bundle)

    with pytest.raises(RuntimeError, match="finish work unit failed"):
        await bundle.finish("cancelled")

    assert errors == ["work_unit", ("lease", "lease-token", None)]


@pytest.mark.asyncio
async def test_bundle_finish_preserves_base_exception_group():
    """两个 finish 失败含 CancelledError 时必须保留 BaseExceptionGroup。"""

    class FailingObservability:
        async def finish_work_unit(self, *_args, **_kwargs):
            raise asyncio.CancelledError("work unit cancelled")

    class FailingContext:
        async def release_lease(self, *_args, **_kwargs):
            raise RuntimeError("lease release failed")

    bundle = SimpleNamespace(
        observability=FailingObservability(),
        context_service=FailingContext(),
        lease="lease-token",
        work_unit_id=1,
    )
    bundle.finish = ObservedExecutionBundle.finish.__get__(bundle)

    with pytest.raises(BaseExceptionGroup) as raised:
        await bundle.finish("cancelled")

    leaves = raised.value.exceptions
    assert any(isinstance(error, asyncio.CancelledError) for error in leaves)
    assert any(isinstance(error, RuntimeError) for error in leaves)


@pytest.mark.asyncio
async def test_cancelled_cleanup_failure_keeps_cancelled_target(monkeypatch):
    """ReviewCancelledError cleanup 失败后不得被 finally 改写为 failed。"""
    execution = _RecordingExecutionBundle()
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = _RecordingActivityIntegration(execution)
    worker._cancel_events = {}
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker.analyzer = SimpleNamespace(
        analyze_pr=AsyncMock(side_effect=ReviewCancelledError("provider cancelled"))
    )
    worker._cancel_and_cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    worker._unregister_task = lambda _task_key: None
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await worker.process_review_task(
            {
                "repo_full_name": "owner/repo",
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "action": "opened",
            }
        )

    assert execution.finish_calls == [("cancelled", None)]


@pytest.mark.asyncio
async def test_cancelled_error_finalize_failure_keeps_cancelled_target(monkeypatch):
    """CancelledError 收尾 I/O 失败时，execution 仍必须保持 cancelled。"""
    execution = _RecordingExecutionBundle()
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.activity_integration = _RecordingActivityIntegration(execution)
    worker._cancel_events = {}
    worker.ai_reviewer = SimpleNamespace(api_client=None)
    worker.check_run_service = SimpleNamespace(
        finalize_review_run=AsyncMock(side_effect=RuntimeError("finalize failed"))
    )
    worker._unregister_task = lambda _task_key: None
    monkeypatch.setattr(
        review_worker,
        "_get_review_semaphore",
        lambda: asyncio.sleep(0, result=asyncio.Semaphore(1)),
    )

    class CancellingAnalyzer:
        async def analyze_pr(self, _pr_info):
            raise asyncio.CancelledError("worker cancelled")

    worker.analyzer = CancellingAnalyzer()
    with pytest.raises(RuntimeError, match="finalize failed"):
        await worker.process_review_task(
            {
                "repo_full_name": "owner/repo",
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "action": "opened",
                "head_sha": "head-sha",
            }
        )

    assert execution.finish_calls == [("cancelled", None)]


class _TimeoutWorker:
    def __init__(self):
        self.cancelled_key = None
        self.saved_errors = []

    async def process_review_task(self, pr_info):
        await asyncio.sleep(10)
        return "done"

    def cancel_task(self, task_key):
        self.cancelled_key = task_key
        return True

    async def _save_error_record(self, pr_info, error, task_id):
        self.saved_errors.append((pr_info, error, task_id))


class _ReportingWorker:
    def __init__(self):
        self.cancelled_key = None
        self.saved_errors = []
        self.reporting_started = False

    async def process_review_task(self, _pr_info):
        self.reporting_started = True
        await asyncio.sleep(0.05)
        return "reported"

    def is_task_reporting(self, _task_key):
        return self.reporting_started

    def cancel_task(self, task_key):
        self.cancelled_key = task_key
        return True

    async def _save_error_record(self, pr_info, error, task_id):
        self.saved_errors.append((pr_info, error, task_id))


@pytest.mark.asyncio
async def test_review_timeout_stops_after_entering_reporting():
    """AI 结果落库后进入 reporting，原总预算不得取消发布收尾。"""
    settings = get_settings()
    old_value = settings.review_timeout_seconds
    try:
        settings.review_timeout_seconds = 0.01
        worker = _ReportingWorker()
        pr_info = {"repo_full_name": "owner/repo", "pr_number": 1}

        result = await _run_review_task_with_timeout(worker, pr_info, "owner/repo#1")

        assert result == "reported"
        assert worker.cancelled_key is None
        assert worker.saved_errors == []
    finally:
        settings.review_timeout_seconds = old_value


def _review_worker_for_normalization():
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.comment_service = CommentService()
    return worker


def test_normalize_review_result_filters_out_of_diff_inline_comment_but_preserves_issue():
    worker = _review_worker_for_normalization()
    analysis = SimpleNamespace(
        changed_lines_map={"backend/example.py": {335, 336}},
        hunk_boundaries={},
    )
    review_result = {
        "inline_comments": [
            {
                "file_path": "backend/example.py",
                "line_number": 59,
                "body": "**Executor join blocks heartbeat**\n\nAvoid blocking the executor.",
                "severity": "major",
            }
        ],
        "issues": {
            "critical": [],
            "major": ["Executor join blocks heartbeat"],
            "minor": [],
            "suggestions": [],
        },
    }

    normalized = worker._normalize_review_result_for_diff(
        review_result,
        analysis,
        "task1",
    )

    assert normalized["inline_comments"] == []
    assert normalized["review_body_inline_comments"] == review_result["inline_comments"]
    assert normalized["issues"]["major"] == ["Executor join blocks heartbeat"]


def test_normalize_review_result_preserves_valid_finding_and_issue():
    worker = _review_worker_for_normalization()
    analysis = SimpleNamespace(
        changed_lines_map={"backend/example.py": {335, 336}},
        hunk_boundaries={},
    )
    review_result = {
        "inline_comments": [
            {
                "file_path": "backend/example.py",
                "line_number": 335,
                "body": "**Current defect**\n\nThe changed code is incorrect.",
                "severity": "minor",
            }
        ],
        "issues": {
            "critical": [],
            "major": [],
            "minor": ["Current defect"],
            "suggestions": [],
        },
    }

    normalized = worker._normalize_review_result_for_diff(
        review_result,
        analysis,
        "task1",
    )

    assert normalized["inline_comments"][0]["line_number"] == 335
    assert normalized["issues"]["minor"] == ["Current defect"]


def test_normalize_review_result_validates_inline_comments_in_one_batch(monkeypatch):
    worker = _review_worker_for_normalization()
    analysis = SimpleNamespace(
        changed_lines_map={"backend/example.py": {335}},
        hunk_boundaries={},
    )
    review_result = {
        "inline_comments": [
            {
                "file_path": "backend/example.py",
                "line_number": 335,
                "body": "**Valid finding**\n\nKeep this comment.",
                "severity": "minor",
            },
            {
                "file_path": "backend/example.py",
                "line_number": 59,
                "body": "**Invalid finding**\n\nFilter this comment.",
                "severity": "suggestion",
            },
        ],
        "issues": {
            "critical": [],
            "major": [],
            "minor": ["Valid finding"],
            "suggestions": ["Invalid finding"],
        },
    }
    calls = []
    original_validate = worker.comment_service._validate_inline_comments

    def tracked_validate(comments, current_analysis):
        calls.append(list(comments))
        return original_validate(comments, current_analysis)

    monkeypatch.setattr(
        worker.comment_service,
        "_validate_inline_comments",
        tracked_validate,
    )

    normalized = worker._normalize_review_result_for_diff(
        review_result,
        analysis,
        "task1",
    )

    assert calls == [review_result["inline_comments"]]
    assert len(normalized["inline_comments"]) == 1
    assert normalized["issues"]["minor"] == ["Valid finding"]
    assert normalized["issues"]["suggestions"] == ["Invalid finding"]


@pytest.mark.asyncio
async def test_review_task_timeout_uses_dynamic_setting():
    settings = get_settings()
    old_value = settings.review_timeout_seconds
    try:
        settings.review_timeout_seconds = 0.01
        pr_info = {"repo_full_name": "owner/repo", "pr_number": 1}
        task_key = ReviewWorker._make_task_key(pr_info)
        worker = _TimeoutWorker()

        with pytest.raises(RuntimeError, match="审查任务超时"):
            await _run_review_task_with_timeout(worker, pr_info, task_key)

        assert worker.cancelled_key == task_key
        assert worker.saved_errors
        assert worker.saved_errors[0][1] == "审查任务超时（0.01秒）"
        assert worker.saved_errors[0][2] == "timeout"
    finally:
        settings.review_timeout_seconds = old_value


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_submit_review_task_registers_cancel_event(monkeypatch):
    created = []

    async def fake_runner(worker, pr_info, task_key):
        del worker, pr_info
        return task_key

    class FakeTask:
        def add_done_callback(self, callback):
            callback(self)

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return FakeTask()

    monkeypatch.setattr(review_worker, "_run_review_task_with_timeout", fake_runner)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    pr_info = {
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_number": 1,
    }
    task_key = await submit_review_task(pr_info)

    worker = review_worker.get_worker()
    assert task_key == "owner/repo#1"
    assert task_key in worker._cancel_events
    assert created


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_review_worker_bridge_exception_does_not_fail_review(monkeypatch):
    called = []

    async def fake_handle(self, review_id):
        del self
        called.append(review_id)
        raise RuntimeError("bridge down")

    monkeypatch.setattr(
        "backend.services.agent_team.pr_review_feedback.AgentTeamPRReviewFeedbackService.handle_review_completed_with_result",
        fake_handle,
    )

    worker = ReviewWorker()
    await worker._notify_agent_team_review_completed(123, "task1")
    assert called == [123]


def _review_worker_for_reflection_history():
    """构造一个仅满足反思历史摘要获取依赖的 ReviewWorker。"""
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.ai_reviewer = SimpleNamespace(
        api_client=object(),
    )
    return worker


class _RecordingHistoryService:
    """记录 fetch_history_summary 调用与返回值的假服务。"""

    def __init__(self, returns="HISTORY-SUMMARY"):
        self._returns = returns
        self.calls = []

    async def fetch_history_summary(self, **kwargs):
        self.calls.append(kwargs)
        return self._returns


@pytest.mark.asyncio
async def test_reflection_history_summary_fetched_when_incremental(monkeypatch):
    """增量审查时，反思历史摘要应调用 HistoryContextService.fetch_history_summary。"""
    worker = _review_worker_for_reflection_history()
    recording = _RecordingHistoryService()
    monkeypatch.setattr(
        "backend.services.history_context_service.HistoryContextService",
        lambda api_client, model: recording,
    )

    analysis = SimpleNamespace(is_incremental=True, pr_id=42)
    pr_info = {"repo_name": "repo", "repo_owner": "owner"}

    summary = await worker._fetch_reflection_history_summary(analysis, pr_info, "task1")

    assert summary == "HISTORY-SUMMARY"
    assert recording.calls == [
        {"pr_id": 42, "repo_name": "repo", "repo_owner": "owner"}
    ]


@pytest.mark.asyncio
async def test_reflection_history_summary_skipped_when_not_incremental(monkeypatch):
    """非增量审查时不应调用历史摘要服务，直接返回 None。"""
    worker = _review_worker_for_reflection_history()
    recording = _RecordingHistoryService()
    monkeypatch.setattr(
        "backend.services.history_context_service.HistoryContextService",
        lambda api_client, model: recording,
    )

    analysis = SimpleNamespace(is_incremental=False, pr_id=42)
    summary = await worker._fetch_reflection_history_summary(
        analysis, {"repo_name": "repo", "repo_owner": "owner"}, "task1"
    )

    assert summary is None
    assert recording.calls == []


@pytest.mark.asyncio
async def test_reflection_history_summary_returns_none_on_failure(monkeypatch):
    """历史摘要获取异常时应吞掉异常并返回 None，不影响反思。"""
    worker = _review_worker_for_reflection_history()

    class _FailingService:
        async def fetch_history_summary(self, **kwargs):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        "backend.services.history_context_service.HistoryContextService",
        lambda api_client, model: _FailingService(),
    )

    analysis = SimpleNamespace(is_incremental=True, pr_id=42)
    summary = await worker._fetch_reflection_history_summary(
        analysis, {"repo_name": "repo", "repo_owner": "owner"}, "task1"
    )

    assert summary is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_incremental_review_restores_messages_and_passes_pending_callback(
    monkeypatch,
):
    settings = get_settings()
    old_values = {
        "auto_index_pr_changes": settings.auto_index_pr_changes,
        "enable_code_index": settings.enable_code_index,
        "enable_rag": settings.enable_rag,
        "enable_pr_summary": settings.enable_pr_summary,
        "enable_pr_dependency_graph": settings.enable_pr_dependency_graph,
        "enable_ai_tools": getattr(settings, "enable_ai_tools", True),
        "sakura_memory_enabled": settings.sakura_memory_enabled,
        "sakura_reflection_enabled": settings.sakura_reflection_enabled,
        "enable_pr_issue_linking": getattr(
            settings,
            "enable_pr_issue_linking",
            False,
        ),
        "enable_semantic_issue_linking": getattr(
            settings,
            "enable_semantic_issue_linking",
            False,
        ),
    }
    settings.auto_index_pr_changes = False
    settings.enable_code_index = False
    settings.enable_rag = False
    settings.enable_pr_summary = False
    settings.enable_pr_dependency_graph = False
    settings.enable_ai_tools = True
    settings.sakura_memory_enabled = False
    settings.sakura_reflection_enabled = False
    settings.enable_pr_issue_linking = False
    settings.enable_semantic_issue_linking = False

    class FakeAnalyzer:
        async def analyze_pr(self, pr_info):
            return SimpleNamespace(
                pr_id=pr_info["pr_id"],
                pr_number=pr_info["pr_number"],
                repo_full_name=pr_info["repo_full_name"],
                total_files=1,
                total_changes=2,
                code_file_count=1,
                code_files=[],
                strategy="standard",
                should_skip=False,
                is_incremental=True,
                changed_lines_map={},
                hunk_boundaries={},
            )

        async def prepare_review_context(self, analysis, pr):
            return {"files": [], "analysis": analysis}

    class FakeCommentService:
        async def create_placeholder_comment(self, pr, strategy, output_language=None):
            return SimpleNamespace(id=1)

        async def delete_placeholder_comment(self, review_obj):
            return None

    class FakeGitHubApp:
        def get_repo_client(self, repo_owner, repo_name):
            return SimpleNamespace(
                get_repo=lambda repo_full_name: SimpleNamespace(
                    get_pull=lambda pr_number: SimpleNamespace(
                        base=SimpleNamespace(repo=SimpleNamespace())
                    )
                )
            )

    class FakeQueueService:
        async def prepare_pending_for_review(self, **kwargs):
            return SimpleNamespace(
                message={"role": "user", "content": "queued increment"},
                queue_ids=[1],
            )

        async def mark_consumed(self, *args, **kwargs):
            captured["marked_consumed"] = (args, kwargs)

    captured = {}

    class FakeAIReviewer:
        def _refresh_ai_clients(self):
            return None

        async def review_pr_with_tools(self, *args, **kwargs):
            captured["initial_messages"] = kwargs["initial_messages"]
            captured["pending_callback"] = kwargs["pending_user_message_callback"]
            captured["pending_message"] = await kwargs[
                "pending_user_message_callback"
            ]()
            await kwargs["event_callback"]("message", captured["pending_message"])
            return {
                "summary": "ok",
                "overall_score": 8,
                "verdict": "approve",
                "comments": [],
                "inline_comments": [],
                "token_usage": {},
            }

    async def fail_history_summary(*_args, **_kwargs):
        raise AssertionError("HistoryContextService should not be used")

    class FailingHistoryContextService:
        fetch_history_summary = fail_history_summary

    class FakeSakuraMemoryService:
        async def get_sakura_context(self, **kwargs):
            return None

    async def fake_dynamic_config(*_args, **_kwargs):
        return None

    async def fake_noop(*_args, **_kwargs):
        return None

    async def fake_create_review_record(*_args, **_kwargs):
        return 99

    async def fake_make_decision(*_args, **_kwargs):
        return ReviewDecision.APPROVE, "ok", None

    try:
        monkeypatch.setattr(review_worker, "GitHubAppClient", FakeGitHubApp)
        monkeypatch.setattr(review_worker, "PRAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(review_worker, "AIReviewer", FakeAIReviewer)
        monkeypatch.setattr(review_worker, "CommentService", FakeCommentService)
        monkeypatch.setattr(review_worker, "_get_label_rec_setting", lambda *_: False)
        monkeypatch.setattr(
            review_worker, "get_user_dynamic_config", fake_dynamic_config
        )
        monkeypatch.setattr(
            ReviewWorker,
            "_log_activity",
            staticmethod(fake_noop),
        )
        monkeypatch.setattr(
            "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
            FakeQueueService,
        )
        monkeypatch.setattr(
            "backend.services.history_context_service.HistoryContextService",
            FailingHistoryContextService,
        )
        monkeypatch.setattr(
            "backend.services.sakura_memory_service.get_sakura_memory_service",
            lambda: FakeSakuraMemoryService(),
        )

        worker = ReviewWorker()
        monkeypatch.setattr(worker, "_create_review_record", fake_create_review_record)
        monkeypatch.setattr(worker, "_update_review_status", fake_noop)
        monkeypatch.setattr(worker, "_save_review_results", fake_noop)
        monkeypatch.setattr(
            worker,
            "_make_and_submit_decision",
            fake_make_decision,
        )
        monkeypatch.setattr(worker, "_notify_agent_team_review_completed", fake_noop)
        monkeypatch.setattr(worker, "_send_review_complete_notification", fake_noop)

        pr_info = {
            "repo_owner": "owner",
            "repo_name": "repo",
            "repo_full_name": "owner/repo",
            "pr_id": 1001,
            "pr_number": 7,
            "author": "alice",
            "title": "PR",
            "branch": "feature",
            "user_id": 42,
            "action": "synchronize",
        }

        await worker.process_review_task(pr_info)

        assert captured["initial_messages"] in (None, [])
        assert captured["pending_callback"] is not None
        assert captured["pending_message"] == {
            "role": "user",
            "content": "queued increment",
        }
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_incremental_review_migrates_check_run_to_new_head(monkeypatch):
    """增量消费时 PR head 变化，check run 应迁移到新 head。

    回归 pr_log.log 场景：审查中收到新提交，PR head 推进到新 commit，但 check run
    仍绑定旧 commit，导致 PR Checks 面板（最新 commit）看不到 Sakura check。
    修复后 _pending_incremental_message 检测 head 变化，收尾旧 head（cancelled），
    在新 head 创建 check run，使审查完成 conclusion 体现在最新 commit 上。
    """
    settings = get_settings()
    old_values = {
        "auto_index_pr_changes": settings.auto_index_pr_changes,
        "enable_code_index": settings.enable_code_index,
        "enable_rag": settings.enable_rag,
        "enable_pr_summary": settings.enable_pr_summary,
        "enable_pr_dependency_graph": settings.enable_pr_dependency_graph,
        "enable_ai_tools": getattr(settings, "enable_ai_tools", True),
        "sakura_memory_enabled": settings.sakura_memory_enabled,
        "sakura_reflection_enabled": settings.sakura_reflection_enabled,
        "enable_pr_issue_linking": getattr(settings, "enable_pr_issue_linking", False),
        "enable_semantic_issue_linking": getattr(
            settings, "enable_semantic_issue_linking", False
        ),
    }
    settings.auto_index_pr_changes = False
    settings.enable_code_index = False
    settings.enable_rag = False
    settings.enable_pr_summary = False
    settings.enable_pr_dependency_graph = False
    settings.enable_ai_tools = True
    settings.sakura_memory_enabled = False
    settings.sakura_reflection_enabled = False
    settings.enable_pr_issue_linking = False
    settings.enable_semantic_issue_linking = False

    OLD_SHA = "old_sha_aaa"
    NEW_SHA = "new_sha_bbb"

    class FakeAnalyzer:
        async def analyze_pr(self, pr_info):
            return SimpleNamespace(
                pr_id=pr_info["pr_id"],
                pr_number=pr_info["pr_number"],
                repo_full_name=pr_info["repo_full_name"],
                total_files=1,
                total_changes=2,
                code_file_count=1,
                code_files=[],
                strategy="standard",
                should_skip=False,
                is_incremental=True,
                changed_lines_map={},
                hunk_boundaries={},
            )

        async def prepare_review_context(self, analysis, pr):
            return {"files": [], "analysis": analysis}

    class FakeCommentService:
        async def create_placeholder_comment(self, pr, strategy, output_language=None):
            return SimpleNamespace(id=1)

        async def delete_placeholder_comment(self, review_obj):
            return None

    class FakeGitHubApp:
        def get_repo_client(self, repo_owner, repo_name):
            return SimpleNamespace(
                get_repo=lambda rf: SimpleNamespace(
                    get_pull=lambda pn: SimpleNamespace(
                        base=SimpleNamespace(repo=SimpleNamespace())
                    )
                )
            )

    class FakeQueueService:
        async def prepare_pending_for_review(self, **kwargs):
            return SimpleNamespace(
                message={"role": "user", "content": "queued increment"},
                queue_ids=[1],
                head_sha=NEW_SHA,
            )

        async def mark_consumed(self, *a, **kw):
            return None

    class FakeAIReviewer:
        def _refresh_ai_clients(self):
            return None

        async def review_pr_with_tools(self, *args, **kwargs):
            # 触发 pending_callback → 增量消费 → check run 迁移
            await kwargs["pending_user_message_callback"]()
            return {
                "summary": "ok",
                "overall_score": 8,
                "verdict": "approve",
                "comments": [],
                "inline_comments": [],
                "token_usage": {},
            }

    async def fake_noop(*a, **kw):
        return None

    async def fake_dynamic_config(*a, **kw):
        return None

    async def fake_create_review_record(*a, **kw):
        return 99

    async def fake_make_decision(*a, **kw):
        return ReviewDecision.APPROVE, "ok", None

    async def fail_history(*a, **kw):
        raise AssertionError("history should not be used")

    class FailingHistory:
        fetch_history_summary = fail_history

    class FakeSakuraMemory:
        async def get_sakura_context(self, **kw):
            return None

    try:
        monkeypatch.setattr(review_worker, "GitHubAppClient", FakeGitHubApp)
        monkeypatch.setattr(review_worker, "PRAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(review_worker, "AIReviewer", FakeAIReviewer)
        monkeypatch.setattr(review_worker, "CommentService", FakeCommentService)
        monkeypatch.setattr(review_worker, "_get_label_rec_setting", lambda *_: False)
        monkeypatch.setattr(
            review_worker, "get_user_dynamic_config", fake_dynamic_config
        )
        monkeypatch.setattr(ReviewWorker, "_log_activity", staticmethod(fake_noop))
        monkeypatch.setattr(
            "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
            FakeQueueService,
        )
        monkeypatch.setattr(
            "backend.services.history_context_service.HistoryContextService",
            FailingHistory,
        )
        monkeypatch.setattr(
            "backend.services.sakura_memory_service.get_sakura_memory_service",
            lambda: FakeSakuraMemory(),
        )

        worker = ReviewWorker()
        worker.check_run_service = SimpleNamespace(
            report_queued=AsyncMock(),
            report_progress=AsyncMock(),
            report_stage_progress=AsyncMock(),
            report_completed=AsyncMock(),
            report_failed=AsyncMock(),
            report_cancelled=AsyncMock(),
            report_skipped=AsyncMock(),
            report_analysis_snapshot=AsyncMock(),
            report_findings_snapshot=AsyncMock(),
            finalize_analysis=AsyncMock(),
            finalize_findings=AsyncMock(),
            finalize_review_run=AsyncMock(),
            cancel_active_runs_by_sha=AsyncMock(),
            get_cached_check_run_id=AsyncMock(return_value=None),
        )
        monkeypatch.setattr(worker, "_create_review_record", fake_create_review_record)
        monkeypatch.setattr(worker, "_update_review_status", fake_noop)
        monkeypatch.setattr(worker, "_save_review_results", fake_noop)
        monkeypatch.setattr(worker, "_make_and_submit_decision", fake_make_decision)
        monkeypatch.setattr(worker, "_notify_agent_team_review_completed", fake_noop)
        monkeypatch.setattr(worker, "_send_review_complete_notification", fake_noop)

        pr_info = {
            "repo_owner": "owner",
            "repo_name": "repo",
            "repo_full_name": "owner/repo",
            "pr_id": 1001,
            "pr_number": 7,
            "author": "alice",
            "title": "PR",
            "branch": "feature",
            "user_id": 42,
            "action": "synchronize",
            "head_sha": OLD_SHA,
        }

        await worker.process_review_task(pr_info)

        # 旧 head 收尾为 cancelled（增量取代）：cancel_active_runs_by_sha(owner, repo, sha)
        worker.check_run_service.cancel_active_runs_by_sha.assert_awaited()
        assert (
            worker.check_run_service.cancel_active_runs_by_sha.call_args.args[2]
            == OLD_SHA
        )

        # 新 head 上创建了 check run：report_stage_progress(run_key, stage=...)
        progress_heads = [
            c.args[0].head_sha
            for c in worker.check_run_service.report_stage_progress.await_args_list
        ]
        assert NEW_SHA in progress_heads

        # 最终 completed 体现在新 head：report_completed(run_key, ...)
        assert (
            worker.check_run_service.report_completed.call_args.args[0].head_sha
            == NEW_SHA
        )
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_review_worker_dependencies")
async def test_review_record_created_before_code_indexing(monkeypatch):
    """审查记录必须在代码索引之前落库。

    增量队列的 find_active_review 依赖 PRReview 行的存在来判定是否有活跃审查。
    若 review record 延后到代码索引之后才创建，索引耗时数十秒的窗口内到达的
    synchronize webhook 会查不到 active review，enqueue 返回 None，从而误触发
    第二个完整审查，造成并发 + 限流雪崩（见 pr_log.log 2026-06-24 的复盘）。
    """
    settings = get_settings()
    old_values = {
        "auto_index_pr_changes": settings.auto_index_pr_changes,
        "enable_code_index": settings.enable_code_index,
        "enable_rag": settings.enable_rag,
        "enable_pr_summary": settings.enable_pr_summary,
        "enable_pr_dependency_graph": settings.enable_pr_dependency_graph,
        "enable_ai_tools": getattr(settings, "enable_ai_tools", True),
        "sakura_memory_enabled": settings.sakura_memory_enabled,
        "sakura_reflection_enabled": settings.sakura_reflection_enabled,
        "enable_pr_issue_linking": getattr(settings, "enable_pr_issue_linking", False),
        "enable_semantic_issue_linking": getattr(
            settings, "enable_semantic_issue_linking", False
        ),
    }
    # 关键：开启代码索引分支
    settings.auto_index_pr_changes = True
    settings.enable_code_index = True
    settings.enable_rag = False
    settings.enable_pr_summary = False
    settings.enable_pr_dependency_graph = False
    settings.enable_ai_tools = True
    settings.sakura_memory_enabled = False
    settings.sakura_reflection_enabled = False
    settings.enable_pr_issue_linking = False
    settings.enable_semantic_issue_linking = False

    call_order = []  # 按真实调用顺序记录事件名

    class FakeAnalyzer:
        async def analyze_pr(self, pr_info):
            return SimpleNamespace(
                pr_id=pr_info["pr_id"],
                pr_number=pr_info["pr_number"],
                repo_full_name=pr_info["repo_full_name"],
                total_files=1,
                total_changes=2,
                code_file_count=1,
                code_files=[],
                strategy="standard",
                should_skip=False,
                is_incremental=False,
                changed_lines_map={},
                hunk_boundaries={},
            )

        async def prepare_review_context(self, analysis, pr):
            return {"files": [], "analysis": analysis}

    class FakeCommentService:
        async def create_placeholder_comment(self, pr, strategy, output_language=None):
            return SimpleNamespace(id=1)

        async def delete_placeholder_comment(self, review_obj):
            return None

    class FakeGitHubApp:
        def get_repo_client(self, repo_owner, repo_name):
            return SimpleNamespace(
                get_repo=lambda repo_full_name: SimpleNamespace(
                    get_pull=lambda pr_number: SimpleNamespace(
                        base=SimpleNamespace(repo=SimpleNamespace())
                    )
                )
            )

    class FakeIndexer:
        async def index_pr_changes(self, **kwargs):
            call_order.append("index_pr_changes")

    class FakeAIReviewer:
        def _refresh_ai_clients(self):
            return None

        async def review_pr_with_tools(self, *args, **kwargs):
            return {
                "summary": "ok",
                "overall_score": 8,
                "verdict": "approve",
                "comments": [],
                "inline_comments": [],
                "token_usage": {},
            }

    async def fake_noop(*_args, **_kwargs):
        return None

    async def fake_create_review_record(*_args, **_kwargs):
        call_order.append("create_review_record")
        return 99

    async def fake_make_decision(*_args, **_kwargs):
        return ReviewDecision.APPROVE, "ok", None

    class FakeSakuraMemoryService:
        async def get_sakura_context(self, **kwargs):
            return None

    async def fake_dynamic_config(*_args, **_kwargs):
        return None

    try:
        monkeypatch.setattr(review_worker, "GitHubAppClient", FakeGitHubApp)
        monkeypatch.setattr(review_worker, "PRAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(review_worker, "AIReviewer", FakeAIReviewer)
        monkeypatch.setattr(review_worker, "CommentService", FakeCommentService)
        monkeypatch.setattr(review_worker, "_get_label_rec_setting", lambda *_: False)
        monkeypatch.setattr(
            review_worker, "get_user_dynamic_config", fake_dynamic_config
        )
        monkeypatch.setattr(ReviewWorker, "_log_activity", staticmethod(fake_noop))
        monkeypatch.setattr(
            "backend.services.pr_code_indexer.get_pr_code_indexer",
            lambda: FakeIndexer(),
        )
        monkeypatch.setattr(
            "backend.services.sakura_memory_service.get_sakura_memory_service",
            lambda: FakeSakuraMemoryService(),
        )

        worker = ReviewWorker()
        monkeypatch.setattr(worker, "_create_review_record", fake_create_review_record)
        monkeypatch.setattr(worker, "_update_review_status", fake_noop)
        monkeypatch.setattr(worker, "_save_review_results", fake_noop)
        monkeypatch.setattr(worker, "_make_and_submit_decision", fake_make_decision)
        monkeypatch.setattr(worker, "_notify_agent_team_review_completed", fake_noop)
        monkeypatch.setattr(worker, "_send_review_complete_notification", fake_noop)

        pr_info = {
            "repo_owner": "owner",
            "repo_name": "repo",
            "repo_full_name": "owner/repo",
            "pr_id": 1001,
            "pr_number": 7,
            "author": "alice",
            "title": "PR",
            "branch": "feature",
            "user_id": 42,
            "action": "opened",
        }

        await worker.process_review_task(pr_info)

        # 不变量：review record 必须在代码索引之前创建，保证增量队列在索引
        # 窗口内能查到 active review
        assert "create_review_record" in call_order
        assert "index_pr_changes" in call_order
        assert call_order.index("create_review_record") < call_order.index(
            "index_pr_changes"
        )
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)
