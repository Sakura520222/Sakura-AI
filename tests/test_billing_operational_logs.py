"""Billing diagnostics use real sinks, retain correlation and protect opaque keys."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import quote

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from backend.core.log_context import get_log_context, log_context
from backend.models.payment_models import PaymentLog
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingService
from backend.services.payment.alipay_gateway import AlipayGateway
from backend.services.payment.nowpayments_gateway import NowPaymentsGateway
from backend.services.payment.paddle_gateway import PaddleGateway
from backend.services.payment.stripe_gateway import StripeGateway
from backend.services.payment_service import PaymentService
from tests.test_billing_entitlements import db as real_database_fixture
from tests.test_logging_security import secured_logs as real_log_fixture
from tests.test_paddle_gateway import paddle_sdk_mock as mock_checkout_sdk_fixture
from tests.test_review_redeem_concurrency import voucher

db = real_database_fixture
secured_logs = real_log_fixture
paddle_sdk_mock = mock_checkout_sdk_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["stripe", "paddle", "alipay", "nowpayments"])
async def test_gateway_exception_and_copied_result_keep_traceback_without_opaque_secrets(
    secured_logs, monkeypatch, provider
):
    console, path, records = secured_logs
    secret = f"opaque/{provider}@credential+value"
    message = f"HTTP 503 upstream rejected {secret} encoded={quote(secret, safe='')}"
    error = RuntimeError(message)
    error.add_note(f"retry credential {secret}")

    def fault(*args, **kwargs):
        logger.debug("SDK diagnostic HTTP 503 {}", secret)
        raise error

    if provider == "stripe":
        import stripe

        gateway = StripeGateway(secret, "test-hook-secret")
        monkeypatch.setattr(stripe.checkout.Session, "retrieve", fault)
    elif provider == "paddle":
        import paddle_billing

        gateway = PaddleGateway(secret, "test-hook-secret")
        client = Mock()
        client.transactions.get.side_effect = fault
        monkeypatch.setattr(paddle_billing, "Client", Mock(return_value=client))
    elif provider == "alipay":
        gateway = AlipayGateway("test-app-id", secret)
        monkeypatch.setattr(gateway, "_sign_with_rsa2", fault)
    else:
        gateway = NowPaymentsGateway(secret, "test-ipn-secret")
        monkeypatch.setattr(gateway, "_get", AsyncMock(side_effect=error))

    with log_context(
        request_id="request-billing", task_id="task-billing", attempt_id=81
    ):
        with bind_billing_context(BillingContext(7, "operation-billing", "pr_review")):
            result = await gateway.get_payment_status("native-payment")
        logger.warning("Copied gateway failure: {}", result.error_message)
        logger.info("Caller scope restored")
        assert get_log_context() == {
            "request_id": "request-billing",
            "task_id": "task-billing",
            "attempt_id": 81,
        }
    assert result.success is False
    assert "HTTP 503" in result.error_message
    assert secret not in result.error_message
    assert quote(secret, safe="") not in result.error_message
    assert error.args == (message,)
    for rendered in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert secret not in rendered
        assert quote(secret, safe="") not in rendered
        assert "RuntimeError" in rendered
        assert "get_payment_status" in rendered
        assert "HTTP 503" in rendered
    failures = [
        record
        for record in records
        if record["extra"].get("exception") and record["extra"].get("payment_gateway")
    ]
    assert failures
    for record in failures:
        assert record["extra"]["request_id"] == "request-billing"
        assert record["extra"]["task_id"] == "task-billing"
        assert record["extra"]["attempt_id"] == 81
        assert record["extra"]["billing_operation_id"] == "operation-billing"
        assert record["extra"]["payment_action"] == "get_payment_status"
    restored = next(
        record for record in records if record["message"] == "Caller scope restored"
    )
    assert "payment_gateway" not in restored["extra"]
    assert "billing_operation_id" not in restored["extra"]


@pytest.mark.asyncio
async def test_webhook_relogs_escaping_gateway_error_safely(secured_logs, monkeypatch):
    import stripe

    from backend.api.webhook import handle_stripe_webhook

    console, path, records = secured_logs
    secret = "opaque-webhook-provider-credential"
    signature_header = "opaque-provider-signature"
    gateway = StripeGateway(secret, "test-webhook-secret")
    error = RuntimeError(f"native SDK failed using {secret}, header {signature_header}")
    monkeypatch.setattr(stripe.Webhook, "construct_event", Mock(side_effect=error))
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/stripe",
            "headers": [(b"stripe-signature", signature_header.encode())],
        },
        receive,
    )
    with log_context(request_id="request-webhook"):
        response = await handle_stripe_webhook(request)
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "retry_required"}
    for rendered in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert secret not in rendered
        assert signature_header not in rendered
        assert "_handle_payment_webhook" in rendered
        assert "RuntimeError" in rendered
    failure = next(
        record
        for record in records
        if record["message"].startswith("Payment receipt failed")
    )
    assert "RuntimeError" in failure["extra"]["exception"]
    assert failure["extra"]["request_id"] == "request-webhook"


@pytest.mark.asyncio
async def test_redeem_logs_and_persisted_payment_details_do_not_copy_voucher(
    db, secured_logs
):
    console, path, records = secured_logs
    user, _, code = await voucher(db)
    service = PaymentService(db)
    with log_context(request_id="request-redeem"):
        winner = await service.redeem_code(user.id, code.code)
        await db.commit()
        assert (await service.redeem_code(user.id, code.code)).id == winner.id
    for rendered in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert code.code not in rendered
    details = (await db.execute(select(PaymentLog.detail))).scalars().all()
    assert details and all(code.code not in (detail or "") for detail in details)
    transitions = [
        record
        for record in records
        if record["message"]
        in {
            "Redeem order fulfilled",
            "Redeem request replayed existing order",
        }
    ]
    assert len(transitions) == 2
    assert all(record["extra"]["order_id"] == winner.id for record in transitions)
    assert all(
        record["extra"]["request_id"] == "request-redeem" for record in transitions
    )


@pytest.mark.asyncio
async def test_billing_scopes_are_isolated_across_async_lanes(secured_logs):
    _, _, records = secured_logs
    barrier = asyncio.Barrier(2)

    async def lane(number):
        with log_context(request_id=f"request-{number}"):
            with bind_billing_context(
                BillingContext(number, f"operation-{number}", "pr_review")
            ):
                await barrier.wait()
                logger.info("Concurrent billing lane")
                await asyncio.to_thread(logger.info, "Billing SDK thread")
            logger.info("Billing lane restored")

    await asyncio.gather(lane(1), lane(2))
    for record in records:
        if record["message"] in {"Concurrent billing lane", "Billing SDK thread"}:
            number = record["extra"]["payer_user_id"]
            assert record["extra"]["request_id"] == f"request-{number}"
            assert record["extra"]["billing_operation_id"] == f"operation-{number}"
        elif record["message"] == "Billing lane restored":
            assert "billing_operation_id" not in record["extra"]


@pytest.mark.asyncio
async def test_admission_and_settlement_logs_report_staged_financial_state(
    db, secured_logs
):
    _, _, records = secured_logs
    service = BillingService(db)
    with log_context(request_id="request-admission"):
        await service.register_operation(
            None, "log-operation", "pr_review", platform_reason="test"
        )
        await service.finish_operation("log-operation", "completed")
    states = [
        record
        for record in records
        if record["message"]
        in {
            "Billing admission staged",
            "Billing settlement staged",
        }
    ]
    assert len(states) == 2
    assert all(
        record["extra"]["billing_operation_id"] == "log-operation" for record in states
    )
    assert all(
        record["extra"]["request_id"] == "request-admission" for record in states
    )
    assert states[-1]["extra"]["status"] == "settled"
    assert states[-1]["extra"]["settled_units"] == 0


@pytest.mark.asyncio
async def test_checkout_log_redacts_before_truncating_url(
    secured_logs, paddle_sdk_mock
):
    console, path, _ = secured_logs
    _, client_class = paddle_sdk_mock
    secret = "opaque-checkout-path-credential-long"
    url = "https://checkout.paddle.com/" + "x" * 43 + "/" + secret
    transaction = MagicMock()
    transaction.id = "txn_safe"
    transaction.status = "ready"
    transaction.checkout.url = url
    client_class.return_value.transactions.create.return_value = transaction
    result = await PaddleGateway(secret, "test-webhook-secret").create_payment(
        order_no="ORD_SAFE",
        amount_cents=1000,
        currency="USD",
        plan_name="Safe plan",
        user_id=1,
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
    )
    assert result.success is True
    assert secret in result.checkout_url
    for rendered in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert secret[:8] not in rendered
        assert "checkout.paddle.com" in rendered


@pytest.mark.asyncio
async def test_escaping_redeem_integrity_error_remains_safe_after_scope_exit(
    db, secured_logs, monkeypatch
):
    console, path, _ = secured_logs
    user, plan, code = await voucher(db)
    service = PaymentService(db)
    existing = await service.grant_plan_to_user(user.id, plan.id, operator_id=user.id)
    await db.commit()
    monkeypatch.setattr(service, "_generate_order_no", lambda: existing.order_no)
    with pytest.raises(IntegrityError) as caught:
        await service.redeem_code(user.id, code.code)
    logger.opt(exception=caught.value).warning("HTTP redemption failure")
    for rendered in (console.getvalue(), path.read_text(encoding="utf-8")):
        assert code.code not in rendered
        assert "IntegrityError" in rendered
        assert "UNIQUE constraint failed" in rendered
