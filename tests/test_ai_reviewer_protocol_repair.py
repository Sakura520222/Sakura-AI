"""Tests for the PR review protocol repair loop (configurable multi-round)."""

from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.result_parser import ReviewResultParser
from backend.services.ai_reviewer.reviewer import AIReviewer
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.services.ai_task_deadline import TIMEOUT_PROMPT, AITaskDeadline

VALID_REVIEW = """<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>9</SCORE>
<DECISION>approve</DECISION>
<DECISION_REASON>
No blocking defects were found.
</DECISION_REASON>
<SUMMARY>
The change is safe.
</SUMMARY>
<FINDINGS>
</FINDINGS>
</SAKURA_REVIEW>"""


class FakeApiClient:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def resolve_role_model_context(self, role):
        assert role == "main"
        return "test-model", 100_000

    async def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content, tool_calls=[])
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
        return SimpleNamespace(choices=[choice], usage=usage)


def _reviewer_with_response(content: str) -> AIReviewer:
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.result_parser = ReviewResultParser()
    reviewer.api_client = FakeApiClient(content)
    return reviewer


@pytest.mark.asyncio
async def test_invalid_response_is_repaired_once():
    reviewer = _reviewer_with_response(VALID_REVIEW)
    tracker = TokenTracker()

    result = await reviewer._parse_or_repair_review(
        "legacy free-form response",
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "standard",
        tracker,
    )

    assert result["parse_source"] == "tagged"
    assert result["ai_decision"] == "approve"
    assert len(reviewer.api_client.calls) == 1
    repair_call = reviewer.api_client.calls[0]
    assert repair_call["temperature"] == 0
    assert "tools" not in repair_call
    # 新循环保留完整 base_messages（system+user），每轮追加 assistant+user
    assert len(repair_call["messages"]) == 4
    assert repair_call["messages"][0]["role"] == "system"
    assert repair_call["messages"][1]["role"] == "user"
    assert repair_call["messages"][-2]["role"] == "assistant"
    assert repair_call["messages"][-1]["role"] == "user"
    # 错误注入到最后的 user 修复指令
    assert "Specific violation" in repair_call["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_presentation_preamble_does_not_trigger_repair():
    reviewer = _reviewer_with_response("repair must not be called")

    result = await reviewer._parse_or_repair_review(
        f"Now I have all the evidence needed.\n{VALID_REVIEW}\nReview complete.",
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "quick",
        TokenTracker(),
    )

    assert result["parse_source"] == "tagged"
    assert result["ai_decision"] == "approve"
    assert reviewer.api_client.calls == []


@pytest.mark.asyncio
async def test_second_invalid_response_falls_back_to_comment():
    reviewer = _reviewer_with_response("still invalid")

    result = await reviewer._parse_or_repair_review(
        "invalid",
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "standard",
        TokenTracker(),
    )

    assert result["parse_source"] == "protocol_error"
    assert result["overall_score"] is None
    assert result["ai_decision"] == "comment"
    assert result["issues"] == {
        "critical": [],
        "major": [],
        "minor": [],
        "suggestions": [],
    }
    # 新循环默认上限 3，fake 每轮都返回 "still invalid"，跑满 3 轮才降级
    assert len(reviewer.api_client.calls) == 3


@pytest.mark.asyncio
async def test_repair_can_discard_invalid_finding_blocks():
    reviewer = _reviewer_with_response(VALID_REVIEW)
    malformed_with_finding = """prefix
<SAKURA_REVIEW>
<FINDINGS>
<FINDING>
broken
</FINDING>
</FINDINGS>
</SAKURA_REVIEW>"""

    result = await reviewer._parse_or_repair_review(
        malformed_with_finding,
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "standard",
        TokenTracker(),
    )

    assert result["parse_source"] == "tagged"
    assert result["ai_decision"] == "approve"
    assert result["comments"] == []
    assert result["inline_comments"] == []


@pytest.mark.asyncio
async def test_repair_respects_configured_max_attempts(monkeypatch):
    """protocol_repair_max_attempts 配置覆盖默认上限 3。"""
    from backend.services.ai_reviewer import reviewer as reviewer_module

    async def _fake_get_dynamic_config(key):
        if key == "protocol_repair_max_attempts":
            return "2"
        return None

    monkeypatch.setattr(reviewer_module, "get_dynamic_config", _fake_get_dynamic_config)

    reviewer = _reviewer_with_response("still invalid")

    result = await reviewer._parse_or_repair_review(
        "invalid",
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "standard",
        TokenTracker(),
    )

    assert result["parse_source"] == "protocol_error"
    assert len(reviewer.api_client.calls) == 2  # 配置覆盖为 2


@pytest.mark.asyncio
async def test_final_assistant_turn_emitted_once():
    """final assistant turn 只推送一次（修复轮次消息除外）。"""
    events = []

    async def _capture(event_type, data):
        if event_type == "message" and data.get("role") == "assistant":
            events.append(data["content"])

    reviewer = _reviewer_with_response(VALID_REVIEW)

    result = await reviewer._parse_or_repair_review(
        "legacy free-form response",
        [{"role": "system", "content": "system"}, {"role": "user", "content": "data"}],
        "standard",
        TokenTracker(),
        event_callback=_capture,
    )

    assert result["parse_source"] == "tagged"
    # 修复轮次的 assistant 修复输出（VALID_REVIEW）推 1 次
    # final_text（"legacy free-form response"）不再由 _parse_or_repair_review 推送
    assert events == [VALID_REVIEW]


@pytest.mark.asyncio
async def test_tool_loop_uses_tool_free_final_call_after_deadline():
    reviewer = _reviewer_with_response(VALID_REVIEW)
    reviewer.enable_compression = False
    reviewer.context_compressor = SimpleNamespace(
        estimate_messages_tokens=lambda _messages: 1,
    )
    reviewer.tool_handler = SimpleNamespace()
    reviewer.model_context_mgr = SimpleNamespace(
        calculate_safe_context=lambda *_args: 100_000,
    )

    result = await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "data"},
        ],
        system_prompt="system",
        strategy="standard",
        enabled_tools=[{"type": "function", "function": {"name": "read_file"}}],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
        deadline=AITaskDeadline.from_timeout(0),
    )

    assert result["ai_decision"] == "approve"
    call = reviewer.api_client.calls[0]
    assert call["tools"] == []
    assert call["tool_choice"] == "none"
    assert sum(
        message.get("content") == TIMEOUT_PROMPT for message in call["messages"]
    ) == 1
