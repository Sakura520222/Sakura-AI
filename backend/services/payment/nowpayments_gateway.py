"""NOWPayments 虚拟币支付网关

使用 NOWPayments API 接收 USDT (TRC-20) 等虚拟币支付。
非托管模式：资金直接到达你的钱包，无需 KYC。

接入条件：
- 注册 https://nowpayments.io 账号
- 设置 Outcome Wallet（你的 USDT TRC-20 钱包地址）
- 生成 API Key
- 在 Store Settings 生成 IPN Secret Key

API 流程：
1. POST /v1/payment → 创建支付，返回充值地址
2. 用户向充值地址发送 USDT
3. NOWPayments 通过 IPN 回调通知支付状态变更
4. 回调验签：HMAC-SHA512(sorted_json, ipn_secret)

支付状态：
- waiting: 等待充值
- confirming: 区块确认中
- confirmed: 已确认
- sending: 发送到你的钱包中
- finished: 完成
- partially_paid: 部分支付
- expired: 过期
- failed: 失败
- refunded: 已退款
"""

import hashlib
import hmac
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx
from loguru import logger

from backend.services.payment.gateway_base import (
    PaymentGateway,
    PaymentIntentResult,
    PaymentStatusResult,
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)


class NowPaymentsGateway(PaymentGateway):
    """NOWPayments 虚拟币支付网关（USDT TRC-20 等）"""

    API_URL = "https://api.nowpayments.io"
    SANDBOX_URL = "https://api-sandbox.nowpayments.io"
    # 零小数位货币（ISO 4217 exponent = 0）；未列出的货币默认使用 2 位小数。
    CURRENCY_DECIMALS = {
        "BIF": 0,
        "CLP": 0,
        "DJF": 0,
        "GNF": 0,
        "JPY": 0,
        "KMF": 0,
        "KRW": 0,
        "MGA": 0,
        "PYG": 0,
        "RWF": 0,
        "UGX": 0,
        "VND": 0,
        "VUV": 0,
        "XAF": 0,
        "XOF": 0,
        "XPF": 0,
    }

    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        pay_currency: str = "usdttrc20",
        sandbox: bool = False,
    ):
        """
        Args:
            api_key: NOWPayments API Key
            webhook_secret: IPN Secret Key（用于验签回调）
            pay_currency: 接收的虚拟币类型，默认 usdttrc20
            sandbox: 是否使用沙箱环境
        """
        self._api_key = api_key
        self._ipn_secret = webhook_secret
        self._pay_currency = pay_currency
        self._base_url = self.SANDBOX_URL if sandbox else self.API_URL

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # 内部 HTTP
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> dict:
        """GET 请求"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{self._base_url}{path}", headers=self._headers)
            if resp.status_code >= 400:
                raise self._api_error(resp)
            return resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        """POST 请求，失败时包含 API 错误信息"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base_url}{path}", headers=self._headers, json=data
            )
            if resp.status_code >= 400:
                raise self._api_error(resp)
            return resp.json()

    @staticmethod
    def _api_error(resp: httpx.Response) -> Exception:
        """从 HTTP 错误响应构造包含 API 错误信息的异常"""
        try:
            body = resp.json()
            msg = body.get("message", "") or str(body)
        except Exception:
            msg = resp.text[:500]
        return ValueError(f"NOWPayments API error {resp.status_code}: {msg}")

    @classmethod
    def _currency_decimals(cls, currency: str) -> int:
        return cls.CURRENCY_DECIMALS.get(currency.upper(), 2)

    @classmethod
    def _to_minor_units(cls, amount: object, currency: str) -> int:
        """Convert a main-unit amount to currency-specific minor units.

        Invalid provider amounts are logged and converted to 0 as a sentinel.
        Callers must treat the 0 result as invalid in payment-confirmation paths;
        PaymentService._validate_payment_amount rejects non-positive paid amounts
        before fulfilling orders.
        """
        decimals = cls._currency_decimals(currency)
        try:
            value = Decimal(str(amount))
        except InvalidOperation, ValueError:
            logger.warning(
                "Invalid amount '{}' for currency '{}', defaulting to 0",
                amount,
                currency,
            )
            return 0
        scale = Decimal(10) ** decimals
        return int((value * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    @classmethod
    def _from_minor_units(cls, amount: int, currency: str) -> int | float:
        """Convert currency-specific minor units to JSON-safe main units.

        Returns int for zero-decimal currencies (JPY, KRW, etc.) and float for
        standard two-decimal currencies (USD, CNY, etc.).
        """
        decimals = cls._currency_decimals(currency)
        value = Decimal(amount) / (Decimal(10) ** decimals)
        return int(value) if decimals == 0 else float(value)

    # ------------------------------------------------------------------
    # IPN 验签
    # ------------------------------------------------------------------

    def _verify_ipn_signature(self, body: bytes, signature: str) -> bool:
        """验证 NOWPayments IPN 回调签名

        算法：
        1. 解析 body 为 dict
        2. 按 key 字母排序
        3. JSON.stringify (sorted keys)
        4. HMAC-SHA512(sorted_json, ipn_secret)
        5. 与 x-nowpayments-sig 头比对
        """
        try:
            data = json.loads(body)
            sorted_data = json.dumps(
                data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            expected = hmac.new(
                self._ipn_secret.encode("utf-8"),
                sorted_data.encode("utf-8"),
                hashlib.sha512,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                logger.warning(
                    "NOWPayments IPN sig mismatch: expected={}, got={}, sorted_data={}",
                    expected[:16] + "...",
                    signature[:16] + "..." if len(signature) > 16 else signature,
                    sorted_data[:200],
                )
                return False
            return True
        except Exception as e:
            logger.warning("NOWPayments IPN verify failed: {}", e)
            return False

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
        """调用 NOWPayments 创建支付，返回充值地址

        price_currency 使用传入的 currency 参数（通常为 CNY），
        NOWPayments 会自动按实时汇率换算成 pay_currency 对应的加密货币。
        success_url 用作 ipn_callback_url。
        """
        price_currency = currency.upper()
        # minor units → 原始金额（如 1300 CNY cents → 13.00 CNY；1200 JPY → 1200 JPY）
        price_amount = self._from_minor_units(amount_cents, price_currency)
        price_currency = price_currency.lower()

        logger.info(
            "NOWPayments create: price_amount={}, price_currency={}, pay_currency={}",
            price_amount,
            price_currency,
            self._pay_currency,
        )

        try:
            data = await self._post(
                "/v1/payment",
                {
                    "price_amount": price_amount,
                    "price_currency": price_currency,
                    "pay_currency": self._pay_currency,
                    "ipn_callback_url": success_url,
                    "order_id": order_no,
                    "order_description": plan_name,
                },
            )

            payment_id = str(data.get("payment_id", ""))
            pay_address = data.get("pay_address", "")
            pay_amount = data.get("pay_amount", 0)

            if not payment_id or not pay_address:
                error_msg = data.get("message", "No payment address returned")
                logger.error("NOWPayments create failed: {}", error_msg)
                return PaymentIntentResult(
                    success=False,
                    error_message=error_msg,
                    raw_data=data,
                )

            # checkout_url 设为充值地址（用于生成 QR 码）
            return PaymentIntentResult(
                success=True,
                provider_tx_id=payment_id,
                checkout_url=pay_address,
                client_secret=str(pay_amount),
                raw_data=data,
            )

        except Exception as e:
            logger.opt(exception=True).error("NOWPayments create_payment error: {}", e)
            return PaymentIntentResult(
                success=False,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Webhook 验签（IPN 回调）
    # ------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        """验证 NOWPayments IPN 回调

        回调是 POST JSON，x-nowpayments-sig 头包含 HMAC-SHA512 签名。
        """
        try:
            # 验签
            signature = headers.get("x-nowpayments-sig", "")
            if not signature:
                logger.warning("NOWPayments IPN: missing signature")
                return WebhookEvent(event_type=WebhookEventType.UNKNOWN, raw_event={})

            if self._ipn_secret and not self._verify_ipn_signature(payload, signature):
                logger.warning("NOWPayments IPN: signature verification failed")
                return WebhookEvent(event_type=WebhookEventType.UNKNOWN, raw_event={})

            data = json.loads(payload)
            payment_status = data.get("payment_status", "")
            payment_id = str(data.get("payment_id", ""))
            order_id = str(data.get("order_id", ""))
            price_amount = data.get("price_amount", 0)
            price_currency = str(data.get("price_currency") or "USD").upper()

            logger.info(
                "NOWPayments IPN: status={}, payment_id={}, order_id={}",
                payment_status,
                payment_id,
                order_id,
            )

            # 金额转换（price_currency 对应的主单位 → currency-specific minor units）
            amount_cents = self._to_minor_units(price_amount, price_currency)

            # 状态映射
            if payment_status in ("finished", "confirmed", "sending"):
                event_type = WebhookEventType.PAYMENT_COMPLETED
            elif payment_status in ("expired", "failed"):
                event_type = WebhookEventType.PAYMENT_EXPIRED
            elif payment_status == "refunded":
                event_type = WebhookEventType.PAYMENT_REFUNDED
            elif payment_status in (
                "waiting",
                "confirming",
                "partially_paid",
            ):
                # 中间状态，暂不处理
                return WebhookEvent(event_type=WebhookEventType.UNKNOWN, raw_event=data)
            else:
                return WebhookEvent(event_type=WebhookEventType.UNKNOWN, raw_event=data)

            return WebhookEvent(
                event_type=event_type,
                provider_tx_id=payment_id,
                order_no=order_id,
                amount_cents=amount_cents,
                currency=price_currency,
                raw_event=data,
            )

        except Exception as e:
            logger.error("NOWPayments webhook verification error: {}", e)
            return WebhookEvent(
                event_type=WebhookEventType.UNKNOWN,
                raw_event={"error": str(e)},
            )

    # ------------------------------------------------------------------
    # 退款（NOWPayments 不支持 API 退款）
    # ------------------------------------------------------------------

    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: int | None = None,
        reason: str | None = None,
    ) -> RefundResult:
        """NOWPayments 不支持 API 退款，需在 Dashboard 手动操作"""
        logger.warning(
            "NOWPayments refund not supported via API. "
            "Payment ID: {}, process manually in dashboard.",
            provider_tx_id,
        )
        return RefundResult(
            success=False,
            error_message="NOWPayments does not support API refunds. "
            "Please process manually in the dashboard.",
        )

    # ------------------------------------------------------------------
    # 支付状态查询
    # ------------------------------------------------------------------

    async def get_payment_status(self, provider_tx_id: str) -> PaymentStatusResult:
        """查询支付状态"""
        try:
            data = await self._get(f"/v1/payment/{provider_tx_id}")

            status = data.get("payment_status", "unknown")
            price_amount = data.get("price_amount", 0)
            price_currency = str(data.get("price_currency") or "USD").upper()

            # 映射到统一状态
            if status in ("finished", "confirmed"):
                unified = "paid"
            elif status in ("waiting", "confirming", "sending"):
                unified = "pending"
            elif status == "partially_paid":
                unified = "partial"
            elif status in ("expired", "failed"):
                unified = "expired"
            elif status == "refunded":
                unified = "refunded"
            else:
                unified = "unknown"

            amount_cents = self._to_minor_units(price_amount, price_currency)

            return PaymentStatusResult(
                success=True,
                status=unified,
                provider_tx_id=str(data.get("payment_id", provider_tx_id)),
                amount_cents=amount_cents,
                currency=price_currency,
                raw_data=data,
            )

        except Exception as e:
            logger.opt(exception=True).error(
                "NOWPayments get_payment_status error: {}", e
            )
            return PaymentStatusResult(
                success=False,
                error_message=str(e),
            )
