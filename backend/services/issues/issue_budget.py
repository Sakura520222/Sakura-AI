"""Complete Issue relation request bounds using live summary model policies."""

import json
from dataclasses import dataclass
from typing import Any

from backend.core.ai_protocol.models import UnifiedMessage
from backend.core.ai_protocol.request_policy import (
    estimate_unified_messages,
    resolve_effective_request_policy,
)
from backend.core.config import get_dynamic_config
from backend.services.issues.relation_runtime import check_relation_boundary


class IssueBudgetError(ValueError):
    """An optional Issue inference lacks a known, bounded input policy."""

    def __init__(self, failure):
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class IssueInputBudget:
    max_input_tokens: int

    def messages(self, *, system_prompt, phase, output_language, current, candidates):
        payload = {
            "phase": phase,
            "output_language": output_language,
            "current": current,
            "candidates": candidates,
        }
        # Reject oversized raw strings and escaped JSON while encoding, avoiding
        # an unbounded extra copy before the shared complete-message estimator.
        max_chars = (self.max_input_tokens + 1) * 4
        remaining = max_chars

        def check(value: Any):
            nonlocal remaining
            if isinstance(value, str):
                remaining -= len(value)
            elif isinstance(value, dict):
                remaining -= len(value)
                if remaining < 0:
                    raise IssueBudgetError("input_budget")
                for key, item in value.items():
                    check(key)
                    check(item)
            elif isinstance(value, (list, tuple)):
                remaining -= len(value)
                if remaining < 0:
                    raise IssueBudgetError("input_budget")
                for item in value:
                    check(item)
            if remaining < 0:
                raise IssueBudgetError("input_budget")

        check(system_prompt)
        check(payload)
        chunks, size = [], len(system_prompt)
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(
            payload
        ):
            size += len(chunk)
            if size > max_chars:
                raise IssueBudgetError("input_budget")
            chunks.append(chunk)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "".join(chunks)},
        ]
        if (
            estimate_unified_messages([UnifiedMessage(**m) for m in messages])
            > self.max_input_tokens
        ):
            raise IssueBudgetError("input_budget")
        return messages


async def resolve_issue_input_budget(client, *, cancel_event=None, deadline=None):
    check_relation_boundary(cancel_event, deadline)
    max_input = await get_dynamic_config("issue_relation_max_input_tokens", fresh=True)
    if type(max_input) is not int or max_input <= 0:
        raise IssueBudgetError("budget_unavailable")
    check_relation_boundary(cancel_event, deadline)
    candidates = await client.resolve_role_candidates("summary")
    check_relation_boundary(cancel_event, deadline)
    if not candidates:
        raise IssueBudgetError("budget_unavailable")
    for candidate in candidates:
        # Shared policy permits legacy context lookup; optional inference must
        # have authoritative metadata for every possible fallback instead.
        if candidate.model.context_window_tokens <= 0:
            raise IssueBudgetError("budget_unavailable")
        policy = resolve_effective_request_policy(
            candidate, [], role="summary", clamp_to_context=False
        )
        max_input = min(
            max_input,
            policy.context_window_tokens
            - policy.max_output_tokens
            - policy.safety_reserve_tokens,
        )
    if max_input <= 0:
        raise IssueBudgetError("input_budget")
    return IssueInputBudget(max_input)
