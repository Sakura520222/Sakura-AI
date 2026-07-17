"""AI API客户端，封装重试机制

从原 ai_reviewer.py 的 _call_ai_with_retry 方法迁移而来 (137-248行)。
"""

import asyncio
import random
import re
import time
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from openai import BadRequestError as OpenAIBadRequestError
from loguru import logger

from backend.core.config import get_settings

from .constants import (
    DEFAULT_MAX_TOKENS,
)

# Context overflow keywords for detecting prompt-too-long errors
CONTEXT_OVERFLOW_KEYWORDS = [
    "context_length",
    "maximum context length",
    "context window",
    "reduce the length",
    "too many tokens",
    "token limit",
    "prompt exceeds max length",
    "exceeds max length",
    "prompt too long",
    "input is too long",
    "input exceeds",
]

# CJK 字符正则（用于 token 估算时判断中文等字符比例）
_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef"
    r"\u2e80-\u2eff\u31c0-\u31ef\u3200-\u32ff]"
)


class AIEmptyResponseError(Exception):
    """AI 返回空响应时抛出的异常"""


class PromptTooLongError(Exception):
    """Prompt 超出模型最大上下文长度时抛出的异常

    Attributes:
        estimated_tokens: 估算的 prompt token 数
        model: 使用的模型名称
        original_error: 原始 BadRequestError
        user_message: 面向用户的友好提示信息
    """

    def __init__(
        self,
        message: str,
        estimated_tokens: int = 0,
        model: str = "",
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.model = model
        self.original_error = original_error
        self.user_message = (
            f"审查内容超出模型上下文长度限制（模型: {model}，估算 ~{estimated_tokens} tokens），"
            "已尝试自动精简或压缩。"
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
        # 关闭 SDK 内置重试，避免与 _retry_loop 叠乘放大超时
        # （SDK 默认 max_retries=2，会使单次调用变成 3 次 HTTP 请求）
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
        # 统一多协议层入口（延迟创建）。当调用方传入 role= 参数时走统一层，
        # 否则保持旧 OpenAI SDK 路径，确保向后兼容。
        # Unified multi-protocol entry (lazy). When the caller passes role=,
        # requests route through the unified layer; otherwise the legacy
        # OpenAI SDK path is preserved for backward compatibility.
        self.base_url = base_url
        self.api_key = api_key
        self._unified_client = None  # type: Optional[Any]

    @staticmethod
    def _estimate_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
        """快速估算消息列表的 token 数量

        使用启发式方法：
        - 纯 ASCII 内容：len(content) // 4
        - 含 CJK 字符的内容：len(content) // 2（CJK 字符通常 1 个字符 ≈ 1-2 个 token）

        Args:
            messages: 消息列表

        Returns:
            估算的 token 数
        """
        estimated = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            if content:
                # 统计 CJK 字符比例
                cjk_count = len(_CJK_PATTERN.findall(content))
                total_chars = len(content)
                cjk_ratio = cjk_count / max(total_chars, 1)

                # CJK 占比高时用更保守的系数
                if cjk_ratio > 0.1:
                    estimated += total_chars // 2
                else:
                    estimated += total_chars // 4

            # 估算 tool_calls 的 token
            for tc in msg.get("tool_calls", []) or []:
                function = getattr(tc, "function", None)
                if function is None and isinstance(tc, dict):
                    function = tc.get("function")

                if isinstance(function, dict):
                    function_name = function.get("name", "")
                    function_arguments = function.get("arguments", "")
                else:
                    function_name = getattr(function, "name", "")
                    function_arguments = getattr(function, "arguments", "")

                estimated += len(str(function_name) + str(function_arguments)) // 4

        return estimated

    @staticmethod
    def _is_context_overflow_error(error: Exception) -> bool:
        """判断 BadRequestError 是否属于上下文超长错误"""
        error_str = str(error).lower()
        return any(kw in error_str for kw in CONTEXT_OVERFLOW_KEYWORDS)

    async def call_with_retry(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        role: Optional[str] = None,
        thinking: Optional[dict] = None,
        effort: Optional[str] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        **kwargs,
    ) -> Any:
        """带重试机制的AI API调用

        重试策略由 Settings 中的 AI API 调用配置控制。

        Args:
            messages: 消息列表
            model: 模型名称
            temperature: 温度参数
            tools: 工具定义列表
            tool_choice: 工具选择策略
            timeout: 单次调用超时（默认使用 Settings 中的 AI API 请求超时）
            max_tokens: 最大输出token数（默认使用 DEFAULT_MAX_TOKENS）
            role: 角色（main/summary/agent_team）；传入则走统一多协议层
            thinking: 思考参数（仅能力匹配的模型生效）
            effort: effort 参数（仅能力匹配的模型生效）
            top_p / top_k: 采样参数（按能力过滤）
            **kwargs: 其他API参数

        Returns:
            OpenAI API响应对象（统一层返回 UnifiedResponse，向后兼容
            response.choices[0].message.xxx 访问）

        Raises:
            Exception: 重试失败或超时
        """
        # 统一多协议路径：当调用方显式指定 role 时，经由 UnifiedAIClient。
        # Unified multi-protocol path: routes through UnifiedAIClient when the
        # caller explicitly passes role=.
        if role is not None:
            return await self._call_via_unified(
                messages=messages,
                model=model,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                timeout=timeout,
                max_tokens=max_tokens,
                role=role,
                thinking=thinking,
                effort=effort,
                top_p=top_p,
                top_k=top_k,
            )

        settings = get_settings()

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
        api_timeout = timeout or settings.ai_api_timeout_seconds
        api_kwargs.setdefault("timeout", api_timeout)
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
        settings = get_settings()
        max_retries = settings.ai_api_max_retries
        total_timeout = settings.ai_api_total_timeout_seconds
        start_time = time.monotonic()

        for attempt in range(max_retries):
            # 检查总超时
            elapsed = time.monotonic() - start_time
            if elapsed > total_timeout:
                logger.error(
                    "重试总超时（已耗时 {:.1f}秒 > {}秒），放弃重试",
                    elapsed,
                    total_timeout,
                )
                raise Exception(f"AI调用失败：重试总超时（{total_timeout}秒）")

            try:
                # 调用AI API
                response = await self.client.chat.completions.create(**kwargs)

                # 检查空响应
                if not self._is_valid_response(response):
                    if attempt < max_retries - 1:
                        # 重新计算已耗时，需包含本次调用真实耗时
                        elapsed = time.monotonic() - start_time
                        delay = self._calculate_delay(attempt)
                        logger.warning(
                            "AI返回空响应，{:.1f}秒后重试 ({}/{}, 已耗时 {:.1f}s)",
                            delay,
                            attempt + 1,
                            max_retries,
                            elapsed,
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error("AI返回空响应，已达最大重试次数")
                        raise Exception("AI返回空响应，已达最大重试次数")

                # 成功返回
                total_time = time.monotonic() - start_time
                logger.info(
                    "✅ AI调用成功（耗时 {:.1f}秒，重试 {} 次）",
                    total_time,
                    attempt,
                )
                return response

            except Exception as e:
                error_type = type(e).__name__

                # BadRequestError（如 prompt 超长）不应重试，包装为 PromptTooLongError
                if isinstance(e, OpenAIBadRequestError):
                    # 仅对上下文超长类错误包装为 PromptTooLongError，
                    # 避免误判其他 BadRequestError（如 schema 验证错误）
                    is_context_overflow = self._is_context_overflow_error(e)
                    if not is_context_overflow:
                        # 非超长的 BadRequestError，直接抛出原始错误
                        tool_names = [
                            tool.get("function", {}).get("name", "")
                            for tool in kwargs.get("tools", []) or []
                        ]
                        logger.error(
                            "AI调用 BadRequestError（非上下文超长）: {} | model={} tools={} "
                            "tool_choice={} max_tokens={} temperature={}",
                            str(e),
                            kwargs.get("model"),
                            tool_names,
                            kwargs.get("tool_choice"),
                            kwargs.get("max_tokens"),
                            kwargs.get("temperature"),
                        )
                        raise

                    total_time = time.monotonic() - start_time
                    # 从 kwargs 中获取 model 和 messages 用于估算 token
                    messages = kwargs.get("messages", [])
                    model = kwargs.get("model", "unknown")
                    estimated_tokens = self._estimate_prompt_tokens(messages)
                    logger.error(
                        "AI调用失败 [{}]，Prompt 超长 (估算 ~{} tokens, 模型: {}) "
                        "(总耗时 {:.1f}s): {}",
                        error_type,
                        estimated_tokens,
                        model,
                        total_time,
                        str(e),
                    )
                    raise PromptTooLongError(
                        f"Prompt exceeds max length (估算 ~{estimated_tokens} tokens, 模型: {model})",
                        estimated_tokens=estimated_tokens,
                        model=model,
                        original_error=e,
                    ) from e

                if attempt < max_retries - 1:
                    # 重新计算已耗时，需包含本次调用真实耗时
                    elapsed = time.monotonic() - start_time
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        "AI调用失败 [{}]: {}，{:.1f}秒后重试 ({}/{}, 已耗时 {:.1f}s)",
                        error_type,
                        str(e),
                        delay,
                        attempt + 1,
                        max_retries,
                        elapsed,
                    )
                    await asyncio.sleep(delay)
                else:
                    total_time = time.monotonic() - start_time
                    logger.error(
                        "AI调用失败 [{}]，已达最大重试次数 (总耗时 {:.1f}s): {}",
                        error_type,
                        total_time,
                        str(e),
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
        - 前3次：基于初始延迟快速退避
        - 后续：基于初始延迟继续慢速退避

        添加随机抖动（±20%）避免惊群效应。

        Args:
            attempt: 当前尝试次数（从0开始）

        Returns:
            延迟秒数
        """
        settings = get_settings()
        initial_delay = settings.ai_api_initial_retry_delay_seconds
        if attempt < 3:
            delay = initial_delay * (2**attempt)  # 1s, 2s, 4s
        else:
            delay = initial_delay * 8 * (2 ** (attempt - 3))  # 8s, 16s...

        # 添加随机抖动（±20%）
        jitter = random.uniform(0.8, 1.2)
        return delay * jitter

    # ------------------------------------------------------------------
    # 统一多协议路径 / Unified multi-protocol path
    # ------------------------------------------------------------------
    async def _call_via_unified(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[List[Dict]],
        tool_choice: Optional[str],
        timeout: Optional[float],
        max_tokens: Optional[int],
        role: str,
        thinking: Optional[dict],
        effort: Optional[str],
        top_p: Optional[float],
        top_k: Optional[int],
    ) -> Any:
        """经由 UnifiedAIClient 的多协议调用入口 / Unified-layer entry.

        解析角色候选链（由 ai_role_bindings + ai_provider_configs 驱动），
        若解析失败（配置未迁移）则回退到旧 OpenAI SDK 路径，保证不中断。
        Falls back to the legacy OpenAI SDK path when role resolution fails
        (e.g. config not yet migrated), ensuring no downtime.
        """
        try:
            chain = await self._resolve_role_chain(role)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "统一层角色解析失败，回退旧路径: role={} err={}", role, exc
            )
            return await self._legacy_call(
                messages=messages,
                model=model,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                timeout=timeout,
                max_tokens=max_tokens,
            )

        if chain is None or not getattr(chain, "candidates", None):
            # 配置未迁移或角色无绑定 → 回退旧路径 / not migrated → fallback
            return await self._legacy_call(
                messages=messages,
                model=model,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                timeout=timeout,
                max_tokens=max_tokens,
            )

        client = self._get_unified_client()
        response = await client.call_with_retry(
            chain,
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            thinking=thinking,
            effort=effort,
            timeout=timeout,
            role=role,
        )
        return response

    def _get_unified_client(self) -> Any:
        """延迟创建 UnifiedAIClient 单例 / Lazily create UnifiedAIClient."""
        if self._unified_client is None:
            from backend.services.ai_reviewer.unified_client import (
                FallbackConfig,
                UnifiedAIClient,
            )
            from backend.core.config import get_settings

            settings = get_settings()
            cfg = FallbackConfig(
                enabled=getattr(settings, "ai_fallback_enabled", True),
                max_candidates=int(getattr(settings, "ai_fallback_max_candidates", 3)),
                max_retries=settings.ai_api_max_retries,
                total_timeout=settings.ai_api_total_timeout_seconds,
                initial_retry_delay=settings.ai_api_initial_retry_delay_seconds,
            )
            self._unified_client = UnifiedAIClient(fallback_config=cfg)
        return self._unified_client

    async def _resolve_role_chain(self, role: str) -> Any:
        """从配置层解析角色候选链 / Resolve role chain from config layer.

        配置层（ai_role_bindings）在 PR-4 落地；在此之前返回 None，触发回退。
        The config layer (ai_role_bindings) lands in PR-4; returns None until
        then, which triggers the legacy fallback path.
        """
        try:
            from backend.core.ai_protocol.role_config import (
                resolve_role_from_config,
            )
        except Exception:  # noqa: BLE001
            return None
        return await resolve_role_from_config(role)

    async def resolve_role_model_context(
        self, role: str
    ) -> tuple[Optional[str], Optional[int]]:
        """返回角色 primary 候选的模型 ID 与上下文窗口。"""
        try:
            chain = await self._resolve_role_chain(role)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "解析角色上下文配置失败，使用旧模型上下文: role={} err={}",
                role,
                exc,
            )
            return None, None
        primary = getattr(chain, "primary", None) if chain is not None else None
        if primary is None:
            return None, None
        return primary.model.model_id, primary.model.context_window_tokens

    async def _legacy_call(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float,
        tools: Optional[List[Dict]],
        tool_choice: Optional[str],
        timeout: Optional[float],
        max_tokens: Optional[int],
    ) -> Any:
        """旧 OpenAI SDK 路径（保持原有行为）/ Legacy OpenAI SDK path."""
        settings = get_settings()
        api_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            api_kwargs["tools"] = tools
        if tool_choice:
            api_kwargs["tool_choice"] = tool_choice
        api_timeout = timeout or settings.ai_api_timeout_seconds
        api_kwargs.setdefault("timeout", api_timeout)
        api_kwargs.setdefault("max_tokens", max_tokens or DEFAULT_MAX_TOKENS)
        return await self._retry_loop(api_kwargs)
