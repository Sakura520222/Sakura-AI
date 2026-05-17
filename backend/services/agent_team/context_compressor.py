"""Agent Team context compression for model calls."""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.core.config import get_settings
from backend.core.model_context import get_model_context_manager
from backend.services.ai_reviewer.message_utils import estimate_messages_tokens
from backend.services.agent_team.ai_client import create_agent_team_summary_client
from backend.utils.config_utils import (
    resolve_bool_config,
    resolve_float_config,
    resolve_int_config,
)
from backend.utils.message_utils import has_missing_tool_results


class AgentTeamContextCompressor:
    """Compress Agent Team messages without mutating persisted checkpoints."""

    def __init__(self, target_model: str, compressor_model: str | None = None):
        self.target_model = target_model
        self.compressor_model = compressor_model or target_model
        self.model_context_mgr = get_model_context_manager()

    async def build_model_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or has_missing_tool_results(messages):
            return messages
        if not await resolve_bool_config("agent_team_enable_context_compression", True):
            return messages

        threshold = await resolve_float_config(
            "agent_team_context_compression_threshold",
            get_settings().agent_team_context_compression_threshold,
        )
        keep_rounds = await resolve_int_config(
            "agent_team_context_compression_keep_rounds",
            get_settings().agent_team_context_compression_keep_rounds,
        )
        summary_max_tokens = await resolve_int_config(
            "agent_team_context_summary_max_tokens",
            get_settings().agent_team_context_summary_max_tokens,
        )
        safe_context = self.model_context_mgr.calculate_safe_context(
            self.target_model,
            safety_ratio=threshold,
        )
        current_tokens = estimate_messages_tokens(messages, self.model_context_mgr)
        if current_tokens <= safe_context:
            return messages

        logger.info(
            "Agent Team 上下文触发压缩: {} tokens > {} tokens (model={})",
            current_tokens,
            safe_context,
            self.target_model,
        )
        return await self.compress_messages(messages, keep_rounds, summary_max_tokens)

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        keep_rounds: int,
        summary_max_tokens: int,
    ) -> list[dict[str, Any]]:
        system_msg, body = _split_system_message(messages)
        blocks = _split_message_blocks(body)
        if len(blocks) <= keep_rounds:
            return messages

        early_blocks = blocks[:-keep_rounds]
        keep_blocks = blocks[-keep_rounds:]
        early_messages = [message for block in early_blocks for message in block]
        kept_messages = [message for block in keep_blocks for message in block]

        try:
            summary = await self._summarize_early_messages(
                early_messages,
                summary_max_tokens,
            )
        except Exception as exc:
            logger.warning("Agent Team 上下文压缩失败，回退保留最近块: {}", exc)
            return _clean_messages(([system_msg] if system_msg else []) + kept_messages)

        compressed = []
        if system_msg:
            compressed.append(system_msg)
        compressed.append(
            {
                "role": "user",
                "content": "## 已压缩的 Agent 历史上下文\n" + summary,
            }
        )
        compressed.extend(kept_messages)
        return _clean_messages(compressed)

    async def _summarize_early_messages(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str:
        client, summary_model, config = await create_agent_team_summary_client()
        compressor_model = self.compressor_model or summary_model or self.target_model
        prompt = _build_compression_prompt(messages, max_tokens)
        response = await client.call_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": "你负责压缩 Agent 专家团队执行历史，保留任务执行所需事实。",
                },
                {"role": "user", "content": prompt},
            ],
            model=compressor_model,
            temperature=0.2,
            timeout=config.timeout_seconds,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()


def _split_system_message(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if messages and messages[0].get("role") == "system":
        return messages[0], messages[1:]
    return None, messages


def _split_message_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    blocks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        block = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            tool_call_ids = {
                str(item.get("id")) for item in message.get("tool_calls") or []
            }
            index += 1
            while index < len(messages):
                next_message = messages[index]
                if (
                    next_message.get("role") == "tool"
                    and str(next_message.get("tool_call_id")) in tool_call_ids
                ):
                    block.append(next_message)
                    index += 1
                    continue
                break
            blocks.append(block)
            continue
        blocks.append(block)
        index += 1
    return blocks


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


def _build_compression_prompt(messages: list[dict[str, Any]], max_tokens: int) -> str:
    text = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content") or ""
        if message.get("tool_calls"):
            calls = []
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                calls.append(
                    f"- {function.get('name', '')}: {function.get('arguments', '')}"
                )
            content = "工具调用:\n" + "\n".join(calls)
        text.append(f"## {role}\n{content}")

    return f"""请将以下 Agent 专家团队历史压缩到 {max_tokens} tokens 以内。

必须保留：
- 原始任务目标和关键约束
- 已读取/分析过的关键文件与结论
- 已修改的文件、修改意图和风险
- 已运行的命令或测试结果
- 专业审查提出的问题、建议和未解决事项
- 下一步执行应继续关注的事项

不要输出无关寒暄，不要编造未发生的修改。

## 历史消息

{chr(10).join(text)}
"""
