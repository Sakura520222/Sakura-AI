"""自建 TRON USDT TRC-20 收款网关

直接通过 TronGrid API 监控链上 USDT 转账，无需第三方支付平台。
资金直接到达你指定的 TRON 钱包，零平台手续费。

工作原理：
1. 管理员配置一个固定的 TRON 钱包地址作为收款地址
2. 创建支付时，将 CNY 金额转换为 USDT，并附加唯一后缀
   （基于 order_no 哈希的微小额，确保每笔订单金额唯一）
3. 前端展示收款地址 + 唯一 USDT 金额 + QR 码
4. 每 15 秒轮询 TronGrid API 检查到账
5. 匹配到对应金额的转账后确认订单

接入条件：
- 一个 TRON 钱包地址（Outcome Wallet）
- 可选：TronGrid API Key（提高频率限制，免费申请）
- 钱包中需要少量 TRX 用于接收 USDT（约 1-2 TRX/笔，由发送方支付）

优势：
- 零平台手续费（仅 TRON 网络费 ~1 TRX ≈ $0.04）
- 资金直达你的钱包，不经第三方
- 无最低金额限制
- 无需 KYC
"""

import hashlib

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

# USDT TRC-20 合约地址
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class TronGateway(PaymentGateway):
    """自建 TRON USDT 收款网关（通过 TronGrid API）"""

    TRONGRID_API = "https://api.trongrid.io"
    TRONSCAN_API = "https://apilist.tronscanapi.com"

    def __init__(
        self,
        wallet_address: str,
        api_key: str = "",
    ):
        """
        Args:
            wallet_address: 收款 TRON 钱包地址（Base58 格式）
            api_key: TronGrid API Key（可选，提高频率限制）
        """
        self._wallet_address = wallet_address
        self._api_key = api_key

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["TRON-PRO-API-KEY"] = self._api_key
        return headers

    # ------------------------------------------------------------------
    # 唯一金额计算
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_unique_amount(base_usdt: float, order_no: str) -> float:
        """为订单计算唯一的 USDT 支付金额

        在基础金额上附加一个基于 order_no 的微量后缀（0.000001~0.009999），
        确保同一收款地址上的不同订单金额不会重复。
        """
        h = hashlib.md5(order_no.encode()).hexdigest()
        suffix = int(h[:4], 16) % 10000  # 0~9999
        return round(base_usdt + suffix * 0.000001, 6)

    @staticmethod
    def _extract_base_amount(unique_usdt: float, order_no: str) -> float:
        """从唯一金额中还原基础金额（用于验证）"""
        h = hashlib.md5(order_no.encode()).hexdigest()
        suffix = int(h[:4], 16) % 10000
        return round(unique_usdt - suffix * 0.000001, 6)

    # ------------------------------------------------------------------
    # TronGrid API
    # ------------------------------------------------------------------

    async def _get_trc20_transfers(self, address: str, limit: int = 20) -> list[dict]:
        """查询最近 TRC-20 USDT 转账到指定地址"""
        url = f"{self.TRONGRID_API}/v1/accounts/{address}/transactions/trc20"
        params = {
            "limit": limit,
            "contract_address": USDT_TRC20_CONTRACT,
            "order_by": "block_timestamp,desc",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params, headers=self._headers)
                if resp.status_code >= 400:
                    logger.warning("TronGrid API error: status={}", resp.status_code)
                    return []
                data = resp.json()
                return data.get("data", [])
        except Exception as e:
            logger.warning("TronGrid query failed: {}", e)
            return []

    def _parse_transfer_amount(self, raw: str) -> float:
        """USDT TRC-20 金额解析（6 位小数，字符串→float）"""
        try:
            return int(raw) / 1_000_000
        except (ValueError, TypeError):
            return 0.0

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
        """创建 TRON USDT 收款订单

        currency 应为 USD/CNY，由调用方转换后传入对应的 USDT 金额。
        内部计算唯一金额并返回收款信息。
        """
        # amount_cents 是 USDT cents（如 1861 = $18.61 USDT）
        base_usdt = amount_cents / 100
        unique_usdt = self._calculate_unique_amount(base_usdt, order_no)

        logger.info(
            "TronGateway create: wallet={}, base_usdt={}, unique_usdt={}, order={}",
            self._wallet_address,
            base_usdt,
            unique_usdt,
            order_no,
        )

        return PaymentIntentResult(
            success=True,
            provider_tx_id=order_no,  # 用 order_no 作为 tx_id
            checkout_url=self._wallet_address,  # 收款地址
            client_secret=str(unique_usdt),  # 存储唯一金额
            raw_data={
                "pay_address": self._wallet_address,
                "pay_amount": str(unique_usdt),
                "pay_currency": "usdttrc20",
                "price_amount": str(base_usdt),
                "price_currency": "usd",
                "payment_id": order_no,
            },
        )

    # ------------------------------------------------------------------
    # Webhook（TRON 没有主动回调，使用轮询模式）
    # ------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        """TRON 不支持 webhook，始终返回 UNKNOWN"""
        return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

    # ------------------------------------------------------------------
    # 退款（TRON 链上无法自动退款）
    # ------------------------------------------------------------------

    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: int | None = None,
        reason: str | None = None,
    ) -> RefundResult:
        """TRON 链上无法自动退款，需手动操作"""
        return RefundResult(
            success=False,
            error_message="Manual refund required for TRON payments",
        )

    # ------------------------------------------------------------------
    # 查询支付状态（通过 TronGrid API）
    # ------------------------------------------------------------------

    async def get_payment_status(
        self,
        provider_tx_id: str,
    ) -> PaymentStatusResult:
        """查询 TRON 链上 USDT 转账状态

        provider_tx_id 为 order_no，通过 metadata 中的唯一金额匹配转账。
        """
        # provider_tx_id 实际是 order_no
        # 调用方应通过 metadata 获取 unique_usdt 金额
        # 此处查询最近转账，由上层根据金额匹配
        transfers = await self._get_trc20_transfers(self._wallet_address, limit=20)

        return PaymentStatusResult(
            success=True,
            status="waiting",
            provider_tx_id=provider_tx_id,
            raw_data={
                "transfers": transfers,
                "wallet_address": self._wallet_address,
            },
        )

    async def check_payment_by_amount(
        self,
        order_no: str,
        expected_usdt: float,
    ) -> PaymentStatusResult:
        """按唯一金额匹配链上转账

        遍历最近的 USDT 转账记录，查找与 expected_usdt 匹配的转入。
        """
        transfers = await self._get_trc20_transfers(self._wallet_address, limit=50)

        for tx in transfers:
            # 检查是否是转入
            to_addr = tx.get("to", "")
            if to_addr != self._wallet_address:
                continue

            # 检查 token 类型
            token_info = tx.get("token_info", {})
            if (
                token_info.get("symbol", "").upper() != "USDT"
                and tx.get("contract_address", "") != USDT_TRC20_CONTRACT
            ):
                continue

            # 解析金额
            raw_value = tx.get("value", "0")
            amount = self._parse_transfer_amount(str(raw_value))

            # 精确匹配唯一金额（允许 ±0.000001 误差）
            if abs(amount - expected_usdt) < 0.000002:
                tx_hash = tx.get("transaction_id", "")
                block_ts = tx.get("block_timestamp", 0)

                logger.info(
                    "TronGateway: matched transfer amount={}, "
                    "expected={}, tx_hash={}, order={}",
                    amount,
                    expected_usdt,
                    tx_hash[:16],
                    order_no,
                )

                return PaymentStatusResult(
                    success=True,
                    status="completed",
                    provider_tx_id=tx_hash,
                    amount_cents=int(amount * 1_000_000),
                    currency="USDT",
                    raw_data={
                        "payment_status": "finished",
                        "tx_hash": tx_hash,
                        "amount": amount,
                        "block_timestamp": block_ts,
                        "from_address": tx.get("from", ""),
                    },
                )

        # 未找到匹配转账
        return PaymentStatusResult(
            success=True,
            status="waiting",
            provider_tx_id=order_no,
            raw_data={"payment_status": "waiting"},
        )
