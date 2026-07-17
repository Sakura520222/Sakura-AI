"""协议适配器抽象基类 / Protocol adapter abstract base.

每个协议族实现一个适配器，负责：
- 模型发现（list_models / fetch_model_metadata）
- 请求序列化（UnifiedRequest → wire format）
- 响应反序列化（wire format → UnifiedResponse）
- 错误归一化（translate_error）
- 流式事件归一化（stream）

Each protocol family has one adapter responsible for model discovery, request
serialization, response deserialization, error normalization, and streaming.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

import httpx

from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ModelCapabilitySet,
    ModelDiscoveryResult,
    ProtocolFamily,
    ResolvedEndpoint,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedStreamEvent,
)


class ProtocolAdapter(ABC):
    """协议适配器抽象基类 / Abstract protocol adapter."""

    family: ProtocolFamily

    @abstractmethod
    def build_headers(
        self, credential: str, endpoint: ResolvedEndpoint
    ) -> dict[str, str]:
        """构建鉴权与协议头 / Build auth + protocol headers."""

    @abstractmethod
    async def list_models(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
    ) -> list[ModelDiscoveryResult]:
        """列出可用模型 / List available models."""

    @abstractmethod
    async def fetch_model_metadata(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        model_id: str,
    ) -> Optional[ModelDiscoveryResult]:
        """获取单个模型元数据 / Fetch metadata for one model."""

    @abstractmethod
    async def chat(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: Optional[float] = None,
    ) -> UnifiedResponse:
        """发送聊天请求并返回归一化响应 / Send chat request, return normalized response."""

    @abstractmethod
    async def stream(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        """发送流式聊天请求 / Send streaming chat request."""

    @abstractmethod
    def translate_error(
        self, status_code: int, body: Any
    ) -> tuple[AIErrorCategory, str]:
        """将厂商错误映射到归一化类别 / Map vendor error to normalized category."""

    def supports_capability(self, capability: ModelCapabilitySet) -> bool:
        """本协议族是否支持给定能力集（默认 True，由子类覆盖）/ Family-level capability gate."""
        return True

    @staticmethod
    def raise_error(
        category: AIErrorCategory,
        message: str,
        *,
        status_code: Optional[int] = None,
        provider: str = "",
        model: str = "",
        cause: Optional[BaseException] = None,
    ) -> "AIError":
        """构造并可直接 raise 的 AIError / Build an AIError ready to raise."""
        return AIError(
            category,
            message,
            status_code=status_code,
            provider=provider,
            model=model,
            cause=cause,
        )
