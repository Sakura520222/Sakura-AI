"""AI API客户端，封装重试机制

从原 ai_reviewer.py 的 _call_ai_with_retry 方法迁移而来 (137-248行)。
"""

import asyncio
import random
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from loguru import logger

from .constants import (
    DEFAULT_API_TIMEOUT,
    DEFAULT_MAX_TOKENS,
    INITIAL_DELAY,
    MAX_RETRIES,
    TOTAL_TIMEOUT,
)


class AIApiClient:
    """AI API客户端，负责与OpenAI兼容API交互

    封装了：
    - 带重试机制的API调用
    - 混合退避策略（前3次快速，后续慢速）
    - 空响应检测和处理
    - 总超时控制
    """

    def __init__(self, base_url: str, api_key: str):
        """初始化API客户端

        Args:
            base_url: API基础URL
            api_key: API密钥
        """
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def call_with_retry(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> Any:
        """带重试机制的AI API调用

        重试策略：
        - 前3次：快速重试（1s, 2s, 4s）
        - 后续次数：慢速重试（8s, 16s, 32s...）
        - 总超时：15分钟

        Args:
            messages: 消息列表
            model: 模型名称
            temperature: 温度参数
            tools: 工具定义列表
            tool_choice: 工具选择策略
            timeout: 单次调用超时（默认使用 DEFAULT_API_TIMEOUT）
            max_tokens: 最大输出token数（默认使用 DEFAULT_MAX_TOKENS）
            **kwargs: 其他API参数

        Returns:
            OpenAI API响应对象

        Raises:
            Exception: 重试失败或超时
        """
        # 准备API参数
        api_kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            api_kwargs["tools"] = tools
        if tool_choice:
            api_kwargs["tool_choice"] = tool_choice

        # 设置默认值
        api_kwargs.setdefault("timeout", timeout or DEFAULT_API_TIMEOUT)
        api_kwargs.setdefault("max_tokens", max_tokens or DEFAULT_MAX_TOKENS)

        # 合并额外参数
        api_kwargs.update(kwargs)

        # 执行重试循环
        return await self._retry_loop(api_kwargs)

    async def _retry_loop(self, kwargs: Dict) -> Any:
        """重试循环逻辑

        Args:
            kwargs: API调用参数

        Returns:
            API响应

        Raises:
            Exception: 重试失败或超时
        """
        start_time = asyncio.get_event_loop().time()

        for attempt in range(MAX_RETRIES):
            # 检查总超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > TOTAL_TIMEOUT:
                logger.error(
                    f"重试总超时（已耗时 {elapsed:.1f}秒 > {TOTAL_TIMEOUT}秒），放弃重试"
                )
                raise Exception(f"AI调用失败：重试总超时（{TOTAL_TIMEOUT}秒）")

            try:
                # 调用AI API
                response = await self.client.chat.completions.create(**kwargs)

                # 检查空响应
                if not self._is_valid_response(response):
                    if attempt < MAX_RETRIES - 1:
                        delay = self._calculate_delay(attempt)
                        logger.warning(
                            f"AI返回空响应，{delay:.1f}秒后重试 "
                            f"({attempt + 1}/{MAX_RETRIES}, 已耗时 {elapsed:.1f}s)"
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error("AI返回空响应，已达最大重试次数")
                        raise Exception("AI返回空响应，已达最大重试次数")

                # 成功返回
                total_time = asyncio.get_event_loop().time() - start_time
                logger.info(f"✅ AI调用成功（耗时 {total_time:.1f}秒，重试 {attempt} 次）")
                return response

            except Exception as e:
                error_type = type(e).__name__
                if attempt < MAX_RETRIES - 1:
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        f"AI调用失败 [{error_type}]: {e}，{delay:.1f}秒后重试 "
                        f"({attempt + 1}/{MAX_RETRIES}, 已耗时 {elapsed:.1f}s)"
                    )
                    await asyncio.sleep(delay)
                else:
                    total_time = asyncio.get_event_loop().time() - start_time
                    logger.error(
                        f"AI调用失败 [{error_type}]，已达最大重试次数 "
                        f"(总耗时 {total_time:.1f}s): {e}"
                    )
                    raise

    def _is_valid_response(self, response: Any) -> bool:
        """验证响应是否有效

        Args:
            response: API响应

        Returns:
            响应是否有效
        """
        if not response.choices:
            return False

        msg = response.choices[0].message
        has_content = bool(msg.content)
        has_tool_calls = bool(getattr(msg, "tool_calls", None))

        logger.debug(f"AI响应状态: content={has_content}, tool_calls={has_tool_calls}")
        return has_content or has_tool_calls

    def _calculate_delay(self, attempt: int) -> float:
        """计算重试延迟时间

        使用混合退避策略：
        - 前3次：快速退避 (1s, 2s, 4s)
        - 后续：慢速退避 (8s, 16s, 32s...)

        添加随机抖动（±20%）避免惊群效应。

        Args:
            attempt: 当前尝试次数（从0开始）

        Returns:
            延迟秒数
        """
        if attempt < 3:
            delay = INITIAL_DELAY * (2**attempt)  # 1s, 2s, 4s
        else:
            delay = 8 * (2 ** (attempt - 3))  # 8s, 16s...

        # 添加随机抖动（±20%）
        jitter = random.uniform(0.8, 1.2)
        return delay * jitter
