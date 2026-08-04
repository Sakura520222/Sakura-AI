import pytest

from backend.core.ai_protocol.adapters.gemini_native import GeminiNativeAdapter
from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.ai_protocol.models import (
    StopReason,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedToolCall,
    UnifiedUsage,
)
from backend.services import sakura_agent_base as agent_base_module
from backend.services.sakura_agent_base import SakuraAgentBase
from backend.services.sakura_consolidation_agent import SakuraConsolidationAgent


@pytest.mark.asyncio
async def test_consolidation_agent_starts_conversation_with_user_task_message():
    captured = {}

    class Client:
        async def call_with_retry(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            captured["model"] = kwargs["model"]
            captured["role"] = kwargs["role"]
            return UnifiedResponse(
                content="done",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=UnifiedUsage(),
            )

    agent = SakuraConsolidationAgent()
    agent._api_client = Client()
    agent._default_model = "test-model"

    changes = await agent.consolidate_file(
        repo=object(),
        repo_full_name="owner/repo",
        sakura_ref="main",
        target_file="SAKURA.md",
        new_reflection_files=["2026-05-31_PR372_adca028.md"],
        total_reflections=383,
        max_chars=5000,
        languages="Python: 1",
        model="test-model",
        max_iterations=1,
    )

    assert changes == {}
    messages = captured["messages"]
    assert [message["role"] for message in messages[:2]] == ["system", "user"]
    assert "SAKURA.md" in messages[1]["content"]
    assert "开始" in messages[1]["content"]
    assert captured["model"] == ""
    assert captured["role"] == "main"


@pytest.mark.asyncio
async def test_agent_conversation_executes_unified_response_tool_calls():
    """统一协议响应含工具调用时，Agent 必须继续执行下一轮，而非访问旧 finish_reason。"""
    captured = {"messages": []}

    class Client:
        def __init__(self):
            self._responses = [
                UnifiedResponse(
                    content="",
                    tool_calls=[
                        UnifiedToolCall(
                            id="call-1",
                            name="write_file",
                            arguments='{"file_path":"memory.md","content":"updated"}',
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                    usage=UnifiedUsage(),
                    reasoning_content="tool-call reasoning",
                ),
                UnifiedResponse(
                    content="done",
                    tool_calls=[],
                    stop_reason=StopReason.END_TURN,
                    usage=UnifiedUsage(),
                ),
            ]

        async def call_with_retry(self, **kwargs):
            captured["messages"].append(kwargs["messages"].copy())
            return self._responses.pop(0)

    agent = SakuraConsolidationAgent()
    agent._api_client = Client()

    changes = await agent.consolidate_file(
        repo=object(),
        repo_full_name="owner/repo",
        sakura_ref="main",
        target_file="memory.md",
        new_reflection_files=[],
        total_reflections=1,
        max_chars=5000,
        languages="Python: 1",
        max_iterations=2,
    )

    assert changes == {"memory.md": "updated"}
    assert len(captured["messages"]) == 2
    assert captured["messages"][1][-2]["role"] == "assistant"
    assert captured["messages"][1][-2]["tool_calls"][0].name == "write_file"
    assert captured["messages"][1][-2]["reasoning_content"] == "tool-call reasoning"
    assert captured["messages"][1][-1] == {
        "role": "tool",
        "content": "已写入: memory.md (7 字符)",
        "tool_call_id": "call-1",
        "name": "write_file",
    }


@pytest.mark.asyncio
async def test_agent_conversation_records_tool_name_for_gemini_replay():
    """工具结果必须保留函数名，供 Gemini 按协议回放 functionResponse。"""
    captured = {"messages": []}

    class Client:
        def __init__(self):
            self._responses = [
                UnifiedResponse(
                    content="",
                    tool_calls=[
                        UnifiedToolCall(
                            id="call-1",
                            name="write_file",
                            arguments='{"file_path":"memory.md","content":"updated"}',
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                    usage=UnifiedUsage(),
                ),
                UnifiedResponse(
                    content="done",
                    tool_calls=[],
                    stop_reason=StopReason.END_TURN,
                    usage=UnifiedUsage(),
                ),
            ]

        async def call_with_retry(self, **kwargs):
            captured["messages"].append(kwargs["messages"].copy())
            return self._responses.pop(0)

    agent = SakuraConsolidationAgent()
    agent._api_client = Client()

    await agent.consolidate_file(
        repo=object(),
        repo_full_name="owner/repo",
        sakura_ref="main",
        target_file="memory.md",
        new_reflection_files=[],
        total_reflections=1,
        max_chars=5000,
        languages="Python: 1",
        max_iterations=2,
    )

    from backend.services.ai_reviewer.unified_client import _messages_from_legacy

    replay_messages = _messages_from_legacy(captured["messages"][1])
    request = UnifiedRequest(
        model="gemini-test",
        messages=replay_messages,
        max_tokens=128,
    )
    body = GeminiNativeAdapter().serialize_request(request)

    assert body["contents"][-1]["parts"][0]["functionResponse"]["name"] == "write_file"


@pytest.mark.asyncio
async def test_agent_conversation_propagates_cancellation_from_llm_call():
    """调用期间触发的取消必须传回 worker，不能被 Agent 的通用异常处理吞掉。"""

    class Client:
        async def call_with_retry(self, **kwargs):
            raise ReviewCancelledError()

    class Agent(SakuraAgentBase):
        def _ensure_client(self):
            return None

        def _get_tools(self):
            return []

    agent = Agent()
    agent._api_client = Client()

    with pytest.raises(ReviewCancelledError):
        await agent._run_agent_conversation(
            system_prompt="system instructions",
            model="test-model",
            max_iterations=1,
        )


@pytest.mark.asyncio
async def test_agent_conversation_does_not_report_max_iterations_after_llm_error(
    monkeypatch,
):
    logs = {"errors": [], "warnings": []}

    class Logger:
        def error(self, message, *args, **kwargs):
            logs["errors"].append(message.format(*args))

        def warning(self, message, *args, **kwargs):
            logs["warnings"].append(message.format(*args))

        def info(self, message, *args, **kwargs):
            pass

    class Client:
        async def call_with_retry(self, **kwargs):
            raise RuntimeError("upstream request failed")

    class Agent(SakuraAgentBase):
        log_prefix = "[test-agent]"

        def _ensure_client(self):
            return None

        def _get_tools(self):
            return []

    monkeypatch.setattr(agent_base_module, "logger", Logger())
    agent = Agent()
    agent._api_client = Client()

    await agent._run_agent_conversation(
        system_prompt="system instructions",
        model="test-model",
        max_iterations=50,
        initial_user_message="start",
    )

    assert any("LLM 调用失败 (iteration 0)" in error for error in logs["errors"])
    assert not any("达到最大迭代次数" in warning for warning in logs["warnings"])
