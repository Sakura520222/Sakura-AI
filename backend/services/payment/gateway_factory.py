"""支付网关工厂"""

from typing import Optional

from loguru import logger

from backend.services.payment.gateway_base import PaymentGateway
from backend.services.payment.alipay_gateway import AlipayGateway
from backend.services.payment.nowpayments_gateway import NowPaymentsGateway
from backend.services.payment.paddle_gateway import PaddleGateway
from backend.services.payment.stripe_gateway import StripeGateway
from backend.services.payment.tron_gateway import TronGateway

_GATEWAY_REGISTRY: dict[str, type[PaymentGateway]] = {
    "stripe": StripeGateway,
    "paddle": PaddleGateway,
    "alipay": AlipayGateway,
    "nowpayments": NowPaymentsGateway,
    "tron": TronGateway,
}

# 已注册的支付提供商名称列表（供外部验证用）
def get_supported_providers() -> tuple[str, ...]:
    """获取当前已注册的支付提供商名称列表"""
    return tuple(_GATEWAY_REGISTRY.keys())


# 向后兼容的模块级常量（模块加载时快照）
SUPPORTED_PROVIDERS = get_supported_providers()


def register_gateway(name: str, gateway_cls: type[PaymentGateway]) -> None:
    """注册新的支付网关提供商"""
    _GATEWAY_REGISTRY[name] = gateway_cls
    # 同步更新模块级常量
    global SUPPORTED_PROVIDERS
    SUPPORTED_PROVIDERS = get_supported_providers()
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

        # Alipay uses different key names
        if provider == "alipay":
            if api_key is None:
                api_key = str(await get_dynamic_config("alipay_app_id") or "")
            if webhook_secret is None:
                webhook_secret = str(
                    await get_dynamic_config("alipay_private_key") or ""
                )
        # NOWPayments uses ipn_secret as webhook secret
        elif provider == "nowpayments":
            if api_key is None:
                api_key = str(
                    await get_dynamic_config("nowpayments_api_key") or ""
                )
            if webhook_secret is None:
                webhook_secret = str(
                    await get_dynamic_config("nowpayments_ipn_secret") or ""
                )
        # Tron uses wallet_address instead of api_key
        elif provider == "tron":
            api_key = str(await get_dynamic_config("tron_wallet_address") or "")
        else:
            if api_key is None:
                api_key = str(await get_dynamic_config(f"{provider}_api_key") or "")
            if webhook_secret is None:
                webhook_secret = str(
                    await get_dynamic_config(f"{provider}_webhook_secret") or ""
                )

    if not api_key and provider != "tron":
        raise ValueError(
            f"Payment provider {provider} is not configured: missing API key"
        )

    # Alipay 需要额外的 alipay_public_key 参数
    if provider == "alipay":
        from backend.core.config import get_dynamic_config

        alipay_public_key = str(
            await get_dynamic_config("alipay_public_key") or ""
        )
        return gateway_cls(
            api_key=api_key,
            webhook_secret=webhook_secret,
            alipay_public_key=alipay_public_key,
        )

    # NOWPayments 需要额外的 pay_currency 参数
    if provider == "nowpayments":
        from backend.core.config import get_dynamic_config

        pay_currency = str(
            await get_dynamic_config("nowpayments_pay_currency") or "usdttrc20"
        )
        return gateway_cls(
            api_key=api_key,
            webhook_secret=webhook_secret,
            pay_currency=pay_currency,
        )

    # TronGateway 需要 wallet_address
    if provider == "tron":
        from backend.core.config import get_dynamic_config

        wallet_address = str(
            await get_dynamic_config("tron_wallet_address") or ""
        )
        tron_api_key = str(
            await get_dynamic_config("tron_api_key") or ""
        )
        if not wallet_address:
            raise ValueError(
                "TronGateway not configured: missing tron_wallet_address"
            )
        return gateway_cls(
            wallet_address=wallet_address,
            api_key=tron_api_key,
        )

    return gateway_cls(api_key=api_key, webhook_secret=webhook_secret)


async def get_configured_providers() -> list[dict[str, str]]:
    """检测已配置的支付提供商，返回可用的 provider 列表

    通过检查动态配置中是否存在对应的 API key / secret 来判断。
    返回格式: [{"id": "stripe", "label": "Stripe"}, ...]
    """
    from backend.core.config import get_dynamic_config

    provider_checks = {
        "stripe": ("stripe_api_key", "Stripe (信用卡)"),
        "paddle": ("paddle_api_key", "Paddle (国际支付)"),
        "alipay": ("alipay_app_id", "支付宝"),
        "nowpayments": ("nowpayments_api_key", "USDT 虚拟币 (NOWPayments)"),
        "tron": ("tron_wallet_address", "USDT 虚拟币 (直收)"),
    }

    configured = []
    for provider_id, (key_name, label) in provider_checks.items():
        value = await get_dynamic_config(key_name)
        if value:
            configured.append({"id": provider_id, "label": label})

    return configured
