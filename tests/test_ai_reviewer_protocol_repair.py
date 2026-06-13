"""Tests for the one-shot PR review protocol repair flow."""

from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.result_parser import ReviewResultParser
from backend.services.ai_reviewer.reviewer import AIReviewer
from backend.services.ai_reviewer.token_tracker import TokenTracker


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

    async def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content)
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
    assert repair_call["messages"][-2]["role"] == "assistant"
    assert repair_call["messages"][-1]["role"] == "user"


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


@pytest.mark.asyncio
async def test_tool_loop_maximum_round_exit_uses_tagged_parser(monkeypatch):
    reviewer = _reviewer_with_response(VALID_REVIEW)
    reviewer.tool_handler = object()
    reviewer.model_context_mgr = SimpleNamespace(
        calculate_safe_context=lambda model, threshold: 100_000
    )
    reviewer.enable_compression = False

    strategy_config = SimpleNamespace(
        get_context_enhancement_config=lambda: {"max_tool_iterations": 0}
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.reviewer.get_strategy_config",
        lambda: strategy_config,
    )

    result = await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "evidence"},
        ],
        system_prompt="system",
        strategy="standard",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
    )

    assert result["parse_source"] == "tagged"
    assert result["ai_decision"] == "approve"
    assert len(reviewer.api_client.calls) == 1
