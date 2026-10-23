"""Stripe must distinguish unavailable verification from verified ignored events."""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from backend.core.time_service import now_utc
from backend.services.payment.gateway_base import (
    PaymentWebhookConfigurationError,
    PaymentWebhookVerificationError,
    WebhookEventType,
)
from backend.services.payment.stripe_gateway import StripeGateway

SECRET = "whsec_local_review_fixture"


def signed_headers(payload: bytes, *, secret: str = SECRET, age: int = 0):
    timestamp = int(now_utc().timestamp()) - age
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return {"stripe-signature": f"t={timestamp},v1={digest}"}


@pytest.fixture
def payload():
    return json.dumps(
        {
            "id": "evt_review_local",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_review_local",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "metadata": {"order_no": "ORD_REVIEW_LOCAL"},
                    "amount_total": 1000,
                    "currency": "usd",
                }
            },
        }
    ).encode()


@pytest.mark.parametrize("secret", [None, "", " ", "\t\n"])
def test_missing_secret_is_configuration_error_before_constructing_event(
    secret, payload
):
    gateway = StripeGateway("sk_local_fixture", secret)
    with patch("stripe.Webhook.construct_event") as construct:
        with pytest.raises(PaymentWebhookConfigurationError):
            gateway.verify_webhook(payload, signed_headers(payload))
        construct.assert_not_called()


def test_repaired_configuration_accepts_same_signed_receipt(payload):
    gateway = StripeGateway("sk_local_fixture", "")
    headers = signed_headers(payload)
    with pytest.raises(PaymentWebhookConfigurationError):
        gateway.verify_webhook(payload, headers)

    gateway = StripeGateway("sk_local_fixture", SECRET)
    event = gateway.verify_webhook(payload, headers)
    assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
    assert event.event_id == "evt_review_local"
    assert event.order_no == "ORD_REVIEW_LOCAL"
    assert event.amount_cents == 1000


@pytest.mark.parametrize("headers", [{}, {"stripe-signature": "invalid"}])
def test_missing_or_malformed_signature_is_verification_error(headers, payload):
    with pytest.raises(PaymentWebhookVerificationError):
        StripeGateway("sk_local_fixture", SECRET).verify_webhook(payload, headers)


@pytest.mark.parametrize("age", [0, 3600])
def test_tampered_or_expired_signature_is_verification_error(age, payload):
    headers = signed_headers(
        payload, secret="incorrect" if age == 0 else SECRET, age=age
    )
    with pytest.raises(PaymentWebhookVerificationError):
        StripeGateway("sk_local_fixture", SECRET).verify_webhook(payload, headers)


@pytest.mark.parametrize("payload", [b"invalid json", b"\xff"])
def test_unparseable_payload_is_verification_error(payload):
    with pytest.raises(PaymentWebhookVerificationError):
        StripeGateway("sk_local_fixture", SECRET).verify_webhook(
            payload, signed_headers(payload)
        )


def test_verified_unsupported_event_remains_ignored(payload):
    event = json.loads(payload)
    event["type"] = "customer.created"
    payload = json.dumps(event).encode()
    result = StripeGateway("sk_local_fixture", SECRET).verify_webhook(
        payload, signed_headers(payload)
    )
    assert result.event_type == WebhookEventType.UNKNOWN


def test_unexpected_sdk_failure_is_not_acknowledged_as_ignored(payload):
    with patch(
        "stripe.Webhook.construct_event", side_effect=RuntimeError("SDK failed")
    ):
        with pytest.raises(RuntimeError, match="SDK failed"):
            StripeGateway("sk_local_fixture", SECRET).verify_webhook(
                payload, signed_headers(payload)
            )
