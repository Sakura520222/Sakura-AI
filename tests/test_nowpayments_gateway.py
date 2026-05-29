"""NowPaymentsGateway 单元测试

覆盖：创建支付、IPN 回调验签、退款（不支持）、支付状态查询。
使用 mock 替代真实 HTTP 调用。
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from backend.services.payment.gateway_base import WebhookEventType
from backend.services.payment.nowpayments_gateway import NowPaymentsGateway

API_KEY = "test-api-key"
IPN_SECRET = "test-ipn-secret-key"


@pytest.fixture
def gateway():
    return NowPaymentsGateway(
        api_key=API_KEY,
        webhook_secret=IPN_SECRET,
        pay_currency="usdttrc20",
    )


def _make_ipn_signature(data: dict, secret: str) -> str:
    """Helper: 生成 HMAC-SHA512 签名"""
    sorted_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        sorted_json.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


class TestCurrencyConversion:
    """货币主单位和最小单位转换。"""

    def test_to_minor_units_jpy(self, gateway):
        assert gateway._to_minor_units(1200, "JPY") == 1200

    def test_to_minor_units_cny(self, gateway):
        assert gateway._to_minor_units("13.00", "CNY") == 1300

    def test_to_minor_units_none_returns_zero_and_logs_warning(self, gateway):
        with patch("backend.services.payment.nowpayments_gateway.logger") as mock_logger:
            result = gateway._to_minor_units(None, "USD")

        assert result == 0
        mock_logger.warning.assert_called_once_with(
            "Invalid amount '{}' for currency '{}', defaulting to 0",
            None,
            "USD",
        )

    def test_to_minor_units_invalid_string_returns_zero_and_logs_warning(self, gateway):
        with patch("backend.services.payment.nowpayments_gateway.logger") as mock_logger:
            result = gateway._to_minor_units("not-a-number", "USD")

        assert result == 0
        mock_logger.warning.assert_called_once_with(
            "Invalid amount '{}' for currency '{}', defaulting to 0",
            "not-a-number",
            "USD",
        )

    def test_to_minor_units_preserves_negative_amount_for_caller_validation(self, gateway):
        assert gateway._to_minor_units("-1.23", "USD") == -123

    def test_to_minor_units_handles_large_decimal_exactly(self, gateway):
        assert gateway._to_minor_units("9999999999999999.99", "USD") == 999999999999999999

    def test_from_minor_units_jpy(self, gateway):
        assert gateway._from_minor_units(1200, "JPY") == 1200

    def test_from_minor_units_cny_returns_float(self, gateway):
        result = gateway._from_minor_units(1300, "CNY")

        assert result == 13.0
        assert isinstance(result, float)


class TestCreatePayment:
    """创建支付"""

    @pytest.mark.asyncio
    async def test_create_success(self, gateway):
        """创建支付成功，返回充值地址"""
        mock_data = {
            "payment_id": 5077125051,
            "payment_status": "waiting",
            "pay_address": "TJYSqMhMr4BYU9gKzwNE7c4kZPQF29cVPn",
            "pay_amount": 10.5,
            "pay_currency": "usdttrc20",
            "price_amount": 10.0,
            "price_currency": "usd",
        }

        with patch.object(gateway, "_post", return_value=mock_data):
            result = await gateway.create_payment(
                order_no="ORDER-001",
                amount_cents=1000,
                currency="USD",
                plan_name="Pro Monthly",
                user_id=42,
                success_url="https://example.com/api/webhook/nowpayments",
                cancel_url="https://example.com/billing/",
            )

        assert result.success is True
        assert result.provider_tx_id == "5077125051"
        assert "TJYSqM" in result.checkout_url
        assert result.client_secret == "10.5"  # pay_amount

    @pytest.mark.asyncio
    async def test_create_jpy_uses_zero_decimal_main_units(self, gateway):
        """创建 JPY 支付时 amount_cents 已是最小单位，不应除以 100。"""
        mock_data = {
            "payment_id": 5077125058,
            "payment_status": "waiting",
            "pay_address": "JPY-address",
            "pay_amount": 1200,
            "pay_currency": "usdttrc20",
            "price_amount": 1200,
            "price_currency": "jpy",
        }

        with patch.object(gateway, "_post", return_value=mock_data) as mock_post:
            result = await gateway.create_payment(
                order_no="ORDER-JPY",
                amount_cents=1200,
                currency="JPY",
                plan_name="JPY Plan",
                user_id=42,
                success_url="https://example.com/api/webhook/nowpayments",
                cancel_url="https://example.com/billing/",
            )

        assert result.success is True
        mock_post.assert_awaited_once()
        request_body = mock_post.await_args.args[1]
        assert request_body["price_amount"] == 1200
        assert request_body["price_currency"] == "jpy"

    @pytest.mark.asyncio
    async def test_create_no_address(self, gateway):
        """API 未返回充值地址"""
        mock_data = {
            "payment_id": 0,
            "message": "Invalid API key",
        }

        with patch.object(gateway, "_post", return_value=mock_data):
            result = await gateway.create_payment(
                order_no="ORDER-ERR",
                amount_cents=500,
                currency="USD",
                plan_name="Basic",
                user_id=1,
                success_url="https://example.com/webhook",
                cancel_url="https://example.com/cancel",
            )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_create_http_error(self, gateway):
        """HTTP 错误"""
        with patch.object(
            gateway, "_post", side_effect=Exception("Connection refused")
        ):
            result = await gateway.create_payment(
                order_no="ORDER-ERR",
                amount_cents=500,
                currency="USD",
                plan_name="Basic",
                user_id=1,
                success_url="https://example.com/webhook",
                cancel_url="https://example.com/cancel",
            )

        assert result.success is False


class TestVerifyWebhook:
    """IPN 回调验签"""

    def test_payment_finished(self, gateway):
        """finished 状态回调"""
        data = {
            "payment_id": 5077125051,
            "payment_status": "finished",
            "pay_address": "TJYSqM",
            "price_amount": 10.0,
            "price_currency": "usd",
            "pay_currency": "usdttrc20",
            "order_id": "ORDER-001",
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.order_no == "ORDER-001"
        assert event.provider_tx_id == "5077125051"
        assert event.amount_cents == 1000
        assert event.currency == "USD"

    def test_payment_expired(self, gateway):
        """expired 状态回调"""
        data = {
            "payment_id": 5077125052,
            "payment_status": "expired",
            "order_id": "ORDER-002",
            "price_amount": 5.0,
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.PAYMENT_EXPIRED
        assert event.amount_cents == 500

    def test_jpy_payment_amount_uses_zero_decimal_minor_units(self, gateway):
        """JPY 等零小数位货币不应统一乘以 100。"""
        data = {
            "payment_id": 5077125058,
            "payment_status": "finished",
            "order_id": "ORDER-JPY",
            "price_amount": 1200,
            "price_currency": "jpy",
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount_cents == 1200
        assert event.currency == "JPY"

    def test_payment_failed(self, gateway):
        """failed 状态回调"""
        data = {
            "payment_id": 5077125053,
            "payment_status": "failed",
            "order_id": "ORDER-003",
            "price_amount": 20.0,
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.PAYMENT_EXPIRED

    def test_payment_refunded(self, gateway):
        """refunded 状态回调"""
        data = {
            "payment_id": 5077125054,
            "payment_status": "refunded",
            "order_id": "ORDER-004",
            "price_amount": 15.0,
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.PAYMENT_REFUNDED

    def test_waiting_ignored(self, gateway):
        """waiting 状态忽略"""
        data = {
            "payment_id": 5077125055,
            "payment_status": "waiting",
            "order_id": "ORDER-005",
        }
        body = json.dumps(data).encode("utf-8")
        sig = _make_ipn_signature(data, IPN_SECRET)
        headers = {"x-nowpayments-sig": sig}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.UNKNOWN

    def test_invalid_signature(self, gateway):
        """签名无效"""
        data = {
            "payment_id": 5077125056,
            "payment_status": "finished",
            "order_id": "ORDER-006",
        }
        body = json.dumps(data).encode("utf-8")
        headers = {"x-nowpayments-sig": "invalid_signature"}

        event = gateway.verify_webhook(body, headers)

        assert event.event_type == WebhookEventType.UNKNOWN

    def test_no_signature(self, gateway):
        """缺少签名头"""
        data = {"payment_id": 5077125057, "payment_status": "finished"}
        body = json.dumps(data).encode("utf-8")

        event = gateway.verify_webhook(body, {})

        assert event.event_type == WebhookEventType.UNKNOWN


class TestRefund:
    """退款（NOWPayments 不支持 API 退款）"""

    @pytest.mark.asyncio
    async def test_refund_not_supported(self, gateway):
        result = await gateway.refund(provider_tx_id="5077125051")

        assert result.success is False
        assert "not support" in result.error_message.lower()


class TestGetPaymentStatus:
    """支付状态查询"""

    @pytest.mark.asyncio
    async def test_query_finished(self, gateway):
        """查询已完成的支付"""
        mock_data = {
            "payment_id": 5077125051,
            "payment_status": "finished",
            "price_amount": 10.0,
        }

        with patch.object(gateway, "_get", return_value=mock_data):
            result = await gateway.get_payment_status("5077125051")

        assert result.success is True
        assert result.status == "paid"
        assert result.amount_cents == 1000

    @pytest.mark.asyncio
    async def test_query_jpy_uses_response_currency_minor_units(self, gateway):
        """查询 JPY 支付状态时使用响应币种的小数位。"""
        mock_data = {
            "payment_id": 5077125058,
            "payment_status": "finished",
            "price_amount": 1200,
            "price_currency": "jpy",
        }

        with patch.object(gateway, "_get", return_value=mock_data):
            result = await gateway.get_payment_status("5077125058")

        assert result.success is True
        assert result.status == "paid"
        assert result.amount_cents == 1200
        assert result.currency == "JPY"

    @pytest.mark.asyncio
    async def test_query_waiting(self, gateway):
        """查询等待中的支付"""
        mock_data = {
            "payment_id": 5077125051,
            "payment_status": "waiting",
            "price_amount": 10.0,
        }

        with patch.object(gateway, "_get", return_value=mock_data):
            result = await gateway.get_payment_status("5077125051")

        assert result.success is True
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_query_error(self, gateway):
        """查询错误"""
        with patch.object(
            gateway, "_get", side_effect=Exception("Not found")
        ):
            result = await gateway.get_payment_status("nonexist")

        assert result.success is False
        assert "Not found" in result.error_message
