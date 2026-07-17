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

from urllib.parse import urlsplit, urlunsplit

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
    def _safe_location(location: str) -> str:
        """删除重定向地址中的查询与片段，避免将凭据写入日志。"""
        try:
            parsed = urlsplit(location)
            hostname = parsed.hostname or ""
            port = parsed.port
        except ValueError:
            return "<invalid>"
        netloc = f"{hostname}:{port}" if port is not None else hostname
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def ensure_success_status(
        self,
        response: httpx.Response,
        endpoint: ResolvedEndpoint,
        *,
        model: str = "",
        operation: str = "AI 请求",
    ) -> None:
        """确保 HTTP 响应属于 2xx，拒绝未跟随的重定向。"""
        if 200 <= response.status_code < 300:
            return
        location = self._safe_location(response.headers.get("location", ""))
        details = f"status={response.status_code}"
        if location:
            details += f" location={location}"
        raise self.raise_error(
            AIErrorCategory.UNKNOWN,
            f"{operation} 返回非成功 HTTP 状态: {details}",
            status_code=response.status_code,
            provider=endpoint.base_url,
            model=model,
        )

    def ensure_sse_response(
        self,
        response: httpx.Response,
        endpoint: ResolvedEndpoint,
        *,
        model: str = "",
        operation: str = "AI stream 请求",
    ) -> None:
        """验证流式响应状态和 Content-Type，避免网关页面被静默吞掉。"""
        if not 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            category, message = self.translate_error(response.status_code, body)
            raise self.raise_error(
                category,
                message,
                status_code=response.status_code,
                provider=endpoint.base_url,
                model=model,
            )
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            raise self.raise_error(
                AIErrorCategory.UNKNOWN,
                f"{operation} 返回非 SSE 内容: status={response.status_code} "
                f"content_type={content_type or '<missing>'}",
                status_code=response.status_code,
                provider=endpoint.base_url,
                model=model,
            )

    def parse_json_response(
        self,
        response: httpx.Response,
        endpoint: ResolvedEndpoint,
        *,
        model: str = "",
        operation: str = "AI 请求",
        allow_list: bool = False,
    ) -> Any:
        """解析成功响应 JSON，并将异常响应归一化为可恢复错误。"""
        self.ensure_success_status(
            response,
            endpoint,
            model=model,
            operation=operation,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "<missing>")
            raise self.raise_error(
                AIErrorCategory.UNKNOWN,
                f"{operation} 返回无效 JSON: status={response.status_code} "
                f"content_type={content_type} content_length={len(response.content)}",
                status_code=response.status_code,
                provider=endpoint.base_url,
                model=model,
                cause=exc,
            ) from exc
        if not isinstance(payload, dict) and not (allow_list and isinstance(payload, list)):
            expected_root = "对象或数组" if allow_list else "对象"
            raise self.raise_error(
                AIErrorCategory.UNKNOWN,
                f"{operation} 返回 JSON 根节点不是{expected_root}: "
                f"status={response.status_code} "
                f"content_type={response.headers.get('content-type', '<missing>')}",
                status_code=response.status_code,
                provider=endpoint.base_url,
                model=model,
            )
        return payload

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
