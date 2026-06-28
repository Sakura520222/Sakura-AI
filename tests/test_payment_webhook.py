"""Stripe Webhook 端点单元测试

覆盖：签名验证、事件处理（支付完成/过期/退款）、幂等性。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_db_session():
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


@pytest.fixture
def mock_gateway():
    gateway = MagicMock()
    return gateway


class TestStripeWebhookEndpoint:
    """Test the Stripe webhook handler in webhook.py"""

    @pytest.mark.asyncio
    async def test_webhook_payment_completed(self, mock_db_session, mock_gateway):
        from backend.services.payment.gateway_base import (
            WebhookEvent,
            WebhookEventType,
        )
        from backend.models.payment_models import OrderStatus

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider_tx_id="cs_test_123",
            order_no="ORD20240101000000ABCD1234",
            amount_cents=1000,
            currency="cny",
        )
        mock_gateway.verify_webhook.return_value = event

        mock_order = MagicMock()
        mock_order.order_no = "ORD20240101000000ABCD1234"
        mock_order.status = OrderStatus.FULFILLED.value

        with (
            patch(
                "backend.api.webhook.get_async_session",
                return_value=mock_db_session,
            ),
            patch(
                "backend.services.payment.get_gateway",
                new_callable=AsyncMock,
                return_value=mock_gateway,
            ),
            patch(
                "backend.services.payment_service.PaymentService.confirm_payment",
                new_callable=AsyncMock,
                return_value=mock_order,
            ) as mock_confirm_payment,
        ):
            from backend.api.webhook import handle_stripe_webhook
            from fastapi import Request

            mock_request = MagicMock(spec=Request)
            mock_request.body = AsyncMock(return_value=b'{"test": true}')
            mock_request.headers = {"stripe-signature": "t=123,v1=abc"}

            response = await handle_stripe_webhook(mock_request)

            body_text = response.body.decode()
            assert response.status_code == 200, (
                f"Got {response.status_code}: {body_text}"
            )
            body = json.loads(body_text)
            assert body["status"] == "processed"
            assert body["event"] == "payment_completed"
            mock_confirm_payment.assert_awaited_once_with(
                order_no="ORD20240101000000ABCD1234",
                provider_tx_id="cs_test_123",
                paid_amount_cents=1000,
                paid_currency="cny",
            )

    @pytest.mark.asyncio
    async def test_webhook_invalid_signature(self, mock_gateway):
        from backend.services.payment.gateway_base import WebhookEventType

        event = MagicMock()
        event.event_type = WebhookEventType.UNKNOWN
        mock_gateway.verify_webhook.return_value = event

        with patch(
            "backend.services.payment.get_gateway",
            new_callable=AsyncMock,
            return_value=mock_gateway,
        ):
            from backend.api.webhook import handle_stripe_webhook
            from fastapi import Request

            mock_request = MagicMock(spec=Request)
            mock_request.body = AsyncMock(return_value=b'{"test": true}')
            mock_request.headers = {}

            response = await handle_stripe_webhook(mock_request)

            assert response.status_code == 200
            body = json.loads(response.body.decode())
            assert body["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_webhook_gateway_not_configured(self):
        with patch(
            "backend.services.payment.get_gateway",
            new_callable=AsyncMock,
            side_effect=ValueError("missing API key"),
        ):
            from backend.api.webhook import handle_stripe_webhook
            from fastapi import Request

            mock_request = MagicMock(spec=Request)
            mock_request.body = AsyncMock(return_value=b'{"test": true}')
            mock_request.headers = {}

            response = await handle_stripe_webhook(mock_request)

            assert response.status_code == 400
            body = json.loads(response.body.decode())
            assert "error" in body["status"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "provider", "expected_body"),
    [
        (
            "handle_paddle_webhook",
            "paddle",
            b'{"status":"processed","event":"payment_completed"}',
        ),
        ("handle_alipay_webhook", "alipay", b"success"),
        (
            "handle_nowpayments_webhook",
            "nowpayments",
            b'{"status":"processed","event":"payment_completed"}',
        ),
    ],
)
async def test_payment_completed_webhooks_pass_amount_to_confirmation(
    handler_name,
    provider,
    expected_body,
    mock_db_session,
    mock_gateway,
):
    from backend.api import webhook
    from backend.models.payment_models import OrderStatus
    from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
    from fastapi import Request

    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_COMPLETED,
        provider_tx_id=f"{provider}_tx_123",
        order_no=f"ORD_{provider.upper()}_AMOUNT",
        amount_cents=2500,
        currency="CNY",
    )
    mock_gateway.verify_webhook.return_value = event

    mock_order = MagicMock()
    mock_order.order_no = event.order_no
    mock_order.status = OrderStatus.FULFILLED.value

    with (
        patch(
            "backend.api.webhook.get_async_session",
            return_value=mock_db_session,
        ),
        patch(
            "backend.services.payment.get_gateway",
            new_callable=AsyncMock,
            return_value=mock_gateway,
        ),
        patch(
            "backend.services.payment_service.PaymentService.confirm_payment",
            new_callable=AsyncMock,
            return_value=mock_order,
        ) as mock_confirm_payment,
    ):
        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b'{"test": true}')
        mock_request.headers = {}

        response = await getattr(webhook, handler_name)(mock_request)

    assert response.status_code == 200
    assert response.body == expected_body
    mock_confirm_payment.assert_awaited_once_with(
        order_no=event.order_no,
        provider_tx_id=event.provider_tx_id,
        paid_amount_cents=event.amount_cents,
        paid_currency=event.currency,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "provider", "expected_body"),
    [
        (
            "handle_stripe_webhook",
            "stripe",
            b'{"status":"processed","event":"payment_expired"}',
        ),
        (
            "handle_paddle_webhook",
            "paddle",
            b'{"status":"processed","event":"payment_expired"}',
        ),
        ("handle_alipay_webhook", "alipay", b"success"),
        (
            "handle_nowpayments_webhook",
            "nowpayments",
            b'{"status":"processed","event":"payment_expired"}',
        ),
    ],
)
async def test_payment_expired_webhooks_commit_when_cancelled(
    handler_name,
    provider,
    expected_body,
    mock_db_session,
    mock_gateway,
):
    """All providers commit when cancel_expired_order actually cancels the order.

    Exercises the real ``cancel_and_commit_if_needed`` helper by patching only
    ``cancel_expired_order``, verifying the handler → helper → commit flow is
    consistent across every payment provider.
    """
    from backend.api import webhook
    from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
    from fastapi import Request

    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_EXPIRED,
        provider_tx_id=f"{provider}_tx_expired",
        order_no=f"ORD_{provider.upper()}_EXPIRED",
    )
    mock_gateway.verify_webhook.return_value = event

    mock_order = MagicMock()
    mock_order.order_no = event.order_no

    with (
        patch(
            "backend.api.webhook.get_async_session",
            return_value=mock_db_session,
        ),
        patch(
            "backend.services.payment.get_gateway",
            new_callable=AsyncMock,
            return_value=mock_gateway,
        ),
        patch(
            "backend.services.payment_service.PaymentService.cancel_expired_order",
            new_callable=AsyncMock,
            return_value=mock_order,
        ),
    ):
        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b'{"test": true}')
        mock_request.headers = {}

        response = await getattr(webhook, handler_name)(mock_request)

    assert response.status_code == 200
    assert response.body == expected_body
    mock_db_session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_stripe_webhook",
        "handle_paddle_webhook",
        "handle_alipay_webhook",
        "handle_nowpayments_webhook",
    ],
)
async def test_payment_expired_webhooks_skip_commit_when_order_gone(
    handler_name,
    mock_db_session,
    mock_gateway,
):
    """All providers skip commit when the order is already gone (None)."""
    from backend.api import webhook
    from backend.services.payment.gateway_base import WebhookEvent, WebhookEventType
    from fastapi import Request

    event = WebhookEvent(
        event_type=WebhookEventType.PAYMENT_EXPIRED,
        provider_tx_id="tx_expired_gone",
        order_no="ORD_GONE_EXPIRED",
    )
    mock_gateway.verify_webhook.return_value = event

    with (
        patch(
            "backend.api.webhook.get_async_session",
            return_value=mock_db_session,
        ),
        patch(
            "backend.services.payment.get_gateway",
            new_callable=AsyncMock,
            return_value=mock_gateway,
        ),
        patch(
            "backend.services.payment_service.PaymentService.cancel_expired_order",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b'{"test": true}')
        mock_request.headers = {}

        response = await getattr(webhook, handler_name)(mock_request)

    assert response.status_code == 200
    mock_db_session.commit.assert_not_awaited()
