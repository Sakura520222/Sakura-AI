from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.result_parser import ReviewResultParser
from backend.services.ai_reviewer.reviewer import AIReviewer
from backend.services.ai_reviewer.token_tracker import TokenTracker


VALID_REVIEW = """<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>8</SCORE>
<DECISION>approve</DECISION>
<DECISION_REASON>
No blocking defects were found.
</DECISION_REASON>
<SUMMARY>
The incremental change is safe.
</SUMMARY>
<FINDINGS>
</FINDINGS>
</SAKURA_REVIEW>"""


class _FakeApiClient:
    def __init__(self):
        self.calls = []

    async def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=VALID_REVIEW, tool_calls=[])
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
        return SimpleNamespace(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_tool_loop_appends_pending_user_message_before_model_request(
    monkeypatch,
):
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.api_client = _FakeApiClient()
    reviewer.result_parser = ReviewResultParser()
    reviewer.tool_handler = object()
    reviewer.model_context_mgr = SimpleNamespace(
        calculate_safe_context=lambda model, threshold: 100_000
    )
    reviewer.enable_compression = False

    strategy_config = SimpleNamespace(
        get_context_enhancement_config=lambda: {"max_tool_iterations": 1}
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.reviewer.get_strategy_config",
        lambda: strategy_config,
    )

    callback_calls = 0

    async def pending_callback():
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            return {"role": "user", "content": "incremental diff"}
        return None

    events = []

    async def event_callback(event_type, data):
        events.append((event_type, data))

    result = await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "initial evidence"},
        ],
        system_prompt="system",
        strategy="standard",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
        event_callback=event_callback,
        pending_user_message_callback=pending_callback,
    )

    assert result["ai_decision"] == "approve"
    assert callback_calls == 1
    assert reviewer.api_client.calls[0]["messages"][-1] == {
        "role": "user",
        "content": "incremental diff",
    }
    assert ("message", {"role": "user", "content": "incremental diff"}) in events
