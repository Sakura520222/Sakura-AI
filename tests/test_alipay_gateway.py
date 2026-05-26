"""AlipayGateway 单元测试

覆盖：创建支付（电脑网站支付 page.pay）、Webhook 验签、退款、支付状态查询。
使用 mock 替代真实 RSA2 签名和 HTTP 调用。
"""

from unittest.mock import patch

import pytest

from backend.services.payment.gateway_base import WebhookEventType
from backend.services.payment.alipay_gateway import AlipayGateway


@pytest.fixture
def gateway():
    return AlipayGateway(
        api_key="2021000000000000",
        webhook_secret="fake_private_key_for_testing",
        alipay_public_key="fake_public_key_for_testing",
    )


class TestCreatePayment:
    """创建支付（电脑网站支付 page.pay）"""

    @pytest.mark.asyncio
    async def test_page_pay_success(self, gateway):
        """构建跳转 URL 成功"""
        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            result = await gateway.create_payment(
                order_no="ORDER-001",
                amount_cents=1000,
                currency="CNY",
                plan_name="Pro Monthly",
                user_id=42,
                success_url="https://example.com/api/webhook/alipay",
                cancel_url="https://example.com/billing/result",
            )

        assert result.success is True
        assert "openapi.alipay.com/gateway.do" in result.checkout_url
        assert "method=alipay.trade.page.pay" in result.checkout_url
        assert "FAST_INSTANT_TRADE_PAY" in result.checkout_url
        assert result.provider_tx_id == "ORDER-001"

    @pytest.mark.asyncio
    async def test_page_pay_sandbox(self):
        """沙箱环境使用沙箱网关 URL"""
        gw = AlipayGateway(
            api_key="sandbox_app_id",
            webhook_secret="fake_key",
            sandbox=True,
        )
        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            result = await gw.create_payment(
                order_no="ORDER-SB",
                amount_cents=500,
                currency="CNY",
                plan_name="Basic",
                user_id=1,
                success_url="https://example.com/notify",
                cancel_url="https://example.com/cancel",
            )

        assert result.success is True
        assert "openapi-sandbox.dl.alipaydev.com" in result.checkout_url

    @pytest.mark.asyncio
    async def test_sign_error(self, gateway):
        """签名异常"""
        with patch.object(
            AlipayGateway, "_sign_with_rsa2", side_effect=Exception("Key error")
        ):
            result = await gateway.create_payment(
                order_no="ORDER-ERR",
                amount_cents=500,
                currency="CNY",
                plan_name="Basic",
                user_id=1,
                success_url="https://example.com/notify",
                cancel_url="https://example.com/cancel",
            )

        assert result.success is False


class TestVerifyWebhook:
    """Webhook 验签"""

    def test_trade_success(self, gateway):
        """TRADE_SUCCESS 回调"""
        params = {
            "trade_no": "2026052400001000100000000001",
            "out_trade_no": "ORDER-001",
            "trade_status": "TRADE_SUCCESS",
            "total_amount": "10.00",
            "sign": "fake_sign",
            "sign_type": "RSA2",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        # Mock 验签（返回 True）
        with patch.object(AlipayGateway, "_verify_rsa2", return_value=True):
            event = gateway.verify_webhook(payload, {})

        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.order_no == "ORDER-001"
        assert event.provider_tx_id == "2026052400001000100000000001"
        assert event.amount_cents == 1000
        assert event.currency == "CNY"

    def test_trade_finished(self, gateway):
        """TRADE_FINISHED 回调"""
        params = {
            "trade_no": "2026052400001",
            "out_trade_no": "ORDER-002",
            "trade_status": "TRADE_FINISHED",
            "total_amount": "50.50",
            "sign": "fake_sign",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        with patch.object(AlipayGateway, "_verify_rsa2", return_value=True):
            event = gateway.verify_webhook(payload, {})

        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount_cents == 5050

    def test_trade_closed(self, gateway):
        """TRADE_CLOSED 回调"""
        params = {
            "trade_no": "2026052400001",
            "out_trade_no": "ORDER-003",
            "trade_status": "TRADE_CLOSED",
            "sign": "fake_sign",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        with patch.object(AlipayGateway, "_verify_rsa2", return_value=True):
            event = gateway.verify_webhook(payload, {})

        assert event.event_type == WebhookEventType.PAYMENT_EXPIRED

    def test_wait_buyer_pay_ignored(self, gateway):
        """WAIT_BUYER_PAY 被忽略"""
        params = {
            "trade_no": "2026052400001",
            "out_trade_no": "ORDER-004",
            "trade_status": "WAIT_BUYER_PAY",
            "sign": "fake_sign",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        with patch.object(AlipayGateway, "_verify_rsa2", return_value=True):
            event = gateway.verify_webhook(payload, {})

        assert event.event_type == WebhookEventType.UNKNOWN

    def test_no_sign(self, gateway):
        """缺少签名"""
        params = {
            "trade_no": "xxx",
            "out_trade_no": "ORDER-005",
            "trade_status": "TRADE_SUCCESS",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        event = gateway.verify_webhook(payload, {})
        assert event.event_type == WebhookEventType.UNKNOWN

    def test_no_public_key_skip_verify(self):
        """无支付宝公钥时跳过验签（仍解析事件）"""
        gw = AlipayGateway(
            api_key="test",
            webhook_secret="test",
            alipay_public_key="",  # 空 = 跳过验签
        )
        params = {
            "trade_no": "2026xxx",
            "out_trade_no": "ORDER-006",
            "trade_status": "TRADE_SUCCESS",
            "total_amount": "1.00",
            "sign": "whatever",
        }
        payload = "&".join(f"{k}={v}" for k, v in params.items()).encode("utf-8")

        event = gw.verify_webhook(payload, {})
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount_cents == 100


class TestRefund:
    """退款"""

    @pytest.mark.asyncio
    async def test_refund_success(self, gateway):
        """退款成功"""
        mock_data = {
            "alipay_trade_refund_response": {
                "code": "10000",
                "msg": "Success",
                "trade_no": "2026052400001",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.refund(
                    provider_tx_id="2026052400001", amount_cents=500
                )

        assert result.success is True
        assert result.status == "refunded"

    @pytest.mark.asyncio
    async def test_refund_error(self, gateway):
        """退款失败"""
        mock_data = {
            "alipay_trade_refund_response": {
                "code": "40004",
                "msg": "Business Failed",
                "sub_msg": "交易不存在",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.refund(provider_tx_id="nonexist")

        assert result.success is False
        assert "40004" in result.error_message


class TestGetPaymentStatus:
    """订单状态查询"""

    @pytest.mark.asyncio
    async def test_query_success(self, gateway):
        """查询成功"""
        mock_data = {
            "alipay_trade_query_response": {
                "code": "10000",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "10.00",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.get_payment_status("2026052400001")

        assert result.success is True
        assert result.status == "paid"
        assert result.amount_cents == 1000

    @pytest.mark.asyncio
    async def test_query_error(self, gateway):
        """查询网络错误"""
        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(
                AlipayGateway, "_post", side_effect=Exception("Connection error")
            ):
                result = await gateway.get_payment_status("nonexist")

        assert result.success is False
        assert "Connection error" in result.error_message


class TestCancelPayment:
    """关闭交易（alipay.trade.close）"""

    @pytest.mark.asyncio
    async def test_close_success(self, gateway):
        """code=10000 → 关闭成功"""
        mock_data = {
            "alipay_trade_close_response": {
                "code": "10000",
                "msg": "Success",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.cancel_payment(provider_tx_id="ORDER-001")

        assert result.success is True
        assert result.status == "cancelled"

    @pytest.mark.asyncio
    async def test_close_trade_not_found(self, gateway):
        """code=40004 → 交易不存在，视为取消成功"""
        mock_data = {
            "alipay_trade_close_response": {
                "code": "40004",
                "msg": "Business Failed",
                "sub_code": "ACQ.TRADE_NOT_EXIST",
                "sub_msg": "交易不存在",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.cancel_payment(provider_tx_id="ORDER-002")

        assert result.success is True
        assert result.status == "cancelled"

    @pytest.mark.asyncio
    async def test_close_other_error(self, gateway):
        """其他错误码 → 返回失败"""
        mock_data = {
            "alipay_trade_close_response": {
                "code": "20000",
                "msg": "Service Not Available",
                "sub_msg": "服务不可用",
            }
        }

        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(AlipayGateway, "_post", return_value=mock_data):
                result = await gateway.cancel_payment(provider_tx_id="ORDER-003")

        assert result.success is False
        assert "服务不可用" in result.error_message

    @pytest.mark.asyncio
    async def test_close_network_error(self, gateway):
        """网络异常 → 返回失败"""
        with patch.object(AlipayGateway, "_sign_with_rsa2", return_value="fake_sign"):
            with patch.object(
                AlipayGateway, "_post", side_effect=Exception("Connection timeout")
            ):
                result = await gateway.cancel_payment(provider_tx_id="ORDER-004")

        assert result.success is False
        assert "Connection timeout" in result.error_message
