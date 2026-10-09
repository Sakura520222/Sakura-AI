"""StripeGateway 单元测试

覆盖：创建支付、Webhook 验证、退款、支付状态查询。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.services.payment.gateway_base import (
    PaymentWebhookVerificationError,
    WebhookEventType,
)
from backend.services.payment.stripe_gateway import StripeGateway


@pytest.fixture
def gateway():
    return StripeGateway(api_key="sk_test_123", webhook_secret="whsec_test")


@pytest.fixture
def mock_session_completed_event():
    """Mock a checkout.session.completed StripeObject (Stripe SDK v15+)"""
    event = MagicMock()
    event.type = "checkout.session.completed"
    event_data_obj = MagicMock()
    event_data_obj.id = "cs_test_123"
    event_data_obj.metadata = {"order_no": "ORD20240101000000ABCD1234", "user_id": "1"}
    event_data_obj.amount_total = 1000
    event_data_obj.currency = "cny"
    event_data_obj.payment_status = "paid"
    event.data.object = event_data_obj
    return event


@pytest.fixture
def mock_session_expired_event():
    event = MagicMock()
    event.type = "checkout.session.expired"
    event_data_obj = MagicMock()
    event_data_obj.id = "cs_test_expired"
    event_data_obj.metadata = {"order_no": "ORD20240101000000EXPI1234", "user_id": "1"}
    event_data_obj.amount_total = 1000
    event_data_obj.currency = "cny"
    event.data.object = event_data_obj
    return event


class TestStripeGatewayCreatePayment:
    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_create_payment_success(self, mock_stripe, gateway):
        mock_session = MagicMock()
        mock_session.id = "cs_test_123"
        mock_session.url = "https://checkout.stripe.com/test"
        mock_session.payment_status = "unpaid"
        mock_stripe.checkout.Session.create.return_value = mock_session

        result = await gateway.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="cny",
            plan_name="Test Plan",
            user_id=1,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is True
        assert result.provider_tx_id == "cs_test_123"
        assert result.checkout_url == "https://checkout.stripe.com/test"
        mock_stripe.checkout.Session.create.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_create_payment_stripe_error(self, mock_stripe, gateway):
        import stripe as real_stripe

        mock_stripe.checkout.Session.create.side_effect = real_stripe.error.StripeError(
            "API error"
        )
        mock_stripe.error = real_stripe.error

        result = await gateway.create_payment(
            order_no="ORD123",
            amount_cents=1000,
            currency="cny",
            plan_name="Test Plan",
            user_id=1,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is False
        assert "API error" in result.error_message


class TestStripeGatewayVerifyWebhook:
    @patch("backend.services.payment.stripe_gateway.stripe")
    def test_verify_webhook_completed(
        self, mock_stripe, gateway, mock_session_completed_event
    ):
        mock_stripe.Webhook.construct_event.return_value = mock_session_completed_event

        result = gateway.verify_webhook(
            payload=b"test",
            headers={"stripe-signature": "t=123,v1=abc"},
        )

        assert result.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert result.provider_tx_id == "cs_test_123"
        assert result.order_no == "ORD20240101000000ABCD1234"
        assert result.amount_cents == 1000

    @patch("backend.services.payment.stripe_gateway.stripe")
    def test_verify_webhook_expired(
        self, mock_stripe, gateway, mock_session_expired_event
    ):
        mock_stripe.Webhook.construct_event.return_value = mock_session_expired_event

        result = gateway.verify_webhook(
            payload=b"test",
            headers={"stripe-signature": "t=123,v1=abc"},
        )

        assert result.event_type == WebhookEventType.PAYMENT_EXPIRED

    @patch("backend.services.payment.stripe_gateway.stripe")
    def test_verify_webhook_invalid_signature(self, mock_stripe, gateway):
        import stripe as real_stripe

        mock_stripe.Webhook.construct_event.side_effect = (
            real_stripe.error.SignatureVerificationError(
                "Invalid signature", "sig_header"
            )
        )
        mock_stripe.error = real_stripe.error

        with pytest.raises(PaymentWebhookVerificationError):
            gateway.verify_webhook(
                payload=b"test",
                headers={"stripe-signature": "invalid"},
            )

    @patch("backend.services.payment.stripe_gateway.stripe")
    def test_verify_webhook_unknown_event_type(self, mock_stripe, gateway):
        event = MagicMock()
        event.type = "customer.created"
        event.data.object = MagicMock()
        mock_stripe.Webhook.construct_event.return_value = event

        result = gateway.verify_webhook(
            payload=b"test",
            headers={"stripe-signature": "t=123,v1=abc"},
        )

        assert result.event_type == WebhookEventType.UNKNOWN


class TestStripeGatewayRefund:
    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_refund_success(self, mock_stripe, gateway):
        mock_session = MagicMock()
        mock_session.id = "cs_test_123"
        mock_session.payment_intent = "pi_test_123"
        mock_session.currency = "cny"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        mock_refund = MagicMock()
        mock_refund.id = "re_test_123"
        mock_refund.amount = 1000
        mock_refund.status = "succeeded"
        mock_stripe.Refund.create.return_value = mock_refund

        result = await gateway.refund(provider_tx_id="cs_test_123")

        assert result.success is True
        assert result.refund_id == "re_test_123"
        assert result.amount_cents == 1000
        mock_stripe.Refund.create.assert_called_once_with(
            api_key="sk_test_123",
            payment_intent="pi_test_123",
        )

    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_refund_partial(self, mock_stripe, gateway):
        mock_session = MagicMock()
        mock_session.id = "cs_test_123"
        mock_session.payment_intent = "pi_test_123"
        mock_session.currency = "cny"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        mock_refund = MagicMock()
        mock_refund.id = "re_test_partial"
        mock_refund.amount = 500
        mock_refund.status = "succeeded"
        mock_stripe.Refund.create.return_value = mock_refund

        result = await gateway.refund(
            provider_tx_id="cs_test_123", amount_cents=500, reason="partial"
        )

        assert result.success is True
        assert result.amount_cents == 500
        mock_stripe.Refund.create.assert_called_once_with(
            api_key="sk_test_123",
            payment_intent="pi_test_123",
            amount=500,
            reason="requested_by_customer",
        )

    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_refund_stripe_error(self, mock_stripe, gateway):
        import stripe as real_stripe

        mock_stripe.checkout.Session.retrieve.side_effect = (
            real_stripe.error.StripeError("Refund failed")
        )
        mock_stripe.error = real_stripe.error

        result = await gateway.refund(provider_tx_id="cs_test_123")

        assert result.success is False
        assert "Refund failed" in result.error_message


class TestStripeGatewayGetPaymentStatus:
    @pytest.mark.asyncio
    @patch("backend.services.payment.stripe_gateway.stripe")
    async def test_get_payment_status_success(self, mock_stripe, gateway):
        mock_session = MagicMock()
        mock_session.id = "cs_test_123"
        mock_session.payment_status = "paid"
        mock_session.payment_intent = "pi_test_123"
        mock_session.currency = "cny"
        mock_session.amount_total = 1000
        mock_session.currency = "cny"
        mock_stripe.checkout.Session.retrieve.return_value = mock_session

        result = await gateway.get_payment_status("cs_test_123")

        assert result.success is True
        assert result.status == "paid"
        assert result.amount_cents == 1000
        assert result.currency == "cny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("currency", "canonical", "wire"),
    (("ISK", 5, 500), ("UGX", 5, 500), ("MGA", 500, 5)),
)
@patch("backend.services.payment.stripe_gateway.stripe")
async def test_stripe_protocol_amounts_are_normalized_at_boundary(
    mock_stripe, gateway, currency, canonical, wire
):
    from types import SimpleNamespace

    mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
        id="cs_units", url="https://example.test/checkout", payment_status="unpaid"
    )
    created = await gateway.create_payment(
        order_no="ORD_UNITS",
        amount_cents=canonical,
        currency=currency,
        plan_name="Exact test money",
        user_id=1,
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
    )
    assert created.success
    assert (
        mock_stripe.checkout.Session.create.call_args.kwargs["line_items"][0][
            "price_data"
        ]["unit_amount"]
        == wire
    )
    session = SimpleNamespace(
        id="cs_units",
        metadata={"order_no": "ORD_UNITS"},
        amount_total=wire,
        currency=currency.lower(),
        payment_status="paid",
        payment_intent="pi_units",
    )
    mock_stripe.Webhook.construct_event.return_value = SimpleNamespace(
        type="checkout.session.completed", data=SimpleNamespace(object=session)
    )
    event = gateway.verify_webhook(b"signed", {"stripe-signature": "fixture"})
    assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
    assert event.amount_cents == canonical
    mock_stripe.checkout.Session.retrieve.return_value = session
    mock_stripe.Refund.create.return_value = SimpleNamespace(
        id="re_units", amount=wire, status="succeeded"
    )
    refunded = await gateway.refund(
        "cs_units", amount_cents=canonical, idempotency_key="refund-units"
    )
    assert refunded.success
    assert refunded.amount_cents == canonical
    assert mock_stripe.Refund.create.call_args.kwargs["amount"] == wire
    assert (await gateway.get_payment_status("cs_units")).amount_cents == canonical


@patch("backend.services.payment.stripe_gateway.stripe")
def test_stripe_completed_unpaid_session_does_not_fulfill(mock_stripe, gateway):
    from types import SimpleNamespace

    mock_stripe.Webhook.construct_event.return_value = SimpleNamespace(
        type="checkout.session.completed",
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="cs_deferred",
                metadata={"order_no": "ORD_DEFERRED"},
                amount_total=1000,
                currency="cny",
                payment_status="unpaid",
            )
        ),
    )
    assert (
        gateway.verify_webhook(b"signed", {"stripe-signature": "fixture"}).event_type
        == WebhookEventType.UNKNOWN
    )
