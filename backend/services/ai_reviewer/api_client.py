"""统一 AI 调用门面 / Unified AI client facade.

所有调用都必须显式指定角色；模型、端点、凭据和故障转移候选链只从
ai_account.* 与 ai_role_bindings 解析。旧 OpenAI SDK 与扁平配置不再参与运行时。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.core.ai_protocol.errors import AllCandidatesFailedError


class AIEmptyResponseError(Exception):
    """AI 返回空响应时抛出的异常 / Raised when AI returns an empty response."""


class PromptTooLongError(Exception):
    """保留旧异常类型，供上层兼容捕获 / Compatibility exception for callers."""

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
    """角色驱动的统一 AI 调用门面 / Role-driven unified AI facade.

    调用方只能传递 role 和请求参数；UnifiedAIClient 负责从账号、角色绑定和
    单模型覆盖中解析实际模型、端点、凭据、协议和故障转移链。
    """

    def __init__(self):
        self._unified_client: Any | None = None

    async def call_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        *,
        role: str | None = None,
        thinking: dict | None = None,
        effort: str | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> Any:
        """按角色调用统一协议层 / Call the unified protocol layer by role."""
        if not role:
            raise ValueError("AI 调用必须显式指定 role")
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

    async def _call_via_unified(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        tools: list[dict] | None,
        tool_choice: str | None,
        timeout: float | None,
        max_tokens: int | None,
        role: str,
        thinking: dict | None,
        effort: str | None,
        top_p: float | None,
        top_k: int | None,
    ) -> Any:
        """解析角色候选链后调用统一客户端 / Resolve and invoke unified client."""
        try:
            chain = await self._resolve_role_chain(role)
        except Exception as exc:
            raise AllCandidatesFailedError(
                f"角色 {role} 候选链解析失败，请检查 AI 账号和角色绑定。"
            ) from exc

        if chain is None or not getattr(chain, "candidates", None):
            raise AllCandidatesFailedError(
                f"角色 {role} 无可用 AI 候选模型，请检查 AI 账号和角色绑定。"
            )

        return await self._get_unified_client().call_with_retry(
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

    def _get_unified_client(self) -> Any:
        """延迟创建统一客户端 / Lazily create UnifiedAIClient."""
        if self._unified_client is None:
            from backend.core.config import get_settings
            from backend.services.ai_reviewer.unified_client import (
                FallbackConfig,
                UnifiedAIClient,
            )

            settings = get_settings()
            config = FallbackConfig(
                enabled=getattr(settings, "ai_fallback_enabled", True),
                max_candidates=int(getattr(settings, "ai_fallback_max_candidates", 3)),
                max_retries=settings.ai_api_max_retries,
                total_timeout=settings.ai_api_total_timeout_seconds,
                initial_retry_delay=settings.ai_api_initial_retry_delay_seconds,
            )
            self._unified_client = UnifiedAIClient(fallback_config=config)
        return self._unified_client

    async def _resolve_role_chain(self, role: str) -> Any:
        """从账号与角色绑定解析候选链 / Resolve a chain from accounts and roles."""
        from backend.core.ai_protocol.role_config import resolve_role_from_config

        return await resolve_role_from_config(role)

    async def resolve_role_model_context(
        self, role: str
    ) -> tuple[str | None, int | None]:
        """返回角色 primary 候选的模型 ID 与上下文窗口。"""
        try:
            chain = await self._resolve_role_chain(role)
        except Exception as exc:
            logger.warning("解析角色上下文配置失败: role={} err={}", role, exc)
            return None, None
        primary = getattr(chain, "primary", None) if chain is not None else None
        if primary is None:
            return None, None
        return primary.model.model_id, primary.model.context_window_tokens
