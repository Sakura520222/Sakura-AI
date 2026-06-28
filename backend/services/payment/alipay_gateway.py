"""支付宝电脑网站支付网关实现

使用支付宝开放平台「电脑网站支付」能力（alipay.trade.page.pay），
用户在电脑浏览器中跳转到支付宝页面完成付款。

接入条件：
- 支付宝企业账号或个体工商户
- 网站已通过 ICP 备案
- 费率 0.6%

关键概念：
- app_id: 支付宝开放平台应用 ID
- app_private_key: 应用私钥（RSA2）
- alipay_public_key: 支付宝公钥（用于验签回调）
- alipay.trade.page.pay: 电脑网站支付（浏览器跳转）
- product_code: FAST_INSTANT_TRADE_PAY
- return_url: 支付完成后前端回跳地址
- notify_url: 异步回调地址

签名：RSA2（SHA256WithRSA）
"""

from typing import Optional
from urllib.parse import parse_qs, urlencode

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


class AlipayGateway(PaymentGateway):
    """支付宝电脑网站支付网关（alipay.trade.page.pay）"""

    GATEWAY_URL = "https://openapi.alipay.com/gateway.do"
    GATEWAY_SANDBOX_URL = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"

    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        alipay_public_key: str = "",
        sandbox: bool = False,
    ):
        """
        Args:
            api_key: 支付宝 app_id
            webhook_secret: 应用私钥（RSA2 PKCS#8 PEM 格式）
            alipay_public_key: 支付宝公钥（用于验签回调）
            sandbox: 是否使用沙箱环境
        """
        self._app_id = api_key
        self._app_private_key = webhook_secret
        self._alipay_public_key = alipay_public_key
        self._sandbox = sandbox
        self._gateway_url = self.GATEWAY_SANDBOX_URL if sandbox else self.GATEWAY_URL

    # ------------------------------------------------------------------
    # 内部辅助：RSA2 签名
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_pem(raw: str, key_type: str = "private") -> str:
        """将各种格式的密钥标准化为合法 PEM

        支持以下输入格式：
        - 完整 PEM（含 BEGIN/END 头尾）
        - 纯 Base64 字符串（自动换行 + 添加头尾）
        - PKCS#1 (BEGIN RSA PRIVATE KEY) / PKCS#8 (BEGIN PRIVATE KEY)
        """
        import re
        import textwrap

        raw = raw.strip()

        # 已经是完整 PEM
        if "BEGIN" in raw and "END" in raw:
            return raw

        # 纯 Base64 — 去掉空白后换行
        b64 = re.sub(r"\s+", "", raw)
        lines = textwrap.wrap(b64, width=64)
        body = "\n".join(lines)

        if key_type == "private":
            # 优先尝试 PKCS#8（通用格式）
            return (
                "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"
            )
        else:
            return "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----"

    @staticmethod
    def _load_private_key(raw: str):
        """加载 RSA 私钥，自动适配 PKCS#1 / PKCS#8 / 纯 Base64"""
        from cryptography.hazmat.primitives import serialization

        pem = AlipayGateway._normalize_pem(raw, "private")
        pem_bytes = pem.encode("utf-8")

        # 尝试 PKCS#8（推荐格式）
        try:
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception:
            pass

        # 尝试 PKCS#1（传统 RSA 格式）
        pkcs1_pem = pem
        if "BEGIN PRIVATE KEY" in pkcs1_pem:
            pkcs1_pem = pkcs1_pem.replace(
                "BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY"
            ).replace("END PRIVATE KEY", "END RSA PRIVATE KEY")
        try:
            return serialization.load_pem_private_key(
                pkcs1_pem.encode("utf-8"), password=None
            )
        except Exception:
            pass

        # 最后尝试 DER 格式
        import base64

        try:
            lines = [
                ln
                for ln in pem.split("\n")
                if ln.strip() and not ln.startswith("-----")
            ]
            der_bytes = base64.b64decode("".join(lines))
            return serialization.load_der_private_key(der_bytes, password=None)
        except Exception:
            pass

        raise ValueError(
            "无法解析应用私钥。请确认密钥为 RSA2 格式（PKCS#8 或 PKCS#1 PEM）。"
            "可使用支付宝密钥生成工具重新生成。"
        )

    @staticmethod
    def _load_public_key(raw: str):
        """加载 RSA 公钥，自动适配各种格式"""
        from cryptography.hazmat.primitives import serialization

        pem = AlipayGateway._normalize_pem(raw, "public")
        pem_bytes = pem.encode("utf-8")

        try:
            return serialization.load_pem_public_key(pem_bytes)
        except Exception:
            pass

        # 尝试 DER
        import base64

        try:
            lines = [
                ln
                for ln in pem.split("\n")
                if ln.strip() and not ln.startswith("-----")
            ]
            der_bytes = base64.b64decode("".join(lines))
            return serialization.load_der_public_key(der_bytes)
        except Exception:
            pass

        raise ValueError(
            "无法解析支付宝公钥。请在开放平台「应用详情 > 接口加签方式」中复制支付宝公钥。"
        )

    @staticmethod
    def _sign_with_rsa2(params: dict, private_key_pem: str) -> str:
        """使用 RSA2 (SHA256WithRSA) 对参数签名"""
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        # 排序拼接
        unsigned_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v)

        private_key = AlipayGateway._load_private_key(private_key_pem)

        signature = private_key.sign(
            unsigned_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    @staticmethod
    def _verify_rsa2(params: dict, sign: str, public_key_pem: str) -> bool:
        """使用支付宝公钥验签"""
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        # 移除 sign 和 sign_type
        verify_params = {
            k: v for k, v in params.items() if k not in ("sign", "sign_type")
        }
        unsigned_string = "&".join(
            f"{k}={v}" for k, v in sorted(verify_params.items()) if v
        )

        try:
            public_key = AlipayGateway._load_public_key(public_key_pem)

            public_key.verify(
                base64.b64decode(sign),
                unsigned_string.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception as e:
            logger.warning("Alipay RSA2 verify failed: {}", e)
            return False

    # ------------------------------------------------------------------
    # 内部 HTTP 辅助：处理支付宝 GBK/UTF-8 响应
    # ------------------------------------------------------------------

    async def _post(self, params: dict) -> dict:
        """POST 到支付宝网关，自动处理 GBK/UTF-8 编码响应

        支付宝部分接口返回 GBK 编码的中文错误消息，
        httpx 默认用 UTF-8 解码会失败，此处手动解码。
        """
        import json as _json

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._gateway_url,
                data=params,
            )
            resp.raise_for_status()
            raw = resp.content
            # 优先 UTF-8，失败则回退 GBK
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
            return _json.loads(text)

    # ------------------------------------------------------------------
    # 创建支付（电脑网站支付）
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
        """构建 alipay.trade.page.pay 跳转 URL

        电脑网站支付不通过服务端 API 调用获取二维码，
        而是构建签名参数后拼接网关 URL，由浏览器直接跳转。

        Args:
            success_url: 用作 notify_url（支付宝异步回调地址）
            cancel_url: 用作 return_url（支付完成后前端回跳地址）
        """
        import json

        # 金额转换：cents → 元
        total_amount = f"{amount_cents / 100:.2f}"

        biz_content = {
            "out_trade_no": order_no,
            "total_amount": total_amount,
            "subject": plan_name,
            "product_code": "FAST_INSTANT_TRADE_PAY",
        }

        params = {
            "app_id": self._app_id,
            "method": "alipay.trade.page.pay",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": self._now_timestamp(),
            "version": "1.0",
            "notify_url": success_url,
            "return_url": cancel_url,
            # ensure_ascii=True: biz_content 将被 URL 编码，非 ASCII 字符需先转义
            "biz_content": json.dumps(biz_content, ensure_ascii=True),
        }

        try:
            # 签名
            sign = self._sign_with_rsa2(params, self._app_private_key)
            params["sign"] = sign

            # 构建跳转 URL，浏览器直接访问即可进入支付宝收银台
            checkout_url = f"{self._gateway_url}?{urlencode(params)}"

            return PaymentIntentResult(
                success=True,
                provider_tx_id=order_no,
                checkout_url=checkout_url,
                raw_data={"method": "page.pay", "order_no": order_no},
            )

        except Exception as e:
            logger.opt(exception=True).error("Alipay create_payment error: {}", e)
            return PaymentIntentResult(
                success=False,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Webhook 验签（支付宝异步通知）
    # ------------------------------------------------------------------

    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        """验证支付宝异步通知签名

        支付宝回调是 POST form-urlencoded，包含 trade_status、out_trade_no 等。
        验签：使用支付宝公钥验证 RSA2 签名。
        """
        try:
            body = payload.decode("utf-8")
            params = parse_qs(body)
            # parse_qs 返回 list 值，转为单值
            flat_params = {
                k: v[0] if isinstance(v, list) and len(v) == 1 else v
                for k, v in params.items()
            }

            sign = flat_params.get("sign", "")
            if not sign:
                logger.warning("Alipay webhook: missing sign")
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=flat_params
                )

            # 验签
            if not self._alipay_public_key:
                logger.warning("Alipay webhook: missing public key, rejecting callback")
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=flat_params
                )

            verified = self._verify_rsa2(flat_params, sign, self._alipay_public_key)
            if not verified:
                logger.warning("Alipay webhook: signature verification failed")
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=flat_params
                )

            trade_status = flat_params.get("trade_status", "")
            out_trade_no = flat_params.get("out_trade_no", "")
            trade_no = flat_params.get("trade_no", "")
            total_amount = flat_params.get("total_amount", "0")

            # 解析金额
            try:
                amount_cents = int(float(total_amount) * 100)
            except (ValueError, TypeError):
                amount_cents = 0

            # 交易状态映射
            if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                event_type = WebhookEventType.PAYMENT_COMPLETED
            elif trade_status == "TRADE_CLOSED":
                event_type = WebhookEventType.PAYMENT_EXPIRED
            elif trade_status == "WAIT_BUYER_PAY":
                # 等待付款，不处理
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=flat_params
                )
            else:
                return WebhookEvent(
                    event_type=WebhookEventType.UNKNOWN, raw_event=flat_params
                )

            return WebhookEvent(
                event_type=event_type,
                provider_tx_id=trade_no,
                order_no=out_trade_no,
                amount_cents=amount_cents,
                currency="CNY",
                raw_event=flat_params,
            )

        except Exception as e:
            logger.error("Alipay webhook verification error: {}", e)
            return WebhookEvent(
                event_type=WebhookEventType.UNKNOWN,
                raw_event={"error": str(e)},
            )

    # ------------------------------------------------------------------
    # 退款
    # ------------------------------------------------------------------

    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> RefundResult:
        """调用 alipay.trade.refund 退款"""
        import json

        biz_content: dict = {"trade_no": provider_tx_id}
        if amount_cents is not None:
            biz_content["refund_amount"] = f"{amount_cents / 100:.2f}"
        if reason:
            biz_content["refund_reason"] = reason

        params = {
            "app_id": self._app_id,
            "method": "alipay.trade.refund",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": self._now_timestamp(),
            "version": "1.0",
            "biz_content": json.dumps(biz_content, ensure_ascii=True),
        }

        try:
            sign = self._sign_with_rsa2(params, self._app_private_key)
            params["sign"] = sign

            data = await self._post(params)

            resp_key = "alipay_trade_refund_response"
            result = data.get(resp_key, {})
            code = result.get("code")

            if code == "10000":
                return RefundResult(
                    success=True,
                    refund_id=result.get("trade_no", provider_tx_id),
                    amount_cents=amount_cents or 0,
                    status="refunded",
                )

            error_msg = result.get("sub_msg") or result.get("msg", "Refund failed")
            return RefundResult(
                success=False,
                error_message=f"[{code}] {error_msg}",
            )

        except Exception as e:
            logger.opt(exception=True).error("Alipay refund error: {}", e)
            return RefundResult(success=False, error_message=str(e))

    # ------------------------------------------------------------------
    # 订单状态查询
    # ------------------------------------------------------------------

    async def get_payment_status(
        self,
        provider_tx_id: str,
    ) -> PaymentStatusResult:
        """调用 alipay.trade.query 查询订单状态"""
        import json

        biz_content = {"trade_no": provider_tx_id}

        params = {
            "app_id": self._app_id,
            "method": "alipay.trade.query",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": self._now_timestamp(),
            "version": "1.0",
            "biz_content": json.dumps(biz_content, ensure_ascii=True),
        }

        try:
            sign = self._sign_with_rsa2(params, self._app_private_key)
            params["sign"] = sign

            data = await self._post(params)

            resp_key = "alipay_trade_query_response"
            result = data.get(resp_key, {})
            code = result.get("code")

            if code == "10000":
                trade_status = result.get("trade_status", "")
                status_map = {
                    "WAIT_BUYER_PAY": "pending",
                    "TRADE_SUCCESS": "paid",
                    "TRADE_FINISHED": "paid",
                    "TRADE_CLOSED": "closed",
                }
                total_amount = result.get("total_amount", "0")
                try:
                    amount_cents = int(float(total_amount) * 100)
                except (ValueError, TypeError):
                    amount_cents = 0

                return PaymentStatusResult(
                    success=True,
                    status=status_map.get(trade_status, trade_status),
                    provider_tx_id=provider_tx_id,
                    amount_cents=amount_cents,
                    currency="CNY",
                    raw_data=result,
                )

            error_msg = result.get("sub_msg") or result.get("msg", "Query failed")
            return PaymentStatusResult(
                success=False,
                error_message=f"[{code}] {error_msg}",
                raw_data=result,
            )

        except Exception as e:
            logger.opt(exception=True).error("Alipay get_payment_status error: {}", e)
            return PaymentStatusResult(
                success=False,
                error_message=str(e),
            )

    async def cancel_payment(
        self,
        provider_tx_id: str,
    ) -> RefundResult:
        """支付宝关闭交易（alipay.trade.close）

        电脑网站支付使用 alipay.trade.close 关闭未付款订单
        """
        import json

        try:
            # ensure_ascii=True: biz_content 将被 URL 编码，非 ASCII 字符需先转义
            biz_content = json.dumps(
                {"out_trade_no": provider_tx_id},
                ensure_ascii=True,
            )
            params = {
                "app_id": self._app_id,
                "method": "alipay.trade.close",
                "charset": "utf-8",
                "sign_type": "RSA2",
                "timestamp": self._now_timestamp(),
                "version": "1.0",
                "biz_content": biz_content,
            }
            sign = self._sign_with_rsa2(params, self._app_private_key)
            params["sign"] = sign

            result = await self._post(params)
            resp = result.get("alipay_trade_close_response", {})
            code = resp.get("code", "")
            if code == "10000":
                logger.info(
                    "Alipay trade closed: out_trade_no={}",
                    provider_tx_id,
                )
                return RefundResult(
                    success=True,
                    status="cancelled",
                )
            # 交易不存在（用户未跳转到支付宝）视为取消成功
            if code == "40004":
                logger.info(
                    "Alipay trade not found, treat as cancelled: out_trade_no={}",
                    provider_tx_id,
                )
                return RefundResult(
                    success=True,
                    status="cancelled",
                )
            return RefundResult(
                success=False,
                error_message=resp.get("sub_msg", resp.get("msg", "Unknown error")),
            )
        except Exception as e:
            logger.opt(exception=True).error("Alipay close error: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _now_timestamp() -> str:
        """当前时间戳（北京时间，支付宝 API 要求），格式 YYYY-MM-DD HH:mm:ss"""
        from datetime import datetime, timezone, timedelta

        # 支付宝 API timestamp 参数要求北京时间（东八区）
        bj_tz = timezone(timedelta(hours=8))
        return datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S")
