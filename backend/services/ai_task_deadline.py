"""Monotonic, soft deadlines shared by AI task call loops.

The deadline is deliberately a local coordination primitive.  It never cancels
an in-flight provider request; callers use :meth:`prepare_call` immediately
before starting the next request and switch that request to a final,
tool-free response once the deadline has elapsed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

TIMEOUT_PROMPT = (
    "Task deadline reached. Do not call any tools. Based only on the information "
    "already present in this conversation, provide the final answer now and "
    "follow the required structured output format exactly.\n\n"
    "任务软超时已到达。不得再调用任何工具；仅基于当前累计信息直接给出最终回答，"
    "并严格遵循既定的结构化输出格式。"
)


@dataclass
class AITaskDeadline:
    """State machine for a task-wide soft AI deadline.

    ``deadline`` is an absolute ``time.monotonic()`` value.  The object starts
    in the active state, moves to ``tools_disabled`` when the deadline is first
    observed, and remains there for all subsequent calls.  The timeout prompt
    is guarded by state and by its exact message content so repeated pre-call
    checks are idempotent.
    """

    deadline: float
    timeout_prompt_sent: bool = False
    tools_disabled: bool = False

    @classmethod
    def from_timeout(cls, timeout_seconds: float | None) -> AITaskDeadline:
        """Create a deadline from a duration measured with ``monotonic``.

        ``None`` means no configured deadline and is represented by positive
        infinity.  Zero and negative durations are useful in tests and mean
        that the task is already expired at its first pre-call check.
        """
        if timeout_seconds is None:
            return cls(deadline=math.inf)
        return cls(deadline=time.monotonic() + max(0.0, float(timeout_seconds)))

    def is_expired(self) -> bool:
        """Return whether the monotonic deadline has been reached."""
        return time.monotonic() >= self.deadline

    @property
    def expired(self) -> bool:
        """Property alias useful to callers that prefer state-style access."""
        return self.is_expired()

    def remaining(self) -> float:
        """Return remaining monotonic seconds, clamped at zero."""
        return max(0.0, self.deadline - time.monotonic())

    def ensure_timeout_prompt(self, messages: list[dict[str, Any]]) -> bool:
        """Append the timeout user message once and disable tools forever.

        Returns ``True`` only when this call appended the prompt.  The content
        check also protects callers that reconstruct a message list around the
        same deadline object, while the state flag is the primary idempotency
        guard for normal cumulative histories.
        """
        self.tools_disabled = True
        if self.timeout_prompt_sent:
            return False

        already_present = any(
            message.get("role") == "user"
            and message.get("content") == TIMEOUT_PROMPT
            for message in messages
        )
        if not already_present:
            messages.append({"role": "user", "content": TIMEOUT_PROMPT})
            appended = True
        else:
            appended = False
        self.timeout_prompt_sent = True
        return appended

    def prepare_call(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Return provider kwargs for the next AI call.

        Before expiry the return value is empty, preserving the existing call
        contract.  At or after expiry the timeout prompt is appended once and
        explicit ``tools=[]`` / ``tool_choice='none'`` kwargs are returned.
        Once the final-only state is entered, every later call receives the
        same tool-free policy even if a caller checks the object again without
        advancing the clock.
        """
        if self.tools_disabled or self.is_expired():
            self.ensure_timeout_prompt(messages)
            return {"tools": [], "tool_choice": "none"}
        return {}
