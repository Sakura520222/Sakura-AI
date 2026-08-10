"""Agent Team resume helpers tests."""

import pytest

from backend.services.agent_team.context_compressor import (
    AgentTeamContextCompressor,
    _has_missing_tool_results,
    _split_message_blocks,
)
from backend.services.agent_team.fullstack_expert import _get_missing_tool_calls
from backend.workers.agent_team_worker import _parse_rate_limit_reset_at


def test_missing_tool_calls_skip_existing_tool_results():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "finish_task", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]

    missing = _get_missing_tool_calls(messages)

    assert len(missing) == 1
    assert missing[0].id == "call_2"
    assert missing[0].function.name == "finish_task"


def test_parse_rate_limit_reset_at_from_quota_error():
    reset_at = _parse_rate_limit_reset_at(
        "已达到 5 小时的使用上限。您的限额将在 2026-05-16 03:15:26 重置。"
    )

    assert reset_at is not None
    assert reset_at.year == 2026
    assert reset_at.month == 5
    assert reset_at.day == 16
    assert reset_at.hour == 3
    assert reset_at.minute == 15
    assert reset_at.second == 26


def test_context_compressor_keeps_tool_call_result_blocks_together():
    messages = [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]

    blocks = _split_message_blocks(messages)

    assert len(blocks) == 3
    assert blocks[1][0]["role"] == "assistant"
    assert blocks[1][1]["role"] == "tool"
    assert blocks[1][1]["tool_call_id"] == "call_1"


def test_context_compressor_detects_missing_tool_results():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }
    ]

    assert _has_missing_tool_results(messages) is True


@pytest.mark.asyncio
async def test_context_compressor_does_not_compress_missing_tool_results(monkeypatch):
    compressor = AgentTeamContextCompressor("tiny-model")

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("compressor should not summarize missing tool results")

    monkeypatch.setattr(compressor, "compress_messages", fail_if_called)
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
    ]

    assert await compressor.build_model_messages(messages) == messages


def test_context_compressor_resolve_safe_context_prefers_context_window_tokens(
    monkeypatch,
):
    """新版 unified config 解析的 context_window_tokens 优先于 model_context 兜底。"""
    compressor = AgentTeamContextCompressor("any-model", context_window_tokens=800_000)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("应使用 context_window_tokens，不应回退 model_context")

    monkeypatch.setattr(
        compressor.model_context_mgr, "calculate_safe_context", fail_if_called
    )
    # 800K tokens * 0.8 = 640_000 tokens
    assert compressor._resolve_safe_context(0.8) == 640_000


def test_context_compressor_resolve_safe_context_falls_back_without_override(
    monkeypatch,
):
    """未注入 context_window_tokens 时回退 model_context。"""
    compressor = AgentTeamContextCompressor("any-model")
    monkeypatch.setattr(
        compressor.model_context_mgr,
        "calculate_safe_context",
        lambda *_a, **_k: 99_999,
    )
    assert compressor._resolve_safe_context(0.8) == 99_999
