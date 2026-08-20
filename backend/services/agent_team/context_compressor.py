"""Agent Team context compression — thin bridge to UnifiedContextCompressor."""

from __future__ import annotations

from typing import Any

from backend.core.ai_protocol.models import UnifiedMessage
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import messages_from_legacy
from backend.utils.message_utils import has_missing_tool_results


async def compress_agent_team_messages(
    messages: list[dict[str, Any]],
    *,
    candidate: Any,
    token_tracker: Any | None = None,
) -> list[dict[str, Any]]:
    """使用 UnifiedContextCompressor 压缩 Agent Team 消息，返回 dict 列表。"""
    if not candidate or not messages or has_missing_tool_results(messages):
        return messages

    # 每次从 Settings 现取配置（支持运行时刷新），用完即释放惰性 HTTP 客户端。
    compressor = UnifiedContextCompressor.from_settings()
    try:
        compressed, result = await compressor.maybe_compress(
            candidate, messages_from_legacy(messages), tracker=token_tracker
        )
    finally:
        await compressor.aclose()
    if not compressed:
        return messages
    return _from_unified_messages(result)


def _from_unified_messages(messages: list[UnifiedMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        d: dict[str, Any] = {"role": msg.role}
        if msg.content is not None:
            d["content"] = msg.content
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        result.append(d)
    return result


__all__ = ["compress_agent_team_messages"]
