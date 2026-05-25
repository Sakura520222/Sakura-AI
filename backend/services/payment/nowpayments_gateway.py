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
from typing import Optional

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
            resp = await client.get(
                f"{self._base_url}{path}", headers=self._headers
            )
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
        return ValueError(
            f"NOWPayments API error {resp.status_code}: {msg}"
        )

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
                data, sort_keys=True, separators=(",", ":")
            )
            expected = hmac.new(
                self._ipn_secret.encode("utf-8"),
                sorted_data.encode("utf-8"),
                hashlib.sha512,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
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
        metadata: Optional[dict[str, str]] = None,
    ) -> PaymentIntentResult:
        """调用 NOWPayments 创建支付，返回充值地址

        amount_cents 会转为美元金额（price_amount）。
        success_url 用作 ipn_callback_url。
        """
        # cents → 美元
        price_amount = amount_cents / 100

        try:
            data = await self._post(
                "/v1/payment",
                {
                    "price_amount": price_amount,
                    "price_currency": "usd",
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
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event={}
                )

            if self._ipn_secret and not self._verify_ipn_signature(
                payload, signature
            ):
                logger.warning("NOWPayments IPN: signature verification failed")
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event={}
                )

            data = json.loads(payload)
            payment_status = data.get("payment_status", "")
            payment_id = str(data.get("payment_id", ""))
            order_id = str(data.get("order_id", ""))
            price_amount = data.get("price_amount", 0)

            logger.info(
                "NOWPayments IPN: status={}, payment_id={}, order_id={}",
                payment_status,
                payment_id,
                order_id,
            )

            # 金额转换（美元 → cents）
            try:
                amount_cents = int(float(price_amount) * 100)
            except (ValueError, TypeError):
                amount_cents = 0

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
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=data
                )
            else:
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=data
                )

            return WebhookEvent(
                event_type=event_type,
                provider_tx_id=payment_id,
                order_no=order_id,
                amount_cents=amount_cents,
                currency="USD",
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
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
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

    async def get_payment_status(
        self, provider_tx_id: str
    ) -> PaymentStatusResult:
        """查询支付状态"""
        try:
            data = await self._get(f"/v1/payment/{provider_tx_id}")

            status = data.get("payment_status", "unknown")
            price_amount = data.get("price_amount", 0)

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

            try:
                amount_cents = int(float(price_amount) * 100)
            except (ValueError, TypeError):
                amount_cents = 0

            return PaymentStatusResult(
                success=True,
                status=unified,
                provider_tx_id=str(data.get("payment_id", provider_tx_id)),
                amount_cents=amount_cents,
                currency="USD",
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
