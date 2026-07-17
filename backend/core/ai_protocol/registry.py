"""适配器注册表 / Adapter registry.

按协议族返回对应适配器单例，并集中解析端点。
Returns the singleton adapter for each protocol family and resolves endpoints.
"""

from __future__ import annotations

from functools import lru_cache

from backend.core.ai_protocol.adapters.anthropic_native import AnthropicNativeAdapter
from backend.core.ai_protocol.adapters.base import ProtocolAdapter
from backend.core.ai_protocol.adapters.gemini_native import GeminiNativeAdapter
from backend.core.ai_protocol.adapters.openai_compatible import (
    OpenAICompatibleAdapter,
)
from backend.core.ai_protocol.adapters.openai_responses import OpenAIResponsesAdapter
from backend.core.ai_protocol.models import (
    AuthScheme,
    ProtocolFamily,
    ProviderDeclaration,
    ResolvedEndpoint,
)

# 各协议族的默认 chat 路径（相对 base_url，base_url 带尾部斜杠）
_DEFAULT_CHAT_PATHS: dict[ProtocolFamily, str] = {
    ProtocolFamily.OPENAI_COMPATIBLE: "chat/completions",
    ProtocolFamily.OPENAI_RESPONSES: "responses",
    ProtocolFamily.ANTHROPIC_NATIVE: "messages",
    ProtocolFamily.GEMINI_NATIVE: "",  # 路径含模型名，在适配器内拼装
}

# 各协议族的默认鉴权方式（账号协议覆盖时使用）
# Default auth scheme per family (used when account overrides the protocol).
_AUTH_SCHEME_BY_FAMILY: dict[ProtocolFamily, AuthScheme] = {
    ProtocolFamily.OPENAI_COMPATIBLE: AuthScheme.BEARER,
    ProtocolFamily.OPENAI_RESPONSES: AuthScheme.BEARER,
    ProtocolFamily.ANTHROPIC_NATIVE: AuthScheme.X_API_KEY,
    ProtocolFamily.GEMINI_NATIVE: AuthScheme.GOOGLE_ADC,
}


@lru_cache(maxsize=None)
def get_adapter(family: ProtocolFamily) -> ProtocolAdapter:
    """获取协议族对应的适配器单例 / Get the adapter singleton for a family."""
    if family == ProtocolFamily.OPENAI_COMPATIBLE:
        return OpenAICompatibleAdapter()
    if family == ProtocolFamily.OPENAI_RESPONSES:
        return OpenAIResponsesAdapter()
    if family == ProtocolFamily.ANTHROPIC_NATIVE:
        return AnthropicNativeAdapter()
    if family == ProtocolFamily.GEMINI_NATIVE:
        return GeminiNativeAdapter()
    raise ValueError(f"暂不支持的协议族 / unsupported protocol family: {family}")


def ensure_trailing_slash(base_url: str) -> str:
    """确保 base_url 以单个斜杠结尾 / Ensure exactly one trailing slash."""
    url = (base_url or "").strip()
    if not url:
        return ""
    return url if url.endswith("/") else f"{url}/"


def resolve_endpoint(provider: ProviderDeclaration, base_url: str | None) -> ResolvedEndpoint:
    """根据提供商声明与可选 base_url 解析端点 / Resolve endpoint for a provider."""
    base = ensure_trailing_slash(base_url or provider.base_url)
    chat_path = _DEFAULT_CHAT_PATHS.get(provider.family, "")
    extra_headers: dict[str, str] = {}
    return ResolvedEndpoint(
        base_url=base,
        chat_path=chat_path,
        auth_scheme=provider.auth_scheme,
        extra_headers=extra_headers,
    )


def resolve_account_endpoint(
    provider: ProviderDeclaration,
    *,
    family: ProtocolFamily,
    base_url: str = "",
) -> ResolvedEndpoint:
    """按账号指定的协议族与 base_url 解析端点 / Resolve endpoint from an account.

    账号可覆盖协议族（如 DeepSeek 账号走 Anthropic 兼容端点）：
    - base_url 优先取账号值，否则取 provider.endpoints[family]，再否则取默认 base_url；
    - auth_scheme 按协议族推导，保证 OpenAI→bearer、Anthropic→x-api-key。
    """
    if base_url:
        base = base_url
    elif family in provider.endpoints:
        base = provider.endpoints[family]
    else:
        base = provider.base_url
    chat_path = _DEFAULT_CHAT_PATHS.get(family, "")
    auth = _AUTH_SCHEME_BY_FAMILY.get(family, provider.auth_scheme)
    return ResolvedEndpoint(
        base_url=ensure_trailing_slash(base),
        chat_path=chat_path,
        auth_scheme=auth,
    )


__all__ = [
    "AuthScheme",
    "get_adapter",
    "resolve_endpoint",
    "resolve_account_endpoint",
    "ensure_trailing_slash",
]
