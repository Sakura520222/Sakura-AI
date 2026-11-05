"""Exercise installed Paddle SDK imports, serialization and response parsing offline."""

import json
from types import SimpleNamespace

import pytest
from paddle_billing.Resources.Transactions.TransactionsClient import TransactionsClient
from requests import Response, Session

from backend.services.payment.paddle_gateway import PaddleGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("amount_cents", [None, 500])
@pytest.mark.parametrize("status", ["approved", "pending_approval"])
async def test_refund_uses_installed_sdk_without_network(
    monkeypatch, amount_cents, status
):
    requests_sent = []
    transactions_read = []

    def get_transaction(_client, transaction_id):
        transactions_read.append(transaction_id)
        return SimpleNamespace(
            details=SimpleNamespace(line_items=[SimpleNamespace(id="txnitm_fixture")])
        )

    def send(_session, request, **_kwargs):
        assert request.method == "POST"
        assert request.url == "https://sandbox-api.paddle.com/adjustments"
        requests_sent.append(json.loads(request.body))
        response = Response()
        response.status_code = 201
        response.request = request
        response._content = json.dumps(
            {
                "data": {
                    "id": "adj_fixture",
                    "action": "refund",
                    "transaction_id": "txn_fixture",
                    "customer_id": "ctm_fixture",
                    "reason": "Customer requested",
                    "currency_code": "USD",
                    "status": status,
                    "items": [],
                    "totals": {
                        "subtotal": "500",
                        "tax": "0",
                        "total": "500",
                        "fee": "0",
                        "earnings": "500",
                        "currency_code": "USD",
                        "retained_fee": "0",
                    },
                    "created_at": "2026-11-05T00:00:00Z",
                    "tax_rates_used": [],
                    "type": "full" if amount_cents is None else "partial",
                }
            }
        ).encode()
        return response

    monkeypatch.setattr(TransactionsClient, "get", get_transaction)
    monkeypatch.setattr(Session, "send", send)
    gateway = PaddleGateway("test_sdk_fixture", "webhook_fixture")

    result = await gateway.refund("txn_fixture", amount_cents, "Customer requested")

    assert result.success, result.error_message
    assert result.refund_id == "adj_fixture"
    assert result.status == ("succeeded" if status == "approved" else "pending")
    assert result.amount_cents == (amount_cents or 0)
    expected = {
        "action": "refund",
        "reason": "Customer requested",
        "transaction_id": "txn_fixture",
        "type": "full" if amount_cents is None else "partial",
    }
    if amount_cents is not None:
        expected["items"] = [
            {"item_id": "txnitm_fixture", "type": "partial", "amount": "500"}
        ]
    assert requests_sent == [expected]
    assert transactions_read == ([] if amount_cents is None else ["txn_fixture"])
