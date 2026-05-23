"""支付网关抽象基类与数据结构"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class WebhookEventType(str, Enum):
    """Webhook 事件类型"""

    PAYMENT_COMPLETED = "payment_completed"
    PAYMENT_EXPIRED = "payment_expired"
    PAYMENT_REFUNDED = "payment_refunded"
    UNKNOWN = "unknown"


@dataclass
class PaymentIntentResult:
    """创建支付意图的结果"""

    success: bool
    provider_tx_id: str = ""
    checkout_url: str = ""
    client_secret: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""


@dataclass
class WebhookEvent:
    """解析后的 Webhook 事件"""

    event_type: WebhookEventType
    provider_tx_id: str = ""
    order_no: str = ""
    amount_cents: int = 0
    currency: str = ""
    raw_event: Any = None


@dataclass
class RefundResult:
    """退款结果"""

    success: bool
    refund_id: str = ""
    amount_cents: int = 0
    status: str = ""
    error_message: str = ""


@dataclass
class PaymentStatusResult:
    """支付状态查询结果"""

    success: bool
    status: str = ""
    provider_tx_id: str = ""
    amount_cents: int = 0
    currency: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""


class PaymentGateway(ABC):
    """支付网关抽象基类"""

    @abstractmethod
    async def create_payment(
        self,
        order_no: str,
        amount_cents: int,
        currency: str,
        plan_name: str,
        user_id: int,
        success_url: str,
        cancel_url: str,
        metadata: Optional[dict[str, str]] = None,
    ) -> PaymentIntentResult:
        """创建支付意图"""

    @abstractmethod
    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        """验证并解析 Webhook 事件"""

    @abstractmethod
    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> RefundResult:
        """发起退款"""

    @abstractmethod
    async def get_payment_status(
        self,
        provider_tx_id: str,
    ) -> PaymentStatusResult:
        """查询支付状态"""
