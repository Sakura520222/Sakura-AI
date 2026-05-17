"""Shared message utility functions for Agent Team tool call handling.

Consolidates duplicated tool-call serialization and reconstruction logic
previously scattered across fullstack_expert.py, professional_reviewer.py,
and context_compressor.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from backend.services.agent_team.tools.base import ToolResult


def tool_call_to_dict(tc: Any) -> dict[str, Any]:
    """Convert an OpenAI SDK tool call object to a JSON-serializable dict."""
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }


def tool_call_from_dict(data: dict[str, Any]) -> Any:
    """Reconstruct a tool-call-like object from a persisted dict."""
    function = data.get("function") or {}
    return SimpleNamespace(
        id=data.get("id", ""),
        function=SimpleNamespace(
            name=function.get("name", ""),
            arguments=function.get("arguments", ""),
        ),
    )


def get_missing_tool_calls(messages: list[dict[str, Any]]) -> list[Any]:
    """Return pending tool calls from the last assistant message that lack results.

    Scans messages in reverse so only the most recent unresolved assistant
    message is considered, which is the correct semantic for tool execution.
    """
    completed = {
        item.get("tool_call_id")
        for item in messages
        if item.get("role") == "tool" and item.get("tool_call_id")
    }
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        missing = [
            tool_call_from_dict(item)
            for item in tool_calls
            if item.get("id") not in completed
        ]
        if missing:
            return missing
    return []


def has_missing_tool_results(messages: list[dict[str, Any]]) -> bool:
    """Check whether any tool calls in the conversation lack result messages."""
    return bool(get_missing_tool_calls(messages))


def serialize_tool_result(result: ToolResult) -> str:
    """Serialize a ToolResult to JSON string for message content."""
    if result.success:
        return json.dumps(result.output, ensure_ascii=False, default=str)
    return json.dumps({"error": result.error}, ensure_ascii=False)
