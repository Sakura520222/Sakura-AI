"""Deterministic evidence retention for Agent context compaction.

The durable, currently projected history is the source of truth. Model summaries
cannot decide that a task, user instruction or an unresolved tool failure is gone.
This module neither grants capabilities nor changes the durable conversation.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from typing import Any

from backend.core.ai_protocol.models import UnifiedMessage
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import messages_from_legacy


def message_sha256(message: UnifiedMessage) -> str:
    serialized = json.dumps(asdict(message), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def history_sha256(messages: list[UnifiedMessage]) -> str:
    return hashlib.sha256(
        "".join(message_sha256(message) for message in messages).encode("ascii")
    ).hexdigest()


def _is_guidance(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") or {}
    return bool(
        message.get("guidance_ids")
        or message.get("prompt_ids")
        or metadata.get("guidance_ids")
        or metadata.get("prompt_ids")
        or str(message.get("content") or "").startswith("[human_guidance:")
    )


def is_runtime_notice(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") or {}
    return any(
        metadata.get(name)
        for name in (
            "repository_context",
            "context_compaction",
            "harness_event",
            "completion_reminder",
            "strategy_self_check",
        )
    )


class CompactionEvidence:
    """Retain the task, user guidance and complete actionable tool exchanges.

    "Recent" means the latest complete tool-call batch, not a configurable
    history quota. Failures remain unresolved until the *same* tool and
    normalized arguments return a successful JSON result, including a successful
    process outcome for Shell. Other successful
    calls never erase a failure. Every call/result span stays indivisible.
    """

    def __init__(self, messages: list[dict[str, Any]]):
        self.messages = messages_from_legacy(messages)
        self.categories: dict[str, set[int]] = {
            name: set()
            for name in (
                "system",
                "active_task",
                "human_guidance",
                "current_context",
                "recent_tools",
                "unresolved_errors",
            )
        }
        for index, message in enumerate(messages):
            if message.get("role") == "system":
                self.categories["system"].add(index)
            if message.get("role") != "user":
                continue
            metadata = message.get("metadata") or {}
            if is_runtime_notice(message):
                continue
            if metadata.get("compaction_current_context"):
                self.categories["current_context"].add(index)
                continue
            category = (
                "active_task"
                if not self.categories["active_task"] and not _is_guidance(message)
                else "human_guidance"
            )
            self.categories[category].add(index)

        # Reuse the shared protocol grouping, including interleaved results.
        blocks = UnifiedContextCompressor._split_message_blocks(self.messages)
        spans: dict[int, set[int]] = {}
        offset = 0
        for block in blocks:
            indices = set(range(offset, offset + len(block)))
            for index in indices:
                spans[index] = indices
            if any(message.tool_calls for message in block):
                self.categories["recent_tools"] = indices
            offset += len(block)

        calls = {}
        unresolved: dict[tuple[str, str], set[int]] = {}
        for index, message in enumerate(self.messages):
            for call in message.tool_calls or []:
                try:
                    arguments = json.dumps(
                        json.loads(call.arguments), sort_keys=True, ensure_ascii=False
                    )
                except TypeError, ValueError:
                    arguments = call.arguments
                calls[call.id] = (call.name, arguments)
            if message.role != "tool" or message.tool_call_id not in calls:
                continue
            try:
                result = json.loads(message.content or "")
            except TypeError, ValueError:
                # An unparseable observation is not proof of a successful retry.
                continue
            key = calls[message.tool_call_id]
            failed = isinstance(result, dict) and (
                "error" in result
                or result.get("success") is False
                or result.get("isError") is True
                or result.get("timed_out") is True
                or (type(result.get("returncode")) is int and result["returncode"] != 0)
            )
            if failed:
                unresolved.setdefault(key, set()).add(index)
            else:
                unresolved.pop(key, None)
        for failures in unresolved.values():
            for index in failures:
                self.categories["unresolved_errors"].update(spans[index])

        # User guidance can arrive inside a tool span. Retain the whole span
        # instead of moving that instruction across a partially retained call.
        for category in self.categories.values():
            expanded = set(category)
            for index in category:
                expanded.update(spans[index])
            category.update(expanded)

    def restore(self, summary: UnifiedMessage) -> list[UnifiedMessage]:
        retained = set().union(*self.categories.values())
        system = self.categories["system"]
        # Keep trusted system messages verbatim. The generated summary is user
        # data, and all original evidence retains its original message role.
        return (
            [self.messages[index] for index in sorted(system)]
            + [summary]
            + [self.messages[index] for index in sorted(retained - system)]
        )

    def audit_retention(self, output: list[UnifiedMessage]) -> dict[str, Any]:
        output_hashes = Counter(message_sha256(message) for message in output)
        return {
            category: {
                "required": len(indices),
                "verified": sum(
                    (
                        Counter(
                            message_sha256(self.messages[index]) for index in indices
                        )
                        & output_hashes
                    ).values()
                ),
                "source_indices": sorted(indices),
                "message_sha256": [
                    message_sha256(self.messages[index]) for index in sorted(indices)
                ],
            }
            for category, indices in self.categories.items()
        }
