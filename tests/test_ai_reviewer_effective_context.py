"""Winner-specific compressed history application in the reviewer tool loop."""

from types import SimpleNamespace

import pytest

from backend.core.ai_protocol.models import (
    ModelCapabilitySet,
    UnifiedMessage,
)
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
The change is safe.
</SUMMARY>
<FINDINGS>
</FINDINGS>
</SAKURA_REVIEW>"""


class _CompressingApiClient:
    def __init__(self):
        self.calls = []

    async def resolve_role_model_context(self, role):
        return "winner-model", 100_000

    async def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"file_path":"a.py"}',
                        ),
                        id="call-1",
                    )
                ],
                reasoning_content=None,
            )
            meta = SimpleNamespace(
                served_capabilities=ModelCapabilitySet(),
                effective_messages=[
                    UnifiedMessage(role="system", content="current system"),
                    UnifiedMessage(role="user", content="compressed evidence"),
                ],
            )
        else:
            message = SimpleNamespace(
                content=VALID_REVIEW,
                tool_calls=[],
                reasoning_content=None,
            )
            meta = SimpleNamespace(served_capabilities=ModelCapabilitySet())
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
            meta=meta,
        )


class _FakeToolHandler:
    async def handle_tool_call(self, tool_call, repo, pr):
        return {"content": "tool result"}


@pytest.mark.asyncio
async def test_tool_loop_applies_winner_effective_messages_before_new_turn():
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.api_client = _CompressingApiClient()
    reviewer.result_parser = ReviewResultParser()
    reviewer.tool_handler = _FakeToolHandler()

    result = await reviewer._run_tool_loop(
        messages=[
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "old full evidence"},
        ],
        system_prompt="current system",
        strategy="standard",
        enabled_tools=[
            {"type": "function", "function": {"name": "read_file"}}
        ],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
    )

    assert result["ai_decision"] == "approve"
    second_messages = reviewer.api_client.calls[1]["messages"]
    assert [message["content"] for message in second_messages[:2]] == [
        "current system",
        "compressed evidence",
    ]
    assert second_messages[2]["role"] == "assistant"
    assert second_messages[2]["tool_calls"][0]["id"] == "call-1"
    assert second_messages[3] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"content": "tool result"}',
    }
    assert not any(message.get("content") == "old full evidence" for message in second_messages)
