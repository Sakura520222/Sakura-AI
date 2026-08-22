"""Reusable soft deadline state and pre-call policy tests."""

from backend.services import ai_task_deadline as deadline_module
from backend.services.ai_task_deadline import (
    TIMEOUT_PROMPT,
    AITaskDeadline,
)


def test_deadline_uses_monotonic_clock_and_switches_once(monkeypatch):
    now = 100.0
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: now)

    deadline = AITaskDeadline.from_timeout(5)
    messages = [{"role": "user", "content": "initial"}]

    assert deadline.is_expired() is False
    assert deadline.remaining() == 5.0
    assert deadline.prepare_call(messages) == {}

    now = 105.0
    policy = deadline.prepare_call(messages)

    assert deadline.is_expired() is True
    assert policy == {"tools": [], "tool_choice": "none"}
    assert messages[-1] == {"role": "user", "content": TIMEOUT_PROMPT}
    assert deadline.timeout_prompt_sent is True
    assert deadline.tools_disabled is True

    second_policy = deadline.prepare_call(messages)
    assert second_policy == {"tools": [], "tool_choice": "none"}
    assert [message["content"] for message in messages].count(TIMEOUT_PROMPT) == 1


def test_zero_timeout_is_already_expired():
    deadline = AITaskDeadline.from_timeout(0)
    messages = []

    assert deadline.is_expired() is True
    assert deadline.prepare_call(messages) == {
        "tools": [],
        "tool_choice": "none",
    }
    assert len(messages) == 1
