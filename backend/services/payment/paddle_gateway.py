"""Paddle Billing 支付网关实现

使用 Paddle Billing Transaction + Checkout 模式。
Paddle 作为 Merchant of Record (MoR) 处理支付、税务和发票。

关键概念：
- Transaction: 创建交易（包含商品/价格信息），获取 checkout URL
- Checkout: Paddle 托管支付页面，支持全球支付方式（信用卡、PayPal、支付宝等）
- Adjustment: 退款/冲正操作
- Webhook: 事件通知（transaction.completed, transaction.paid 等）

Webhook 签名验证：
- Header: Paddle-Signature，格式 ts=<timestamp>;h1=<hmac_hex>
- HMAC-SHA256(secret, f"{timestamp}:{raw_body}")
"""

import asyncio
import hashlib
import hmac
import re

from loguru import logger

from backend.services.payment.gateway_base import (
    PaymentGateway,
    PaymentIntentResult,
    PaymentStatusResult,
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)


class PaddleGateway(PaymentGateway):
    """Paddle Billing 网关（使用 paddle-python-sdk）"""

    def __init__(self, api_key: str, webhook_secret: str):
        self._api_key = api_key
        self._webhook_secret = webhook_secret

    # ------------------------------------------------------------------
    # 创建支付
    # ------------------------------------------------------------------

    async def create_payment(
        self,
        order_no: str,
        amount_cents: int,
        currency: str,
        plan_name: str,
        user_id: int,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, str] | None = None,
    ) -> PaymentIntentResult:
        """创建 Paddle Transaction 并返回 Checkout URL

        使用非目录商品（Non-catalog price with product），
        无需预先在 Paddle Dashboard 创建 Product/Price。
        """
        try:
            from paddle_billing import Client, Environment, Options
            from paddle_billing.Entities.Shared import (
                CollectionMode,
                CurrencyCode,
                CustomData,
                Money,
                TaxCategory,
            )
            from paddle_billing.Resources.Transactions.Operations import (
                CreateTransaction,
            )
            from paddle_billing.Resources.Transactions.Operations.Create import (
                TransactionCreateItemWithPrice,
            )
            from paddle_billing.Resources.Transactions.Operations.Price import (
                TransactionNonCatalogPriceWithProduct,
                TransactionNonCatalogProduct,
            )

            # Paddle SDK 是同步的，但我们的方法标记为 async 以匹配基类。
            # 在实际使用中，这会在 FastAPI 的线程池中执行。
            # 选择环境：test_mode 标识 sandbox
            # 注意：sandbox API key 通常以 test_ 开头
            env = (
                Environment.SANDBOX
                if self._api_key.startswith("test_")
                else Environment.PRODUCTION
            )
            paddle = Client(self._api_key, options=Options(env))

            # 构建 custom_data，用于 webhook 回调时关联订单
            custom = {"order_no": order_no, "user_id": str(user_id)}
            if metadata:
                custom.update(metadata)

            # Paddle 金额格式：字符串，无小数点（最小货币单位，如 cents）
            amount_str = str(amount_cents)

            transaction = await asyncio.to_thread(
                paddle.transactions.create,
                CreateTransaction(
                    items=[
                        TransactionCreateItemWithPrice(
                            price=TransactionNonCatalogPriceWithProduct(
                                description=f"Order {order_no}",
                                unit_price=Money(
                                    amount_str, CurrencyCode(currency.upper())
                                ),
                                product=TransactionNonCatalogProduct(
                                    name=plan_name,
                                    tax_category=TaxCategory.Standard,
                                ),
                            ),
                            quantity=1,
                        )
                    ],
                    collection_mode=CollectionMode.Automatic,
                    currency_code=CurrencyCode(currency.upper()),
                    custom_data=CustomData(custom),
                    checkout={"url": success_url},
                ),
            )

            checkout_url = transaction.checkout.url if transaction.checkout else ""
            tx_id = transaction.id

            logger.info(
                "Paddle Transaction created: tx_id={}, order_no={}, checkout_url={}",
                tx_id,
                order_no,
                checkout_url[:80] if checkout_url else "(empty)",
            )

            return PaymentIntentResult(
                success=True,
                provider_tx_id=tx_id,
                checkout_url=checkout_url or "",
                client_secret="",
                raw_data={
                    "transaction_id": tx_id,
                    "checkout_url": checkout_url,
                    "status": str(transaction.status) if transaction.status else "",
                },
            )
        except ImportError:
            logger.error(
                "paddle-python-sdk is not installed. Run: pip install paddle-python-sdk"
            )
            return PaymentIntentResult(
                success=False,
                error_message="paddle-python-sdk is not installed",
            )
        except Exception as e:
            logger.error("Paddle create_payment failed: {}", e)
            return PaymentIntentResult(
                success=False,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Webhook 验证
    # ------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        """验证 Paddle Webhook 签名并解析事件

        Paddle webhook 签名格式：
        - Header: Paddle-Signature: ts=<timestamp>;h1=<hmac_sha256_hex>
        - 验证: HMAC-SHA256(webhook_secret, f"{timestamp}:{raw_body}")
        """
        signature_header = headers.get("paddle-signature", "")
        if not signature_header:
            logger.warning("Paddle webhook: missing Paddle-Signature header")
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        # 解析 ts=<timestamp>;h1=<hmac_hex>
        ts_match = re.search(r"ts=(\d+)", signature_header)
        h1_match = re.search(r"h1=([a-fA-F0-9]+)", signature_header)
        if not ts_match or not h1_match:
            logger.warning("Paddle webhook: invalid signature format")
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        timestamp = ts_match.group(1)
        provided_sig = h1_match.group(1)

        # 计算 HMAC-SHA256
        payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        signed_payload = f"{timestamp}:{payload_str}"
        computed_sig = hmac.new(
            self._webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # 时序安全比较
        if not hmac.compare_digest(computed_sig, provided_sig):
            logger.warning("Paddle webhook: signature verification failed")
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        # 解析事件 payload
        try:
            import json

            body = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Paddle webhook: invalid JSON payload: {}", e)
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        event_type_str = body.get("event_type", "") or body.get("meta", {}).get(
            "event_name", ""
        )
        data = body.get("data", {})
        attrs = data.get("attributes", data)  # Paddle Billing 使用 data.attributes

        # 事件类型映射
        type_map = {
            "transaction.completed": WebhookEventType.PAYMENT_COMPLETED,
            "transaction.paid": WebhookEventType.PAYMENT_COMPLETED,
            "transaction.canceled": WebhookEventType.PAYMENT_EXPIRED,
            "transaction.payment_failed": WebhookEventType.PAYMENT_EXPIRED,
            "transaction.past_due": WebhookEventType.PAYMENT_EXPIRED,
            "adjustment.created": WebhookEventType.PAYMENT_REFUNDED,
        }
        resolved_type = type_map.get(event_type_str, WebhookEventType.UNKNOWN)

        if resolved_type == WebhookEventType.UNKNOWN:
            logger.warning("Paddle webhook: unmapped event type '{}'", event_type_str)
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        # 提取 provider_tx_id（transaction ID）
        provider_tx_id = data.get("id", "")

        # 提取 custom_data 中的 order_no
        custom_data = attrs.get("custom_data", {}) or {}
        order_no = custom_data.get("order_no", "")

        # 对于 adjustment 事件，从 transaction_id 获取
        if not order_no and resolved_type == WebhookEventType.PAYMENT_REFUNDED:
            # adjustment 事件可能包含 transaction_id
            order_no = ""  # 需要通过 provider_tx_id 反查

        # 金额（Paddle 使用字符串金额，最小货币单位）
        detail_totals = attrs.get("details", {}).get("totals", {})
        amount_str = detail_totals.get("total", attrs.get("total", "0"))
        try:
            amount_cents = int(amount_str) if amount_str else 0
        except ValueError, TypeError:
            amount_cents = 0

        currency = attrs.get("currency_code", attrs.get("currency", ""))

        return WebhookEvent(
            event_type=resolved_type,
            provider_tx_id=provider_tx_id,
            order_no=order_no,
            amount_cents=amount_cents,
            currency=currency,
            raw_event=body,
        )

    # ------------------------------------------------------------------
    # 退款
    # ------------------------------------------------------------------

    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: int | None = None,
        reason: str | None = None,
    ) -> RefundResult:
        """通过 Paddle Adjustments API 发起退款

        使用 paddle-python-sdk 创建 full 或 partial adjustment。
        """
        try:
            from paddle_billing import Client, Environment, Options
            from paddle_billing.Entities.Shared import Action, AdjustmentType
            from paddle_billing.Resources.Adjustments.Operations import CreateAdjustment
            from paddle_billing.Resources.Adjustments.Operations.Create import (
                CreateAdjustmentItem,
            )

            env = (
                Environment.SANDBOX
                if self._api_key.startswith("test_")
                else Environment.PRODUCTION
            )
            paddle = Client(self._api_key, options=Options(env))

            refund_reason = reason or "requested_by_customer"

            if amount_cents is not None:
                # 部分退款：需要获取 transaction items
                transaction = await asyncio.to_thread(
                    paddle.transactions.get, provider_tx_id
                )
                items = []
                for item in (
                    transaction.details.line_items if transaction.details else []
                ):
                    items.append(
                        CreateAdjustmentItem(
                            item_id=item.id,
                            type=AdjustmentType.Partial,
                            amount=str(amount_cents),
                        )
                    )
                    break  # 只对第一个 item 做部分退款即可

                if not items:
                    return RefundResult(
                        success=False,
                        error_message="No line items found for transaction",
                    )

                adjustment = await asyncio.to_thread(
                    paddle.adjustments.create,
                    CreateAdjustment.partial(
                        action=Action.Refund,
                        items=items,
                        reason=refund_reason,
                        transaction_id=provider_tx_id,
                    ),
                )
            else:
                # 全额退款
                adjustment = await asyncio.to_thread(
                    paddle.adjustments.create,
                    CreateAdjustment.full(
                        action=Action.Refund,
                        reason=refund_reason,
                        transaction_id=provider_tx_id,
                    ),
                )

            logger.info(
                "Paddle refund adjustment created: adjustment_id={}, tx_id={}",
                adjustment.id,
                provider_tx_id,
            )

            return RefundResult(
                success=True,
                refund_id=adjustment.id,
                amount_cents=amount_cents or 0,
                status=str(adjustment.status) if adjustment.status else "",
            )
        except ImportError:
            logger.error("paddle-python-sdk is not installed")
            return RefundResult(
                success=False,
                error_message="paddle-python-sdk is not installed",
            )
        except Exception as e:
            error_str = str(e)
            logger.error("Paddle refund failed: {}", error_str)
            return RefundResult(
                success=False,
                error_message=error_str,
            )

    # ------------------------------------------------------------------
    # 查询支付状态
    # ------------------------------------------------------------------

    async def get_payment_status(
        self,
        provider_tx_id: str,
    ) -> PaymentStatusResult:
        """查询 Paddle Transaction 状态"""
        try:
            from paddle_billing import Client, Environment, Options

            env = (
                Environment.SANDBOX
                if self._api_key.startswith("test_")
                else Environment.PRODUCTION
            )
            paddle = Client(self._api_key, options=Options(env))

            transaction = await asyncio.to_thread(
                paddle.transactions.get, provider_tx_id
            )

            # 提取金额
            detail_totals = transaction.details.totals if transaction.details else None
            amount_str = (
                detail_totals.total if detail_totals and detail_totals.total else "0"
            )
            try:
                amount_cents = int(amount_str)
            except ValueError, TypeError:
                amount_cents = 0

            currency = (
                str(transaction.currency_code) if transaction.currency_code else ""
            )

            return PaymentStatusResult(
                success=True,
                status=str(transaction.status) if transaction.status else "",
                provider_tx_id=transaction.id,
                amount_cents=amount_cents,
                currency=currency,
                raw_data={
                    "transaction_id": transaction.id,
                    "status": str(transaction.status) if transaction.status else "",
                    "currency_code": currency,
                    "checkout_url": (
                        transaction.checkout.url if transaction.checkout else None
                    ),
                },
            )
        except ImportError:
            logger.error("paddle-python-sdk is not installed")
            return PaymentStatusResult(
                success=False,
                error_message="paddle-python-sdk is not installed",
            )
        except Exception as e:
            logger.error("Paddle get_payment_status failed: {}", e)
            return PaymentStatusResult(
                success=False,
                error_message=str(e),
            )

    async def cancel_payment(
        self,
        provider_tx_id: str,
    ) -> RefundResult:
        """取消 Paddle 交易"""
        try:
            # 延迟导入：paddle_billing SDK 为可选依赖，避免未安装时 import 失败
            from paddle_billing import Client, Environment, Options
            from paddle_billing.Entities.Shared import TransactionStatus
            from paddle_billing.Resources.Transactions.Operations import UpdateTransaction

            env = (
                Environment.SANDBOX
                if self._api_key.startswith("test_")
                else Environment.PRODUCTION
            )
            paddle = Client(self._api_key, options=Options(env))
            await asyncio.to_thread(
                paddle.transactions.update,
                provider_tx_id,
                UpdateTransaction(status=TransactionStatus.Canceled),
            )
            logger.info(
                "Paddle transaction cancelled: tx_id={}",
                provider_tx_id,
            )
            return RefundResult(
                success=True,
                status="cancelled",
            )
        except ImportError:
            logger.error("paddle-python-sdk is not installed")
            return RefundResult(
                success=False,
                error_message="paddle-python-sdk is not installed",
            )
        except Exception as e:
            logger.error("Paddle cancel failed: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )
