"""Agent Team resume helpers tests."""

import pytest

from backend.services.agent_team.context_compressor import (
    _from_unified_messages,
    compress_agent_team_messages,
)
from backend.services.agent_team.fullstack_expert import _get_missing_tool_calls
from backend.services.agent_team.iteration_loop import _normalize_legacy_messages
from backend.services.agent_team.prompt_config import (
    IMPLEMENTATION_SYSTEM_PROMPT,
    build_implementation_user_message,
)
from backend.services.ai_reviewer.unified_client import messages_from_legacy
from backend.workers.agent_team_worker import _parse_rate_limit_reset_at


def test_legacy_fullstack_resume_replaces_only_system_and_initial_user():
    initial_user = build_implementation_user_message(
        task_title="Current task",
        task_summary="Current objective",
        source_type="issue",
        source_issue_number=7,
    )
    guidance = {
        "role": "user",
        "content": "keep this guidance exactly",
        "metadata": {"guidance_ids": [42]},
    }
    assistant = {"role": "assistant", "content": "historical response"}
    tool = {"role": "tool", "content": "historical tool result"}

    normalized = _normalize_legacy_messages(
        [
            {"role": "system", "content": "old fullstack policy"},
            {"role": "user", "content": "old dynamic initial prompt"},
            assistant,
            guidance,
            tool,
        ],
        initial_user_message=initial_user,
    )

    assert normalized[0] == {
        "role": "system",
        "content": IMPLEMENTATION_SYSTEM_PROMPT,
    }
    assert normalized[1] == {"role": "user", "content": initial_user}
    assert normalized[2] is not assistant
    assert normalized[2] == assistant
    assert normalized[3] == guidance
    assert normalized[4] == tool


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


def test_to_unified_round_trip_preserves_structure():
    """dict -> UnifiedMessage -> dict 往返保留关键字段。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]

    unified = messages_from_legacy(messages)
    assert len(unified) == 5
    assert unified[0].role == "system"
    assert unified[2].tool_calls is not None
    assert unified[2].tool_calls[0].name == "read_file"
    assert unified[2].tool_calls[0].arguments == '{"path": "a.py"}'
    assert unified[3].tool_call_id == "call_1"

    back = _from_unified_messages(unified)
    assert len(back) == 5
    assert back[0]["role"] == "system"
    assert back[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert back[3]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_compress_agent_team_messages_skips_when_no_candidate():
    messages = [{"role": "user", "content": "hello"}]
    result = await compress_agent_team_messages(messages, candidate=None)
    assert result is messages


@pytest.mark.asyncio
async def test_compress_agent_team_messages_skips_missing_tool_results():
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
    result = await compress_agent_team_messages(messages, candidate=object())
    assert result is messages
