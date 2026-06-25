"""Review worker dynamic timeout coverage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.config import get_settings
from backend.services.comment_service import CommentService
from backend.workers import review_worker
from backend.workers.review_worker import (
    ReviewWorker,
    ReviewDecision,
    _run_review_task_with_timeout,
    submit_review_task,
)


@pytest.fixture
def stub_review_worker_dependencies(monkeypatch):
    monkeypatch.setattr(review_worker, "GitHubAppClient", lambda: object())
    monkeypatch.setattr(review_worker, "PRAnalyzer", lambda: object())
    monkeypatch.setattr(review_worker, "AIReviewer", lambda: object())
    monkeypatch.setattr(review_worker, "CommentService", lambda: object())
    monkeypatch.setattr(review_worker, "_worker_instance", None)
    yield
    monkeypatch.setattr(review_worker, "_worker_instance", None)


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

    pr_info = {"repo_full_name": "owner/repo", "repo_owner": "owner", "repo_name": "repo", "pr_number": 1}
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
        summary_api_client=object(),
        summary_model="summary-model",
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

    class FakeCheckpoint:
        def __init__(self, source_type, source_task_id):
            self.source_type = source_type
            self.source_task_id = source_task_id

        async def create_session(self, **kwargs):
            return SimpleNamespace(id=55)

        async def append_message(self, session_id, data):
            return SimpleNamespace(id=100 + len(data))

        async def mark_tool_call_completed(self, session_id, tool_call_id, msg_id):
            return None

        async def mark_tool_call_running(self, session_id, tool_call_id):
            return None

        async def complete_session(self, session_id, tool_calls_count=0):
            return None

        async def save_session_result(self, session_id, payload):
            return None

        async def fail_session(self, session_id, error_message):
            return None

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

    async def fake_restore_history(*_args, **_kwargs):
        return [{"role": "assistant", "content": "previous"}]

    async def fake_create_review_record(*_args, **_kwargs):
        return 99

    async def fake_make_decision(*_args, **_kwargs):
        return ReviewDecision.APPROVE, "ok"

    try:
        monkeypatch.setattr(review_worker, "GitHubAppClient", FakeGitHubApp)
        monkeypatch.setattr(review_worker, "PRAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(review_worker, "AIReviewer", FakeAIReviewer)
        monkeypatch.setattr(review_worker, "CommentService", FakeCommentService)
        monkeypatch.setattr(review_worker, "_get_label_rec_setting", lambda *_: False)
        monkeypatch.setattr(review_worker, "get_user_dynamic_config", fake_dynamic_config)
        monkeypatch.setattr(
            ReviewWorker,
            "_log_activity",
            staticmethod(fake_noop),
        )
        monkeypatch.setattr(
            "backend.services.activity_checkpoint_service.ActivityCheckpointService",
            FakeCheckpoint,
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
        previous_messages = [{"role": "assistant", "content": "previous"}]
        monkeypatch.setattr(
            worker,
            "_restore_incremental_activity_history",
            fake_restore_history,
        )
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

        assert captured["initial_messages"] == previous_messages
        assert captured["pending_callback"] is not None
        assert captured["pending_message"] == {
            "role": "user",
            "content": "queued increment",
        }
        assert captured["marked_consumed"][0] == ([1],)
        assert captured["marked_consumed"][1]["consumed_message_id"] is not None
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
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

    class FakeCheckpoint:
        def __init__(self, *a, **kw):
            pass

        async def create_session(self, **kw):
            return SimpleNamespace(id=55)

        async def append_message(self, sid, data):
            return SimpleNamespace(id=100)

        async def mark_tool_call_completed(self, *a, **kw):
            return None

        async def mark_tool_call_running(self, *a, **kw):
            return None

        async def complete_session(self, sid, **kw):
            return None

        async def save_session_result(self, sid, payload):
            return None

        async def fail_session(self, sid, msg):
            return None

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
        return ReviewDecision.APPROVE, "ok"

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
        monkeypatch.setattr(review_worker, "get_user_dynamic_config", fake_dynamic_config)
        monkeypatch.setattr(ReviewWorker, "_log_activity", staticmethod(fake_noop))
        monkeypatch.setattr(
            "backend.services.activity_checkpoint_service.ActivityCheckpointService",
            FakeCheckpoint,
        )
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
            report_completed=AsyncMock(),
            report_failed=AsyncMock(),
            report_cancelled=AsyncMock(),
            report_skipped=AsyncMock(),
        )
        monkeypatch.setattr(worker, "_restore_incremental_activity_history", fake_noop)
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

        # 旧 head 收尾为 cancelled（增量取代）
        worker.check_run_service.report_cancelled.assert_awaited()
        assert worker.check_run_service.report_cancelled.call_args.args[2] == OLD_SHA

        # 新 head 上创建了 check run（迁移的 reviewing + 后续 reporting）
        progress_heads = [
            c.args[2] for c in worker.check_run_service.report_progress.await_args_list
        ]
        assert NEW_SHA in progress_heads

        # 最终 completed 体现在新 head（PR 最新 commit）
        assert worker.check_run_service.report_completed.call_args.args[2] == NEW_SHA
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
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
            return None

    class FakeCheckpoint:
        def __init__(self, source_type, source_task_id):
            pass

        async def create_session(self, **kwargs):
            return SimpleNamespace(id=55)

        async def append_message(self, session_id, data):
            return SimpleNamespace(id=1)

        async def mark_tool_call_completed(self, session_id, tool_call_id, msg_id):
            return None

        async def mark_tool_call_running(self, session_id, tool_call_id):
            return None

        async def complete_session(self, session_id, tool_calls_count=0):
            return None

        async def save_session_result(self, session_id, payload):
            return None

        async def fail_session(self, session_id, error_message):
            return None

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
        return ReviewDecision.APPROVE, "ok"

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
        monkeypatch.setattr(review_worker, "get_user_dynamic_config", fake_dynamic_config)
        monkeypatch.setattr(
            ReviewWorker, "_log_activity", staticmethod(fake_noop)
        )
        monkeypatch.setattr(
            "backend.services.activity_checkpoint_service.ActivityCheckpointService",
            FakeCheckpoint,
        )
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
        monkeypatch.setattr(
            worker, "_make_and_submit_decision", fake_make_decision
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
