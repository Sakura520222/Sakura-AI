"""上下文压缩模块

从原 ai_reviewer.py 迁移的上下文压缩相关方法：
- _compress_conversation_history (1971-2138行)
- _estimate_messages_tokens (1942-1969行)
- _find_tool_result (2146-2164行)
- _clean_message_for_model (2166-2188行)
- _fallback_simplify_messages_full (2190-2238行)
"""

import json
from typing import Any, Dict, List

from loguru import logger

from backend.core.config import get_strategy_config
from backend.core.model_context import get_model_context_manager
from backend.services.ai_reviewer.constants import DEFAULT_COMPRESSION_KEEP_ROUNDS


class ContextCompressor:
    """上下文压缩器

    负责在对话历史过长时进行智能压缩，保留关键信息。
    """

    def __init__(self, api_client, model, keep_rounds=DEFAULT_COMPRESSION_KEEP_ROUNDS):
        """初始化上下文压缩器

        Args:
            api_client: AI API 客户端
            model: 模型名称
            keep_rounds: 保留的对话轮数
        """
        self.api_client = api_client
        self.model = model
        self.keep_rounds = keep_rounds
        self.model_context_mgr = get_model_context_manager()

    async def compress_conversation_history(
        self, messages: List[Dict[str, Any]], system_prompt: str, max_tokens: int
    ) -> List[Dict[str, Any]]:
        """智能压缩对话历史，保留工具调用的完整性

        压缩策略：
        1. 识别并保留最近 N 轮完整的工具调用链路
        2. 压缩更早的对话历史为摘要
        3. 确保消息结构完整，兼容所有模型

        Args:
            messages: 当前的消息列表
            system_prompt: 系统提示词
            max_tokens: 压缩后的最大 token 数

        Returns:
            压缩后的消息列表
        """
        try:
            logger.info(
                f"🗜️  开始压缩对话历史，当前大小: {self.estimate_messages_tokens(messages)} tokens"
            )

            # 1. 分离消息：保留最近几轮工具调用，压缩更早的历史
            compressed_messages = []

            # 保留 system 消息
            system_msg = (
                messages[0]
                if messages and messages[0].get("role") == "system"
                else None
            )
            if system_msg:
                compressed_messages.append(system_msg)

            # 2. 从后向前扫描，保留最近 N 轮完整的工具调用
            tool_call_rounds = []
            current_round = []

            for msg in reversed(messages[1:]):  # 跳过 system，倒序扫描
                current_round.insert(0, msg)  # 保持原始顺序

                # 如果是 assistant 消息且有 tool_calls，说明一轮结束
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    tool_call_rounds.insert(0, current_round)
                    current_round = []

                    # 如果已收集足够的轮次，停止
                    if len(tool_call_rounds) >= self.keep_rounds:
                        break

            # 处理剩余的未闭合消息
            if current_round:
                if tool_call_rounds:
                    tool_call_rounds[0] = current_round + tool_call_rounds[0]
                else:
                    tool_call_rounds.append(current_round)

            # 3. 提取需要压缩的早期历史
            early_history = []
            total_kept = sum(len(round) for round in tool_call_rounds)

            if total_kept < len(messages) - (1 if system_msg else 0):
                # 有需要压缩的早期历史
                early_end_idx = len(messages) - total_kept - (1 if system_msg else 0)
                if early_end_idx > 0:
                    early_history = messages[1 if system_msg else 0 : early_end_idx + 1]

            # 4. 如果有早期历史，进行压缩
            if early_history:
                compressed_summary = await self._compress_early_history(
                    early_history, max_tokens
                )

                compressed_messages.append(
                    {"role": "user", "content": compressed_summary}
                )

                # 添加保留的工具调用轮次
                for round_msgs in tool_call_rounds:
                    compressed_messages.extend(round_msgs)

                logger.info(
                    f"✅ 压缩完成: "
                    f"{self.estimate_messages_tokens(messages)} → "
                    f"{self.estimate_messages_tokens(compressed_messages)} tokens "
                    f"(保留了 {len(tool_call_rounds)} 轮工具调用)"
                )
            else:
                # 没有早期历史需要压缩，直接返回保留的消息
                for round_msgs in tool_call_rounds:
                    compressed_messages.extend(round_msgs)

                logger.info(
                    f"ℹ️  无需压缩，仅保留最近 {len(tool_call_rounds)} 轮工具调用"
                )

            return compressed_messages

        except Exception as e:
            logger.error(f"压缩对话历史失败: {e}", exc_info=True)
            logger.warning("压缩失败，回退到简化模式")
            return self._fallback_simplify_messages_full(messages, system_prompt)

    async def _compress_early_history(
        self, early_history: List[Dict], max_tokens: int
    ) -> str:
        """压缩早期对话历史

        Args:
            early_history: 早期消息列表
            max_tokens: 最大token数

        Returns:
            压缩后的摘要
        """
        # 构建待压缩的文本
        conversation_text = ""
        for msg in early_history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            if tool_calls:
                conversation_text += f"\n## {role.upper()} (工具调用)\n"
                for tc in tool_calls:
                    func = tc.function
                    conversation_text += f"- 调用工具: {func.name}\n"
                    conversation_text += f"- 参数: {func.arguments}\n"
                    # 查找对应的工具结果
                    tool_result = self._find_tool_result_in_history(
                        early_history, tc.id
                    )
                    if tool_result:
                        conversation_text += f"- 结果: {str(tool_result)[:200]}...\n"
            elif content:
                conversation_text += f"\n## {role.upper()}\n{content}\n"

        # 构建压缩 prompt
        compress_prompt = f"""请将以下代码审查对话历史压缩为 {max_tokens} tokens 以内的精简摘要。

## 压缩要求

1. **保留关键信息**：
   - 所有已发现的代码问题（按严重程度：critical/major/minor/suggestions）
   - 所有行内评论的位置（文件路径:行号）和内容
   - 重要工具调用的结果（文件内容、目录结构）
   - 当前审查的进度

2. **移除冗余**：
   - 重复的对话轮次
   - 冗余的工具调用详情
   - 已处理完成的问题

3. **保持结构**：
   - 保持与原始 user_message 相同的格式
   - 确保 PR 信息、文件信息等结构完整
   - 行内评论格式：`### 🔴 文件路径:行号`

## 对话历史

{conversation_text}

请输出压缩后的摘要（保持与原始 PR 审查上下文相同的格式）。
"""

        # 调用 AI 压缩
        response = await self.api_client.call_with_retry(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是代码审查助手，擅长精简和总结对话历史。",
                },
                {"role": "user", "content": compress_prompt},
            ],
            temperature=0.3,
            timeout=60.0,
            max_tokens=max_tokens,
        )

        return response.choices[0].message.content.strip()

    def estimate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """估算消息列表的 token 数量

        Args:
            messages: 消息列表

        Returns:
            估算的 token 数量
        """
        total_tokens = 0

        for message in messages:
            content = message.get("content", "")
            if content:
                total_tokens += self.model_context_mgr.estimate_tokens(content)

            # 估算 tool_calls 的 token
            tool_calls = message.get("tool_calls")
            if tool_calls:
                for tool_call in tool_calls:
                    function = tool_call.function
                    total_tokens += self.model_context_mgr.estimate_tokens(
                        function.name + str(function.arguments)
                    )

        return total_tokens

    def _find_tool_result_in_history(
        self, messages: List[Dict], tool_call_id: str
    ) -> Any:
        """查找工具调用的结果

        Args:
            messages: 消息列表
            tool_call_id: 工具调用ID

        Returns:
            工具结果内容
        """
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                try:
                    return json.loads(msg.get("content", "{}"))
                except json.JSONDecodeError:
                    return msg.get("content", "")
        return None

    def _fallback_simplify_messages_full(
        self, messages: List[Dict[str, Any]], system_prompt: str
    ) -> List[Dict[str, Any]]:
        """压缩失败时的完整简化后备方案

        Args:
            messages: 消息列表
            system_prompt: 系统提示词

        Returns:
            简化后的消息列表
        """
        logger.warning("使用完整简化模式，仅保留最近2轮工具调用")

        result = []
        system_msg = (
            messages[0] if messages and messages[0].get("role") == "system" else None
        )
        if system_msg:
            result.append(self._clean_message_for_model(system_msg))

        # 从后向前保留最近2轮工具调用
        keep_rounds = 2
        tool_call_rounds = []
        current_round = []

        for msg in reversed(messages[1:]):
            current_round.insert(0, msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_call_rounds.insert(0, current_round)
                current_round = []

                if len(tool_call_rounds) >= keep_rounds:
                    break

        if current_round:
            if tool_call_rounds:
                tool_call_rounds[0] = current_round + tool_call_rounds[0]
            else:
                tool_call_rounds.append(current_round)

        # 添加保留的工具调用轮次
        for round_msgs in tool_call_rounds:
            for msg in round_msgs:
                result.append(self._clean_message_for_model(msg))

        return result

    def _clean_message_for_model(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """清理消息中当前模型不支持的字段

        Args:
            message: 原始消息

        Returns:
            清理后的消息
        """
        cleaned_msg = message.copy()

        # 检查当前模型是否支持 reasoning_content
        strategy_config = get_strategy_config()
        supports_reasoning = strategy_config.is_model_supports_reasoning_content(
            self.model
        )

        if not supports_reasoning and "reasoning_content" in cleaned_msg:
            del cleaned_msg["reasoning_content"]
            logger.debug("移除不兼容的 reasoning_content 字段")

        return cleaned_msg
