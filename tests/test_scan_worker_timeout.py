"""仓库扫描软超时与协议失败终态测试。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.scan_models import ScanStatus
from backend.services.ai_task_deadline import TIMEOUT_PROMPT, AITaskDeadline
from backend.workers import scan_worker as scan_worker_module
from backend.workers.scan_worker import ScanWorker


class _AsyncSession:
    def __init__(self, scan):
        self.scan = scan

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args):
        return self.scan


class _Execution:
    def __init__(self):
        self.finish = AsyncMock()


class _ScanTracker:
    def accumulate(self, _response):
        pass

    def log_context_usage(self, *_args):
        pass


class _ManualDeadline:
    timeout_prompt_sent = False
    tools_disabled = False

    def __init__(self):
        self._expired = False

    def is_expired(self):
        return self._expired

    def prepare_call(self, messages):
        if self.tools_disabled or self._expired:
            self.tools_disabled = True
            if not self.timeout_prompt_sent:
                messages.append({"role": "user", "content": TIMEOUT_PROMPT})
                self.timeout_prompt_sent = True
            return {"tools": [], "tool_choice": "none"}
        return {}


class _ScanApiClient:
    def __init__(self, responses, *, after_call=None):
        self.responses = list(responses)
        self.after_call = after_call
        self.calls = []

    async def resolve_role_model_context(self, role):
        assert role == "main"
        return "scan-test-model", 100_000

    async def call_with_retry(self, **kwargs):
        call = dict(kwargs)
        call["messages"] = list(kwargs["messages"])
        self.calls.append(call)
        response = self.responses.pop(0)
        if self.after_call is not None:
            self.after_call(len(self.calls))
        return response


class _ScanToolService:
    def __init__(self):
        self.messages = []
        self.completed = []

    async def append_conversation_message(self, **kwargs):
        self.messages.append(kwargs["message"])

    def is_failed_tool_result(self, _message):
        return False

    async def mark_tool_execution_completed(self, _work_unit_id, tool_call_id):
        self.completed.append(tool_call_id)


class _ObservingExecution:
    def __init__(self):
        self.thread = SimpleNamespace(id="thread-1")
        self.work_unit = SimpleNamespace(id="work-unit-1")
        self.observer = SimpleNamespace(last_attempt_id="attempt-1")
        self.invocation_context = {"source": "test"}
        self.lease = "lease-1"
        self.tool_service = _ScanToolService()


def _tool_call(call_id):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="read_file", arguments="{}"),
    )


def _response(content, tool_calls):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls)
            )
        ],
    )


def _prepare_scan_worker(monkeypatch, api_client, tool_handler, *, config_value=None):
    class _FakeReviewer:
        def __init__(self):
            self.api_client = api_client

            async def _get_enabled_tools(_repo_name):
                return [{"type": "function", "function": {"name": "read_file"}}]

            self.tool_manager = SimpleNamespace(
                get_enabled_tools=_get_enabled_tools,
            )
            self.tool_handler = SimpleNamespace(handle_tool_call=tool_handler)
            self.model_context_mgr = SimpleNamespace(
                calculate_safe_context=lambda *_args: 80_000,
            )
            self.enable_compression = False
            self.context_compressor = SimpleNamespace(
                estimate_messages_tokens=lambda _messages: 1,
            )

    worker = ScanWorker.__new__(ScanWorker)
    worker.github_app = SimpleNamespace(
        get_installation_client=lambda *_args: None,
    )
    worker._update_scan = AsyncMock()

    import backend.services.ai_reviewer.reviewer as reviewer_module
    import backend.services.ai_reviewer.tools.local_repo_adapter as adapter_module
    import backend.services.sakura_memory_service as memory_module
    import backend.services.scan_prompt_builder as prompt_builder

    monkeypatch.setattr(reviewer_module, "AIReviewer", _FakeReviewer)
    monkeypatch.setattr(
        adapter_module,
        "LocalRepoAdapter",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        prompt_builder,
        "build_scan_context",
        lambda **_kwargs: "scan context",
    )
    monkeypatch.setattr(
        prompt_builder,
        "build_scan_system_prompt",
        lambda *_args, **_kwargs: "scan system",
    )
    monkeypatch.setattr(
        prompt_builder,
        "build_scan_user_message",
        lambda *_args, **_kwargs: "scan user",
    )

    async def _empty_sakura_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        memory_module,
        "get_sakura_memory_service",
        lambda: SimpleNamespace(get_sakura_context=_empty_sakura_context),
    )
    monkeypatch.setattr(
        scan_worker_module,
        "get_settings",
        lambda: SimpleNamespace(
            ai_temperature=0.2,
            context_compression_threshold=0.8,
            review_timeout_seconds=120,
        ),
    )

    async def _get_dynamic_config(key):
        if key == "protocol_repair_max_attempts":
            return config_value
        return None

    monkeypatch.setattr(scan_worker_module, "get_dynamic_config", _get_dynamic_config)
    return worker


def _capture_scan_repair(monkeypatch):
    import backend.services.protocol_repair as protocol_repair_module

    calls = []

    async def _fake_repair_loop(**kwargs):
        calls.append(kwargs)
        return {
            "findings": [],
            "overall_score": 100,
            "summary": "scan test result",
            "parse_source": "tagged",
        }

    monkeypatch.setattr(
        protocol_repair_module,
        "run_protocol_repair_loop",
        _fake_repair_loop,
    )
    return calls


@pytest.mark.asyncio
async def test_protocol_failure_marks_scan_failed_before_side_effects(
    monkeypatch, tmp_path
):
    scan = SimpleNamespace(
        status=ScanStatus.PENDING.value,
        repo_name="owner/repo",
        triggered_by="webui:1",
    )

    import backend.models.database as database_module

    monkeypatch.setattr(
        database_module,
        "async_session",
        lambda: _AsyncSession(scan),
    )

    worker = ScanWorker.__new__(ScanWorker)
    execution = _Execution()
    updates = []
    save_findings = AsyncMock()
    generate_reports = AsyncMock()

    async def _update_scan(_scan_id, **kwargs):
        updates.append(kwargs)

    async def _full_scan(**_kwargs):
        return (
            {
                "findings": [],
                "overall_score": None,
                "summary": "scan protocol could not be repaired",
                "parse_source": "scan_protocol_error",
            },
            4,
        )

    monkeypatch.setattr(worker, "_start_threaded_execution", AsyncMock(return_value=execution))
    monkeypatch.setattr(worker, "_clone_repo", AsyncMock(return_value=str(tmp_path)))
    monkeypatch.setattr(worker, "_get_commit_sha", AsyncMock(return_value="abc"))
    monkeypatch.setattr(
        worker,
        "_index_repository",
        AsyncMock(return_value={"total_chunks": 2, "indexed": 1}),
    )
    monkeypatch.setattr(
        scan_worker_module,
        "get_user_dynamic_config",
        AsyncMock(return_value="zh-CN"),
    )
    import backend.services.scan_prompt_builder as prompt_builder

    monkeypatch.setattr(
        prompt_builder,
        "collect_code_files",
        lambda _repo_path: [{"path": "app.py"}],
    )
    monkeypatch.setattr(worker, "_update_scan", _update_scan)
    monkeypatch.setattr(worker, "_full_scan_with_tools", _full_scan)
    monkeypatch.setattr(worker, "_save_findings", save_findings)
    monkeypatch.setattr(worker, "_generate_reports", generate_reports)

    await worker._process_scan_inner(
        7,
        deadline=AITaskDeadline.from_timeout(60),
    )

    assert any(
        update.get("status") == ScanStatus.FAILED.value for update in updates
    )
    assert not any(
        update.get("status") in {ScanStatus.REPORTING.value, ScanStatus.COMPLETED.value}
        for update in updates
    )
    save_findings.assert_not_awaited()
    generate_reports.assert_not_awaited()
    execution.finish.assert_awaited_once_with(
        "failed", error_message="scan protocol could not be repaired"
    )


@pytest.mark.asyncio
async def test_scan_cancellation_finishes_execution_and_propagates(monkeypatch):
    """外部取消必须停止 observability lease 并保留 CancelledError。"""
    scan = SimpleNamespace(
        status=ScanStatus.PENDING.value,
        repo_name="owner/repo",
        triggered_by="webui:1",
    )

    import backend.models.database as database_module

    monkeypatch.setattr(
        database_module,
        "async_session",
        lambda: _AsyncSession(scan),
    )

    worker = ScanWorker.__new__(ScanWorker)
    execution = _Execution()
    monkeypatch.setattr(
        worker, "_start_threaded_execution", AsyncMock(return_value=execution)
    )
    monkeypatch.setattr(worker, "_update_scan", AsyncMock())
    monkeypatch.setattr(
        worker, "_clone_repo", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await worker._process_scan_inner(
            8,
            deadline=AITaskDeadline.from_timeout(60),
        )

    execution.finish.assert_awaited_once_with("cancelled", error_message=None)


@pytest.mark.asyncio
async def test_process_scan_passes_one_deadline_through_semaphore(monkeypatch):
    worker = ScanWorker.__new__(ScanWorker)
    observed = {}

    class _Semaphore:
        async def __aenter__(self):
            observed["entered"] = True
            return self

        async def __aexit__(self, *_args):
            return False

    async def _get_semaphore():
        assert "deadline" not in observed
        return _Semaphore()

    async def _inner(scan_id, *, deadline):
        observed["scan_id"] = scan_id
        observed["deadline"] = deadline

    monkeypatch.setattr(scan_worker_module, "_get_scan_semaphore", _get_semaphore)
    monkeypatch.setattr(worker, "_process_scan_inner", _inner)

    await worker.process_scan(11)

    assert observed["scan_id"] == 11
    assert isinstance(observed["deadline"], AITaskDeadline)
    assert observed["entered"] is True


@pytest.mark.asyncio
async def test_scan_tool_calls_returned_after_deadline_are_closed_before_timeout_call(
    monkeypatch, tmp_path
):
    """跨 deadline 返回的扫描工具调用必须先闭合再发送最终请求。"""
    deadline = _ManualDeadline()
    tool_calls = [_tool_call("scan-call-1"), _tool_call("scan-call-2")]

    def _expire_after_first_call(call_count):
        if call_count == 1:
            deadline._expired = True

    client = _ScanApiClient(
        [
            _response("partial scan", tool_calls),
            _response("final scan", []),
        ],
        after_call=_expire_after_first_call,
    )
    tool_handler = AsyncMock(return_value={"ok": True})
    execution = _ObservingExecution()
    worker = _prepare_scan_worker(
        monkeypatch,
        client,
        tool_handler,
    )
    repair_calls = _capture_scan_repair(monkeypatch)

    result, iteration = await worker._full_scan_with_tools(
        21,
        "owner/repo",
        str(tmp_path),
        "sha-cross-deadline",
        _ScanTracker(),
        [],
        execution=execution,
        deadline=deadline,
    )

    assert result["parse_source"] == "tagged"
    assert iteration == 2
    assert tool_handler.await_count == 0
    assert len(client.calls) == 2

    final_call = client.calls[1]
    assert final_call["tools"] == []
    assert final_call["tool_choice"] == "none"
    assert sum(
        message.get("content") == TIMEOUT_PROMPT
        for message in final_call["messages"]
    ) == 1

    assistant_turns = [
        message
        for message in final_call["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    tool_messages = [
        message for message in final_call["messages"] if message.get("role") == "tool"
    ]
    assert len(assistant_turns) == 1
    assert {tool_call.id for tool_call in assistant_turns[0]["tool_calls"]} == {
        "scan-call-1",
        "scan-call-2",
    }
    assert {message["tool_call_id"] for message in tool_messages} == {
        "scan-call-1",
        "scan-call-2",
    }
    assert all(
        json.loads(message["content"])["error"]
        == "Task deadline reached; this tool call was not executed."
        for message in tool_messages
    )

    assert len(repair_calls) == 1
    assert {
        message["tool_call_id"]
        for message in repair_calls[0]["base_messages"]
        if message.get("role") == "tool"
    } == {"scan-call-1", "scan-call-2"}
    assert {
        message["tool_call_id"]
        for message in execution.tool_service.messages
        if message.get("role") == "tool"
    } == {"scan-call-1", "scan-call-2"}


@pytest.mark.asyncio
async def test_scan_tools_disabled_provider_tool_calls_close_and_use_configured_repair_attempts(
    monkeypatch, tmp_path
):
    """tools_disabled 状态下的违规工具响应仍须闭合并进入协议修复。"""
    deadline = AITaskDeadline.from_timeout(0)
    tool_calls = [_tool_call("disabled-call-1"), _tool_call("disabled-call-2")]
    client = _ScanApiClient([_response("provider violated tool-free mode", tool_calls)])
    tool_handler = AsyncMock(return_value={"unexpected": True})
    execution = _ObservingExecution()
    worker = _prepare_scan_worker(
        monkeypatch,
        client,
        tool_handler,
        config_value="2",
    )
    repair_calls = _capture_scan_repair(monkeypatch)

    result, iteration = await worker._full_scan_with_tools(
        22,
        "owner/repo",
        str(tmp_path),
        "sha-tools-disabled",
        _ScanTracker(),
        [],
        execution=execution,
        deadline=deadline,
    )

    assert result["parse_source"] == "tagged"
    assert iteration == 1
    assert tool_handler.await_count == 0
    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["tool_choice"] == "none"
    assert len(repair_calls) == 1
    assert repair_calls[0]["max_attempts"] == 2

    repair_tool_messages = [
        message
        for message in repair_calls[0]["base_messages"]
        if message.get("role") == "tool"
    ]
    assert {message["tool_call_id"] for message in repair_tool_messages} == {
        "disabled-call-1",
        "disabled-call-2",
    }


@pytest.mark.asyncio
async def test_scan_normal_tool_call_path_remains_executable(monkeypatch, tmp_path):
    """未到 deadline 时，扫描仍执行工具并把正常结果交给下一轮。"""
    deadline = _ManualDeadline()
    tool_call = _tool_call("normal-call")
    client = _ScanApiClient(
        [
            _response("need repository evidence", [tool_call]),
            _response("final scan", []),
        ]
    )
    tool_handler = AsyncMock(return_value={"path": "app.py"})
    execution = _ObservingExecution()
    worker = _prepare_scan_worker(
        monkeypatch,
        client,
        tool_handler,
    )
    repair_calls = _capture_scan_repair(monkeypatch)

    result, iteration = await worker._full_scan_with_tools(
        23,
        "owner/repo",
        str(tmp_path),
        "sha-normal",
        _ScanTracker(),
        [],
        execution=execution,
        deadline=deadline,
    )

    assert result["parse_source"] == "tagged"
    assert iteration == 2
    tool_handler.assert_awaited_once()
    assert client.calls[0]["tools"]
    assert client.calls[0]["tool_choice"] == "auto"
    assert client.calls[1]["tools"]
    assert "tool_choice" not in client.calls[1] or client.calls[1]["tool_choice"] == "auto"

    tool_messages = [
        message
        for message in client.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "normal-call"
    assert json.loads(tool_messages[0]["content"]) == {"path": "app.py"}
    assert TIMEOUT_PROMPT not in [
        message.get("content") for message in client.calls[1]["messages"]
    ]
    assert len(repair_calls) == 1


@pytest.mark.asyncio
async def test_scan_deadline_during_multiple_tools_skips_remaining_calls(
    monkeypatch, tmp_path
):
    """多工具执行中到期时，只执行已开始的调用并闭合其余调用。"""
    deadline = _ManualDeadline()
    first_call = _tool_call("running-call")
    skipped_call = _tool_call("skipped-call")
    client = _ScanApiClient(
        [
            _response("need two files", [first_call, skipped_call]),
            _response("final scan", []),
        ]
    )

    async def _handle_tool_call(tool_call, *_args):
        if tool_call.id == "running-call":
            deadline._expired = True
        return {"executed": tool_call.id}

    execution = _ObservingExecution()
    worker = _prepare_scan_worker(
        monkeypatch,
        client,
        _handle_tool_call,
    )
    repair_calls = _capture_scan_repair(monkeypatch)

    result, iteration = await worker._full_scan_with_tools(
        24,
        "owner/repo",
        str(tmp_path),
        "sha-mid-tool-loop",
        _ScanTracker(),
        [],
        execution=execution,
        deadline=deadline,
    )

    assert result["parse_source"] == "tagged"
    assert iteration == 2
    assert len(client.calls) == 2
    assert client.calls[1]["tools"] == []

    tool_messages = [
        message
        for message in client.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert {message["tool_call_id"] for message in tool_messages} == {
        "running-call",
        "skipped-call",
    }
    running_message = next(
        message for message in tool_messages if message["tool_call_id"] == "running-call"
    )
    skipped_message = next(
        message for message in tool_messages if message["tool_call_id"] == "skipped-call"
    )
    assert json.loads(running_message["content"]) == {"executed": "running-call"}
    assert json.loads(skipped_message["content"])["error"] == (
        "Task deadline reached; this tool call was not executed."
    )
    assert len(repair_calls) == 1
    assert {
        message["tool_call_id"]
        for message in execution.tool_service.messages
        if message.get("role") == "tool"
    } == {"running-call", "skipped-call"}
