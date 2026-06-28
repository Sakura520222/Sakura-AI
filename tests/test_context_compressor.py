"""ContextCompressor 回归测试。

聚焦连续审查场景：审查进行中注入增量 diff 后触发上下文压缩时，
最新增量 user 消息必须原样保留，不得并入早期历史摘要而丢失。
"""

from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.compression import ContextCompressor


class _FakeCompressClient:
    """模拟压缩用 AI 客户端，返回固定摘要，不发起真实请求。"""

    async def call_with_retry(self, **kwargs):
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5)
        message = SimpleNamespace(content="COMPRESSED_EARLY_HISTORY")
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], usage=usage)


def _assistant_with_tool(call_id: str) -> dict:
    """构造一条带 tool_calls 的 assistant 消息（一轮工具调用的起点）。

    tool_calls 项用 SimpleNamespace 模拟 OpenAI SDK 返回的对象结构：生产中
    _run_tool_loop 直接把 response.message.tool_calls 原样 append 进 messages，
    其项是 SDK 对象（带 .function.name 等属性），而非 dict——_compress_early_history
    正是按 `tc.function` 属性访问来遍历的。
    """
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name="get_file_diff", arguments="{}"),
            )
        ],
    }


def _tool_result(call_id: str) -> dict:
    """构造对应 tool_call_id 的工具结果消息。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": '{"diff": "old changes"}',
    }


@pytest.mark.asyncio
async def test_compression_preserves_latest_incremental_user_message():
    """连续审查触发压缩时，最新注入的增量 user 消息必须原样保留。

    场景对应 _run_tool_loop 中 _append_pending_user_message_if_any 刚把增量
    diff 作为 user 消息追加到 messages 末尾、AI 尚未回复时上下文即超限触发
    压缩。压缩按"从后向前保留最近 N 轮工具调用"策略，应将该增量消息纳入
    保留区，而不是并入 early_history 摘要导致丢失。
    """
    compressor = ContextCompressor(
        api_client=_FakeCompressClient(),
        model="main-model",
        keep_rounds=2,
    )

    incremental_msg = {
        "role": "user",
        "content": "INCREMENTAL_DIFF_FROM_NEW_COMMIT",
    }

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "initial pr context"},
    ]
    # 三轮工具调用历史：最早一轮落入 early_history 被压成摘要，最近两轮保留
    for idx in range(3):
        call_id = f"call_old_{idx}"
        messages.append(_assistant_with_tool(call_id))
        messages.append(_tool_result(call_id))
    # 末尾追加的增量 user 消息（连续审查场景，AI 尚未对其回复）
    messages.append(incremental_msg)

    compressed = await compressor.compress_conversation_history(
        messages,
        system_prompt="system prompt",
        max_tokens=100,
    )

    # 1. 最新增量 user 消息原样保留（压缩操作的是原 dict 引用，身份不变）
    assert incremental_msg in compressed
    # 2. 内容未被改动
    assert any(
        m.get("role") == "user"
        and m.get("content") == "INCREMENTAL_DIFF_FROM_NEW_COMMIT"
        for m in compressed
    )
    # 3. early_history 确实被压缩成了摘要（证明压缩真正发生，且摘要中
    #    不含增量消息原文）
    summary_messages = [
        m
        for m in compressed
        if m.get("role") == "user"
        and "COMPRESSED_EARLY_HISTORY" in m.get("content", "")
    ]
    assert summary_messages, "早期历史应被压缩为摘要消息"
    assert all(
        "INCREMENTAL_DIFF_FROM_NEW_COMMIT" not in m.get("content", "")
        for m in summary_messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_call",
    [
        pytest.param(
            {
                "id": "call_dict",
                "type": "function",
                "function": {"name": "get_file_diff", "arguments": "{}"},
            },
            id="dict_form",
        ),
        pytest.param(
            "ChatCompletionMessageToolCall(id='call_str', function=Completion(...))",
            id="stringified_form",
        ),
    ],
)
async def test_compression_handles_restored_non_sdk_tool_calls(tool_call):
    """从 checkpoint 恢复的历史 tool_calls 不是 SDK 对象时仍需正常压缩。

    连续/增量审查的消息经 json.dumps(default=str) 持久化、json.loads 恢复，
    tool_calls 可能是 dict（规范化形态）或字符串（SDK 对象被 default=str），
    而非带 .function 属性的 SDK 对象。_compress_early_history 不得因此抛
    异常并静默回退 fallback（无摘要），应正常产出摘要。
    """
    compressor = ContextCompressor(
        api_client=_FakeCompressClient(),
        model="main-model",
        keep_rounds=2,
    )

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "initial pr context"},
    ]
    # 三轮历史，最早一轮落入 early_history 被压缩（其 tool_calls 为非 SDK 形态）
    for idx in range(3):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_old_{idx}",
                "content": '{"diff": "old"}',
            }
        )
    messages.append({"role": "user", "content": "INCREMENTAL_DIFF"})

    compressed = await compressor.compress_conversation_history(
        messages,
        system_prompt="system prompt",
        max_tokens=100,
    )

    # 主路径应产出摘要，而非静默回退 fallback（fallback 无摘要）
    assert any(
        "COMPRESSED_EARLY_HISTORY" in m.get("content", "")
        for m in compressed
        if m.get("role") == "user"
    ), (
        f"tool_calls 形态 {type(tool_call).__name__} 应被正常压缩为摘要，而非回退 fallback"
    )
