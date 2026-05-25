"""支付网关抽象层"""

from backend.services.payment.gateway_base import (
    PaymentGateway,
    PaymentIntentResult,
    WebhookEvent,
    WebhookEventType,
    RefundResult,
    PaymentStatusResult,
)
from backend.services.payment.gateway_factory import get_gateway

__all__ = [
    "PaymentGateway",
    "PaymentIntentResult",
    "WebhookEvent",
    "WebhookEventType",
    "RefundResult",
    "PaymentStatusResult",
    "get_gateway",
]


# Provider name constants
PROVIDER_STRIPE = "stripe"
PROVIDER_PADDLE = "paddle"
PROVIDER_ALIPAY = "alipay"
PROVIDER_NOWPAYMENTS = "nowpayments"
PROVIDER_MANUAL = "manual"
PROVIDER_REDEEM_CODE = "redeem_code"

# Providers that require external payment gateway integration
EXTERNAL_PAYMENT_PROVIDERS = {PROVIDER_STRIPE, PROVIDER_PADDLE, PROVIDER_ALIPAY, PROVIDER_NOWPAYMENTS}
