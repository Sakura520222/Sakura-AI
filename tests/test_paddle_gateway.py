"""PaddleGateway 单元测试

覆盖：创建支付、Webhook 验证、退款、支付状态查询。

由于 paddle-python-sdk 是可选依赖，测试使用 sys.modules mock 模拟 SDK。
"""

import hashlib
import hmac
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services.payment.gateway_base import WebhookEventType


@pytest.fixture(autouse=True)
def paddle_sdk_mock():
    """自动注入 paddle_billing SDK mock

    使用 MagicMock 作为模块，这样所有子属性访问都会自动返回 MagicMock。
    """
    # 保存原始模块（如果存在）
    original_modules = {}
    for key in list(sys.modules.keys()):
        if key.startswith("paddle_billing"):
            original_modules[key] = sys.modules.pop(key)

    # 创建 MagicMock 作为 paddle_billing 包
    mock_sdk = MagicMock()
    sys.modules["paddle_billing"] = mock_sdk

    # 为常用子路径也注册 mock，确保 from X import Y 正常工作
    sub_paths = [
        "paddle_billing.Entities",
        "paddle_billing.Entities.Shared",
        "paddle_billing.Resources",
        "paddle_billing.Resources.Transactions",
        "paddle_billing.Resources.Transactions.Operations",
        "paddle_billing.Resources.Transactions.Operations.Create",
        "paddle_billing.Resources.Transactions.Operations.Price",
        "paddle_billing.Resources.Adjustments",
        "paddle_billing.Resources.Adjustments.Operations",
        "paddle_billing.Resources.Adjustments.Operations.Create",
    ]
    for path in sub_paths:
        sys.modules[path] = MagicMock()

    yield mock_sdk, mock_sdk.Client

    # 清理
    for key in list(sys.modules.keys()):
        if key.startswith("paddle_billing"):
            sys.modules.pop(key, None)
    # 恢复原始模块
    sys.modules.update(original_modules)


from backend.services.payment.paddle_gateway import PaddleGateway


@pytest.fixture
def gateway():
    return PaddleGateway(api_key="test_12345", webhook_secret="whsec_test_paddle")


def _make_paddle_signature(
    payload: bytes, secret: str, timestamp: str = "1700000000"
) -> str:
    """Helper: 生成合法的 Paddle webhook 签名"""
    payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    signed_payload = f"{timestamp}:{payload_str}"
    sig = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"ts={timestamp};h1={sig}"


class TestPaddleGatewayCreatePayment:
    @pytest.mark.asyncio
    async def test_create_payment_success(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_tx = MagicMock()
        mock_tx.id = "txn_01test123"
        mock_tx.status = "ready"
        mock_tx.checkout = MagicMock()
        mock_tx.checkout.url = "https://checkout.paddle.com/test?_ptxn=txn_01test123"

        mock_client_instance = MagicMock()
        mock_client_instance.transactions.create.return_value = mock_tx
        mock_client_cls.return_value = mock_client_instance

        result = await gateway.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="USD",
            plan_name="Test Plan",
            user_id=1,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is True
        assert result.provider_tx_id == "txn_01test123"
        from urllib.parse import urlparse

        assert urlparse(result.checkout_url).hostname == "checkout.paddle.com"

    @pytest.mark.asyncio
    async def test_create_payment_sdk_error(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock
        mock_client_cls.side_effect = Exception("Paddle API error")

        result = await gateway.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="USD",
            plan_name="Test Plan",
            user_id=1,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is False
        assert "Paddle API error" in result.error_message


class TestPaddleGatewayVerifyWebhook:
    def test_verify_webhook_transaction_completed(self, gateway):
        payload_dict = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_01completed",
                "attributes": {
                    "custom_data": {
                        "order_no": "ORD20240101000000ABCD1234",
                        "user_id": "1",
                    },
                    "details": {"totals": {"total": "1000"}},
                    "currency_code": "USD",
                },
            },
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        sig = _make_paddle_signature(payload, "whsec_test_paddle")

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": sig},
        )

        assert result.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert result.provider_tx_id == "txn_01completed"
        assert result.order_no == "ORD20240101000000ABCD1234"
        assert result.amount_cents == 1000
        assert result.currency == "USD"

    def test_verify_webhook_transaction_paid(self, gateway):
        payload_dict = {
            "event_type": "transaction.paid",
            "data": {
                "id": "txn_01paid",
                "attributes": {
                    "custom_data": {"order_no": "ORD20240101000000PAID1234"},
                    "details": {"totals": {"total": "2000"}},
                    "currency_code": "EUR",
                },
            },
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        sig = _make_paddle_signature(payload, "whsec_test_paddle")

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": sig},
        )

        assert result.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert result.provider_tx_id == "txn_01paid"

    def test_verify_webhook_transaction_canceled(self, gateway):
        payload_dict = {
            "event_type": "transaction.canceled",
            "data": {
                "id": "txn_01canceled",
                "attributes": {
                    "custom_data": {"order_no": "ORD20240101000000CANCEL"},
                    "currency_code": "USD",
                },
            },
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        sig = _make_paddle_signature(payload, "whsec_test_paddle")

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": sig},
        )

        assert result.event_type == WebhookEventType.PAYMENT_EXPIRED

    def test_verify_webhook_adjustment_refund(self, gateway):
        payload_dict = {
            "event_type": "adjustment.created",
            "data": {
                "id": "adj_01refund",
                "attributes": {
                    "action": "refund",
                    "transaction_id": "txn_01original",
                },
            },
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        sig = _make_paddle_signature(payload, "whsec_test_paddle")

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": sig},
        )

        assert result.event_type == WebhookEventType.PAYMENT_REFUNDED

    def test_verify_webhook_invalid_signature(self, gateway):
        payload = b'{"event_type": "transaction.completed"}'

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": "ts=123;h1=invalid_hex"},
        )

        assert result.event_type == WebhookEventType.UNKNOWN

    def test_verify_webhook_missing_signature(self, gateway):
        result = gateway.verify_webhook(
            payload=b"test",
            headers={},
        )

        assert result.event_type == WebhookEventType.UNKNOWN

    def test_verify_webhook_unknown_event_type(self, gateway):
        payload_dict = {
            "event_type": "subscription.created",
            "data": {"id": "sub_01test"},
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        sig = _make_paddle_signature(payload, "whsec_test_paddle")

        result = gateway.verify_webhook(
            payload=payload,
            headers={"paddle-signature": sig},
        )

        assert result.event_type == WebhookEventType.UNKNOWN


class TestPaddleGatewayRefund:
    @pytest.mark.asyncio
    async def test_refund_full(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_adjustment = MagicMock()
        mock_adjustment.id = "adj_01fullrefund"
        mock_adjustment.status = "approved"

        mock_client_instance = MagicMock()
        mock_client_instance.adjustments.create.return_value = mock_adjustment
        mock_client_cls.return_value = mock_client_instance

        result = await gateway.refund(
            provider_tx_id="txn_01test123",
            reason="Customer requested",
        )

        assert result.success is True
        assert result.refund_id == "adj_01fullrefund"

    @pytest.mark.asyncio
    async def test_refund_partial(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_line_item = MagicMock()
        mock_line_item.id = "txnitm_01item1"

        mock_details = MagicMock()
        mock_details.line_items = [mock_line_item]

        mock_tx = MagicMock()
        mock_tx.details = mock_details

        mock_adjustment = MagicMock()
        mock_adjustment.id = "adj_01partialrefund"
        mock_adjustment.status = "pending_approval"

        mock_client_instance = MagicMock()
        mock_client_instance.transactions.get.return_value = mock_tx
        mock_client_instance.adjustments.create.return_value = mock_adjustment
        mock_client_cls.return_value = mock_client_instance

        result = await gateway.refund(
            provider_tx_id="txn_01test123",
            amount_cents=500,
            reason="Partial refund",
        )

        assert result.success is True
        assert result.refund_id == "adj_01partialrefund"

    @pytest.mark.asyncio
    async def test_refund_error(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock
        mock_client_cls.side_effect = Exception("Refund failed")

        result = await gateway.refund(provider_tx_id="txn_01test123")

        assert result.success is False
        assert "Refund failed" in result.error_message


class TestPaddleGatewayCancelPayment:
    @pytest.mark.asyncio
    async def test_cancel_payment_updates_transaction_status(
        self, gateway, paddle_sdk_mock
    ):
        _, mock_client_cls = paddle_sdk_mock
        sys.modules[
            "paddle_billing.Entities.Shared"
        ].TransactionStatus = SimpleNamespace(Canceled="canceled")
        sys.modules[
            "paddle_billing.Resources.Transactions.Operations"
        ].UpdateTransaction.side_effect = lambda **kwargs: SimpleNamespace(**kwargs)
        mock_client_instance = MagicMock()
        mock_client_cls.return_value = mock_client_instance

        result = await gateway.cancel_payment("txn_01test123")

        assert result.success is True
        assert result.status == "cancelled"
        mock_client_instance.transactions.update.assert_called_once()
        args = mock_client_instance.transactions.update.call_args.args
        assert args[0] == "txn_01test123"
        assert args[1].status == "canceled"

    @pytest.mark.asyncio
    async def test_cancel_payment_fails_when_sdk_import_is_unavailable(
        self, gateway, paddle_sdk_mock
    ):
        del sys.modules["paddle_billing.Entities.Shared"]

        result = await gateway.cancel_payment("txn_01test123")

        assert result.success is False
        assert result.error_message == "paddle-python-sdk is not installed"


class TestPaddleGatewayGetPaymentStatus:
    @pytest.mark.asyncio
    async def test_get_payment_status_success(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_tx = MagicMock()
        mock_tx.id = "txn_01test123"
        mock_tx.status = "completed"
        mock_tx.currency_code = "USD"
        mock_tx.details = MagicMock()
        mock_tx.details.totals = MagicMock()
        mock_tx.details.totals.total = "1000"
        mock_tx.checkout = MagicMock()
        mock_tx.checkout.url = "https://checkout.paddle.com/test"

        mock_client_instance = MagicMock()
        mock_client_instance.transactions.get.return_value = mock_tx
        mock_client_cls.return_value = mock_client_instance

        result = await gateway.get_payment_status("txn_01test123")

        assert result.success is True
        assert result.status == "completed"
        assert result.amount_cents == 1000
        assert result.currency == "USD"

    @pytest.mark.asyncio
    async def test_get_payment_status_error(self, gateway, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock
        mock_client_cls.side_effect = Exception("Not found")

        result = await gateway.get_payment_status("txn_nonexistent")

        assert result.success is False
        assert "Not found" in result.error_message


class TestPaddleGatewayEnvironment:
    """测试 API Key 环境选择（test_ 开头用 Sandbox，其他用 Production）"""

    @pytest.mark.asyncio
    async def test_sandbox_key(self, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_tx = MagicMock()
        mock_tx.id = "txn_test"
        mock_tx.status = "ready"
        mock_tx.checkout = MagicMock()
        mock_tx.checkout.url = "https://sandbox-checkout.paddle.com/test"

        mock_client_instance = MagicMock()
        mock_client_instance.transactions.create.return_value = mock_tx
        mock_client_cls.return_value = mock_client_instance

        gw = PaddleGateway(api_key="test_abc123", webhook_secret="secret")
        result = await gw.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="USD",
            plan_name="Test",
            user_id=1,
            success_url="https://example.com/s",
            cancel_url="https://example.com/c",
        )

        assert result.success is True
        mock_client_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_production_key(self, paddle_sdk_mock):
        _mock_pkg, mock_client_cls = paddle_sdk_mock

        mock_tx = MagicMock()
        mock_tx.id = "txn_live"
        mock_tx.status = "ready"
        mock_tx.checkout = MagicMock()
        mock_tx.checkout.url = "https://checkout.paddle.com/test"

        mock_client_instance = MagicMock()
        mock_client_instance.transactions.create.return_value = mock_tx
        mock_client_cls.return_value = mock_client_instance

        gw = PaddleGateway(api_key="live_abc123", webhook_secret="secret")
        result = await gw.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="USD",
            plan_name="Test",
            user_id=1,
            success_url="https://example.com/s",
            cancel_url="https://example.com/c",
        )

        assert result.success is True
        mock_client_cls.assert_called_once()
