"""账号连接探测 / Account connection probing.

为 AI 配置页提供"测试连接"与"获取模型列表"能力：按账号指定的协议族解析端点，
通过对应适配器发起一次模型发现请求，返回可用模型列表或结构化错误。
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from backend.core.ai_protocol.endpoint_security import validate_provider_base_url
from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import ProtocolFamily
from backend.core.ai_protocol.registry import get_adapter, resolve_account_endpoint
from backend.core.ai_providers import get_builtin_provider, provider_declaration_to_dict


def _parse_family(value: str) -> ProtocolFamily:
    try:
        return ProtocolFamily(value)
    except ValueError:
        return ProtocolFamily.OPENAI_COMPATIBLE


async def probe_account(
    *,
    provider_id: str,
    protocol: str,
    api_base: str,
    api_key: str,
    model: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """测试账号连接并返回可用模型 / Probe an account and return models.

    返回结构：{success, message, models, provider, default_model, context_window_k}。
    与 setup_service.test_ai_api 对齐，便于前端复用。
    """
    # 用户经常从网页/聊天工具复制令牌，首尾空白或换行不应参与鉴权。
    api_key = api_key.strip()

    if not api_key and provider_id not in (
        "ollama",
        "vllm",
        "lmstudio",
        "custom",
        "custom-anthropic",
    ):
        return {"success": False, "message": "API Key 不能为空"}

    decl = get_builtin_provider(provider_id)
    ok, message = validate_provider_base_url(
        decl.id,
        api_base,
        protocol=protocol,
    )
    if not ok:
        return {"success": False, "message": message}

    family = _parse_family(protocol) if protocol else decl.family
    endpoint = resolve_account_endpoint(decl, family=family, base_url=api_base)
    adapter = get_adapter(family)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
        ) as client:
            discovered = await adapter.list_models(client, endpoint, api_key)
        model_ids = sorted({d.model_id for d in discovered})
        context_window_k: int | None = None
        selected = model or (decl.default_models[0] if decl.default_models else "")
        if selected:
            for d in discovered:
                if d.model_id == selected and d.context_window_tokens:
                    ctx = d.context_window_tokens
                    context_window_k = max(1, round(ctx / 1000)) if ctx > 2000 else ctx
                    break
        return {
            "success": True,
            "message": f"连接成功，可用模型 {len(model_ids)} 个",
            "models": model_ids,
            "provider": provider_declaration_to_dict(decl),
            "default_model": selected,
            "context_window_k": context_window_k,
        }
    except AIError as exc:
        if exc.category.value == "auth_invalid":
            return {
                "success": False,
                "message": (
                    "API 鉴权失败：上游拒绝了当前凭证，请检查 API Key 是否过期、"
                    "令牌权限/渠道是否可用，以及 API Base URL 是否正确"
                ),
            }
        if exc.category.value == "network":
            return {
                "success": False,
                "message": "无法连接到 API 服务，请检查 API Base URL",
            }
        return {"success": False, "message": f"验证失败: {exc}"}
    except Exception as exc:
        logger.debug("账号探测异常 / account probe error: {}", exc)
        return {"success": False, "message": f"验证异常: {exc}"}


__all__ = ["probe_account"]
