"""支付网关工厂"""

from typing import Optional

from loguru import logger

from backend.services.payment.gateway_base import PaymentGateway
from backend.services.payment.stripe_gateway import StripeGateway

_GATEWAY_REGISTRY: dict[str, type[PaymentGateway]] = {
    "stripe": StripeGateway,
}


def register_gateway(name: str, gateway_cls: type[PaymentGateway]) -> None:
    """注册新的支付网关提供商"""
    _GATEWAY_REGISTRY[name] = gateway_cls
    logger.info("Payment gateway registered: {}", name)


async def get_gateway(
    provider: str,
    api_key: Optional[str] = None,
    webhook_secret: Optional[str] = None,
) -> PaymentGateway:
    """根据 provider 名称获取对应的支付网关实例

    当 api_key / webhook_secret 未传入时，从动态配置中读取。
    """
    gateway_cls = _GATEWAY_REGISTRY.get(provider)
    if gateway_cls is None:
        supported = ", ".join(_GATEWAY_REGISTRY.keys())
        raise ValueError(
            f"Unsupported payment provider: {provider}. Supported: {supported}"
        )

    if api_key is None or webhook_secret is None:
        from backend.core.config import get_dynamic_config

        if api_key is None:
            api_key = str(await get_dynamic_config(f"{provider}_api_key") or "")
        if webhook_secret is None:
            webhook_secret = str(
                await get_dynamic_config(f"{provider}_webhook_secret") or ""
            )

    if not api_key:
        raise ValueError(
            f"Payment provider {provider} is not configured: missing API key"
        )

    return gateway_cls(api_key=api_key, webhook_secret=webhook_secret)
