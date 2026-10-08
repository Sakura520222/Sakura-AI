"""Billing projections exercise real persistence and existing authorization.

External identity resolution is simulated at its boundary. No financial service
or database result is mocked, and price samples are test-only configuration.
"""

from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.api.v1 import deps as api_deps
from backend.api.v1.billing import PlanCreateRequest, WalletThresholdRequest, router
from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import (
    BillingOperation,
    BillingTransaction,
    BillingWallet,
)
from backend.models.database import Base, PRReview
from backend.models.telegram_models import TelegramUser
from backend.services.billing_view_service import parse_pricing_json
from backend.webui import deps as web_deps
from backend.webui.deps import get_db, get_user_preferences, require_payment_enabled
from backend.webui.routes.billing import router as web_router
from tests.test_billing_wallet import SQLSession

TEST_PRICE = {
    "currency": "USD",
    "settlement_currency": "USD",
    "fx_rate": "1",
    "markup": "1.5",
    "credits_per_currency_unit": "10",
    "unit": "tokens",
    "input_price": "2",
    "output_price": "3",
}


class APISQLSession(SQLSession):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def delete(self, record):
        self.session.delete(record)

    def add_all(self, records):
        self.session.add_all(records)


@pytest_asyncio.fixture
async def billing_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'billing.db'}")
    Base.metadata.create_all(engine)

    def sessions():
        return APISQLSession(Session(engine, expire_on_commit=False))

    async with sessions() as db:
        db.add_all(
            [
                TelegramUser(id=1, github_username="alice", role="user"),
                TelegramUser(id=2, github_username="bob", role="user"),
                TelegramUser(id=3, github_username="operator", role="super_admin"),
                BillingWallet(
                    user_id=1, balance_units=9_500_000, reserved_units=1_000_000
                ),
                BillingWallet(user_id=2, balance_units=80_000_000, reserved_units=0),
                PRReview(
                    id=10,
                    pr_id=100,
                    pr_number=5,
                    repo_owner="alice",
                    repo_name="project",
                    author="bob",
                    strategy="test",
                ),
                PRReview(
                    id=11,
                    pr_id=101,
                    pr_number=7,
                    repo_owner="secret",
                    repo_name="private",
                    author="secret",
                    strategy="test",
                ),
                BillingOperation(
                    operation_id="alice-review",
                    user_id=1,
                    feature="pr_review",
                    source={"review_id": 10},
                    status="settled",
                ),
                BillingOperation(
                    operation_id="hidden-source",
                    user_id=1,
                    feature="agent",
                    source={"review_id": 11, "prompt": "SECRET_PROMPT"},
                    status="pending_pricing",
                ),
                BillingOperation(
                    operation_id="bob-review",
                    user_id=2,
                    feature="pr_review",
                    source={"review_id": 10},
                    status="settled",
                ),
                BillingTransaction(
                    id=1,
                    user_id=1,
                    operation_id="alice-review",
                    kind="consumption",
                    delta_units=-500_000,
                    idempotency_key="t1",
                    snapshot={"secret": "SECRET_PRICE"},
                ),
                BillingTransaction(
                    id=2,
                    user_id=1,
                    operation_id="hidden-source",
                    kind="consumption",
                    delta_units=-10,
                    idempotency_key="t2",
                    snapshot={"secret": "SECRET_PRICE"},
                ),
                BillingTransaction(
                    id=3,
                    user_id=2,
                    operation_id="bob-review",
                    kind="consumption",
                    delta_units=-5_000_000,
                    idempotency_key="t3",
                    snapshot={},
                ),
            ]
        )
        await db.commit()

    async def current_user(request: Request):
        identity = request.headers.get("x-test-identity", "alice")
        identity_map = {
            "alice": {"user_id": 1, "sub": "alice", "role": "user"},
            "bob": {"user_id": 2, "sub": "bob", "role": "user"},
            "operator": {"user_id": 3, "sub": "operator", "role": "super_admin"},
        }
        return identity_map[identity]

    async def no_mfa(*args, **kwargs):
        return False

    async def enabled():
        return None

    async def db_dependency():
        async with sessions() as db:
            yield db

    monkeypatch.setattr(web_deps, "get_current_user", current_user)
    monkeypatch.setattr(web_deps, "user_requires_mfa_enrollment", no_mfa)
    monkeypatch.setattr(api_deps, "get_api_current_user", current_user)
    monkeypatch.setattr(api_deps, "user_requires_mfa_enrollment", no_mfa)
    monkeypatch.setattr(api_deps.db_module, "async_session", sessions)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(web_router)
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_payment_enabled] = enabled
    app.dependency_overrides[get_user_preferences] = lambda: {
        "items_per_page": 1,
        "language": "en",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_wallet_and_paged_ledger_are_own_only_and_sources_authorized(
    billing_client,
):
    client, _ = billing_client
    response = await client.get("/api/v1/billing/wallet?user_id=2")
    assert response.status_code == 200
    assert response.json()["available"] == "8.5"
    response = await client.get("/api/v1/billing/transactions?limit=1")
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["operation_id"] == "hidden-source"
    assert response.json()["items"][0]["source"] == {}
    assert "SECRET" not in response.text
    assert "secret/private" not in response.text
    response = await client.get(
        "/api/v1/billing/transactions?offset=1&limit=1&feature=pr_review"
    )
    assert response.json()["items"] == []
    response = await client.get(
        "/api/v1/billing/transactions?feature=pr_review&kind=consumption"
    )
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["source"] == {
        "review_id": 10,
        "repo": "alice/project",
        "url": "/pr/10",
        "pr_number": 5,
    }
    assert response.json()["items"][0]["credits"] == "-0.5"
    assert (
        await client.get("/api/v1/billing/transactions?limit=101")
    ).status_code == 422


@pytest.mark.asyncio
async def test_operations_filter_and_owner_do_not_leak(billing_client):
    client, _ = billing_client
    response = await client.get(
        "/api/v1/billing/operations?status=pending_pricing&user_id=2"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["operation_id"] == "hidden-source"
    assert response.json()["items"][0]["source"] == {}
    assert "prompt" not in response.text


@pytest.mark.asyncio
async def test_threshold_is_persisted_without_owner_spoofing(billing_client):
    client, sessions = billing_client
    response = await client.put(
        "/api/v1/billing/wallet/threshold", json={"credits": "10", "user_id": 2}
    )
    assert response.status_code == 200
    assert response.json()["low_balance"] is True
    async with sessions() as db:
        assert (
            await db.get(BillingWallet, 1)
        ).low_balance_threshold_units == 10_000_000
        assert (await db.get(BillingWallet, 2)).low_balance_threshold_units == 0
    assert (
        await client.put("/api/v1/billing/wallet/threshold", json={"credits": 0.1})
    ).status_code == 422


@pytest.mark.asyncio
async def test_only_super_admin_can_publish_price_or_adjust_wallet(billing_client):
    client, sessions = billing_client
    profile = {
        "provider_id": "test-provider",
        "model_id": "test-model",
        "call_kind": "chat",
        "config": TEST_PRICE,
    }
    assert (
        await client.post("/api/v1/billing/admin/pricing", json=profile)
    ).status_code == 403
    assert (await client.get("/api/v1/billing/admin/pricing")).status_code == 403
    assert (
        await client.post(
            "/api/v1/billing/admin/wallets/2/adjust",
            json={"credits": "1", "idempotency_key": "admin-test", "reason": "test"},
        )
    ).status_code == 403
    response = await client.post(
        "/api/v1/billing/admin/pricing",
        json=profile,
        headers={"x-test-identity": "operator"},
    )
    assert response.status_code == 200
    response = await client.get(
        "/api/v1/billing/admin/pricing", headers={"x-test-identity": "operator"}
    )
    assert response.json()["items"][0]["config"]["input_price"] == "2"
    async with sessions() as db:
        logs = (
            (
                await db.execute(
                    select(AdminActionLog).where(
                        AdminActionLog.action == "billing_publish_price"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1 and logs[0].admin_id == 3


@pytest.mark.asyncio
async def test_adjustment_is_real_idempotent_and_audited(billing_client):
    client, sessions = billing_client
    payload = {
        "credits": "3.000001",
        "idempotency_key": "test-manual-adjustment",
        "reason": "isolated test adjustment",
    }
    first = await client.post(
        "/api/v1/billing/admin/wallets/2/adjust",
        json=payload,
        headers={"x-test-identity": "operator"},
    )
    second = await client.post(
        "/api/v1/billing/admin/wallets/2/adjust",
        json=payload,
        headers={"x-test-identity": "operator"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["transaction_id"] == second.json()["transaction_id"]
    assert second.json()["wallet"]["balance"] == "83.000001"
    async with sessions() as db:
        rows = (
            (
                await db.execute(
                    select(BillingTransaction).where(
                        BillingTransaction.idempotency_key == payload["idempotency_key"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].actor_id == 3 and rows[0].reason == payload["reason"]


def test_exact_schema_and_price_json_reject_binary_floats():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WalletThresholdRequest(credits=0.1)
    with pytest.raises(ValidationError):
        PlanCreateRequest(name="test", plan_type="one_time", credit_grant=0.1)
    with pytest.raises(ValidationError):
        PlanCreateRequest(
            name="test", plan_type="one_time", rate_limits={"pr_daily": True}
        )
    with pytest.raises(ValueError):
        parse_pricing_json('{"input_price": 0.1}')
    assert PlanCreateRequest(
        name="test", plan_type="one_time", credit_grant="0.1"
    ).credit_grant == Decimal("0.1")


@pytest.mark.asyncio
async def test_webui_renders_real_ledger_and_submits_threshold(billing_client):
    import re

    client, sessions = billing_client
    response = await client.get("/billing/credits")
    assert response.status_code == 200
    assert 'data-testid="credit_available">8.5' in response.text
    assert "Awaiting pricing" in response.text
    assert "secret/private" not in response.text and "SECRET_PRICE" not in response.text
    assert "Next" in response.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    posted = await client.post(
        "/billing/credits/threshold", data={"credits": "12.000001", "csrf_token": token}
    )
    assert posted.status_code == 302
    async with sessions() as db:
        assert (
            await db.get(BillingWallet, 1)
        ).low_balance_threshold_units == 12_000_001
    response = await client.get("/billing/credits?feature=pr_review&kind=")
    assert "alice/project #5" in response.text
    assert "Credits are settled from actual AI usage" in response.text


@pytest.mark.asyncio
async def test_webui_price_page_authorization_and_validation(billing_client):
    import json
    import re

    client, sessions = billing_client
    assert (await client.get("/billing/admin/pricing")).status_code == 403
    headers = {"x-test-identity": "operator"}
    response = await client.get("/billing/admin/pricing", headers=headers)
    assert response.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    posted = await client.post(
        "/billing/admin/pricing",
        headers=headers,
        data={
            "provider_id": "test-provider",
            "model_id": "test-model",
            "call_kind": "chat",
            "config": json.dumps(TEST_PRICE),
            "csrf_token": token,
        },
    )
    assert posted.status_code == 302
    response = await client.get("/billing/admin/pricing", headers=headers)
    assert "test-model" in response.text and "test-provider" in response.text
    posted = await client.post(
        "/billing/admin/pricing",
        headers=headers,
        data={
            "provider_id": "test-provider",
            "model_id": "test-model",
            "call_kind": "chat",
            "config": '{"input_price":0.1}',
            "csrf_token": token,
        },
    )
    assert posted.status_code == 302
    async with sessions() as db:
        logs = (
            (
                await db.execute(
                    select(AdminActionLog).where(
                        AdminActionLog.action == "billing_publish_price"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1


@pytest.mark.asyncio
async def test_credit_surface_remains_available_when_payment_entries_disabled(
    billing_client, monkeypatch
):
    async def disabled():
        return False

    client, _ = billing_client
    client._transport.app.dependency_overrides.pop(require_payment_enabled)
    monkeypatch.setattr(web_deps, "is_payment_enabled", disabled)
    assert (await client.get("/api/v1/billing/wallet")).status_code == 200
    assert (await client.get("/billing/credits")).status_code == 200
    assert (await client.get("/api/v1/billing/plans")).status_code == 404
    assert (await client.get("/billing/")).status_code == 404


@pytest.mark.asyncio
async def test_plan_billing_contract_persists_and_renders(billing_client):
    client, sessions = billing_client
    headers = {"x-test-identity": "operator"}
    response = await client.post(
        "/api/v1/billing/admin/plans",
        headers=headers,
        json={
            "name": "Credits test plan",
            "plan_type": "one_time",
            "credit_grant": "21.123456",
            "rate_limits": {"pr_daily": 3, "agent_daily": 1},
            "concurrency_limit": 2,
            "currency": "USD",
            "price_cents": 125,
        },
    )
    assert response.status_code == 200
    plan_id = response.json()["id"]
    response = await client.get("/api/v1/billing/plans")
    assert response.json()[0]["credit_grant"] == "21.123456"
    assert response.json()[0]["rate_limits"] == {"pr_daily": 3, "agent_daily": 1}
    response = await client.get("/billing/admin/plans", headers=headers)
    assert response.status_code == 200
    assert "21.123456 Credits" in response.text and "USD 1.25" in response.text
    assert (
        'id="edit-credit_grant"' in response.text
        and 'id="edit-rate_limits"' in response.text
    )
    response = await client.put(
        f"/api/v1/billing/admin/plans/{plan_id}",
        headers=headers,
        json={"credit_grant": "31.654321"},
    )
    assert response.status_code == 200
    async with sessions() as db:
        logs = (
            (
                await db.execute(
                    select(AdminActionLog).where(
                        AdminActionLog.action.in_(
                            ["billing_create_plan", "billing_update_plan"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 2 and all(row.admin_id == 3 for row in logs)


@pytest.mark.asyncio
async def test_admin_grant_reuses_form_event_and_cannot_double_credit(billing_client):
    import re

    from backend.models.payment_models import Order, Plan

    client, sessions = billing_client
    headers = {"x-test-identity": "operator"}
    async with sessions() as db:
        db.add(
            Plan(
                id=20,
                name="Test Credits grant",
                plan_type="one_time",
                credit_grant=Decimal("7.25"),
                price_cents=0,
            )
        )
        await db.commit()
    assert (
        await client.post(
            "/api/v1/billing/admin/grant",
            headers=headers,
            json={"user_id": 1, "plan_id": 20},
        )
    ).status_code == 422
    page = await client.get("/billing/admin/pricing", headers=headers)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    key = re.search(
        r'name="idempotency_key" value="(admin-grant:[^"]+)"', page.text
    ).group(1)
    payload = {"user_id": 1, "plan_id": 20, "csrf_token": token, "idempotency_key": key}
    first = await client.post("/billing/admin/grant", headers=headers, data=payload)
    second = await client.post("/billing/admin/grant", headers=headers, data=payload)
    assert first.status_code == second.status_code == 302
    async with sessions() as db:
        orders = (
            (await db.execute(select(Order).where(Order.grant_idempotency_key == key)))
            .scalars()
            .all()
        )
        assert len(orders) == 1
        assert (await db.get(BillingWallet, 1)).balance_units == 16_750_000


@pytest.mark.asyncio
async def test_owned_usage_preserves_unknown_and_filters_by_operation(billing_client):
    from backend.models.ai_usage_models import AIUsageRecord

    client, sessions = billing_client
    async with sessions() as db:
        db.add_all(
            [
                AIUsageRecord(
                    record_key="alice-unknown",
                    user_id=1,
                    operation_id="alice-review",
                    feature="pr_review",
                    provider_id="test-provider",
                    model_id="test-model",
                    call_kind="chat",
                    protocol_family="openai_compatible",
                    role="main",
                    input_tokens=None,
                    output_tokens=None,
                    usage_reported=False,
                ),
                AIUsageRecord(
                    record_key="bob-known",
                    user_id=2,
                    operation_id="bob-review",
                    feature="pr_review",
                    provider_id="test-provider",
                    model_id="test-model",
                    call_kind="chat",
                    protocol_family="openai_compatible",
                    role="main",
                    input_tokens=10,
                    output_tokens=0,
                    usage_reported=True,
                ),
            ]
        )
        await db.commit()
    result = await client.get(
        "/api/v1/billing/usage?operation_id=alice-review&user_id=2"
    )
    assert result.status_code == 200
    item = result.json()["items"][0]
    assert item["input_tokens"] is None and item["output_tokens"] is None
    assert item["unknown_calls"] == 1
    result = await client.get("/api/v1/billing/usage?operation_id=bob-review")
    assert result.status_code == 200 and result.json()["items"] == []


@pytest.mark.asyncio
async def test_balance_notices_are_own_only_and_read_idempotently(billing_client):
    from backend.models.billing_models import BillingNotice

    client, sessions = billing_client
    async with sessions() as db:
        db.add_all([BillingNotice(id=10, user_id=1), BillingNotice(id=11, user_id=2)])
        await db.commit()
    response = await client.get("/api/v1/billing/notices?user_id=2")
    assert response.status_code == 200
    assert response.json()["total"] == 1 and response.json()["items"][0]["id"] == 10
    assert (await client.post("/api/v1/billing/notices/11/read")).status_code == 404
    assert (await client.post("/api/v1/billing/notices/10/read")).status_code == 200
    async with sessions() as db:
        first_read_at = (await db.get(BillingNotice, 10)).read_at
        assert (await db.get(BillingNotice, 11)).read_at is None
    assert (await client.post("/api/v1/billing/notices/10/read")).status_code == 200
    async with sessions() as db:
        assert (await db.get(BillingNotice, 10)).read_at == first_read_at
    page = await client.get("/billing/credits")
    assert page.status_code == 200
    assert "Balance notifications" in page.text and "Read" in page.text


@pytest.mark.asyncio
async def test_jpy_integer_minor_units_render_exactly_for_plan_order_and_refund(
    billing_client,
):
    from backend.models.payment_models import Order, RefundRequest

    client, sessions = billing_client
    headers = {"x-test-identity": "operator"}
    created = await client.post(
        "/api/v1/billing/admin/plans",
        headers=headers,
        json={
            "name": "JPY Credits plan",
            "plan_type": "one_time",
            "credit_grant": "2",
            "currency": "JPY",
            "price_cents": 1200,
        },
    )
    assert created.status_code == 200
    plan_id = created.json()["id"]
    async with sessions() as db:
        db.add(
            Order(
                id=30,
                order_no="JPY-TEST-ORDER",
                user_id=1,
                plan_id=plan_id,
                amount_cents=2400,
                refunded_amount_cents=600,
                currency="JPY",
                status="fulfilled",
            )
        )
        db.add(
            RefundRequest(
                id=30,
                order_id=30,
                user_id=1,
                amount_cents=600,
                currency="JPY",
                reason="isolated formatting test",
            )
        )
        await db.commit()
    response = await client.get("/api/v1/billing/plans")
    assert response.json()[0]["price_cents"] == 1200
    assert response.json()[0]["formatted_price"] == "1200"
    response = await client.get("/api/v1/billing/orders/30")
    assert response.json()["amount_cents"] == 2400
    assert response.json()["formatted_amount"] == "2400"
    assert response.json()["formatted_refunded_amount"] == "600"
    response = await client.get("/billing/admin/plans", headers=headers)
    assert response.status_code == 200 and "JPY 1200" in response.text
    assert "JPY 12.00" not in response.text
    response = await client.get("/billing/")
    assert response.status_code == 200
    assert (
        "JPY 1200" in response.text
        and "JPY 2400" in response.text
        and "JPY 600" in response.text
    )
    assert "JPY 12.00" not in response.text and "JPY 24.00" not in response.text
    response = await client.get("/billing/admin/refund-requests", headers=headers)
    assert response.status_code == 200 and "JPY 600" in response.text
    assert "JPY 6.00" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "refunded"])
async def test_user_hides_order_without_erasing_invoice_or_financial_history(
    billing_client, status
):
    import re

    from sqlalchemy.exc import IntegrityError

    from backend.models.legacy_entitlement_models import PaymentReceipt
    from backend.models.payment_models import Order, PaymentLog

    client, sessions = billing_client
    async with sessions() as db:
        db.add(
            Order(
                id=60,
                order_no="PERMANENT-TRON-INVOICE",
                user_id=1,
                amount_cents=1000,
                currency="USD",
                status=status,
                payment_provider="tron",
                invoice_identity="tron:permanent-usdt-atomic-amount",
                plan_snapshot={"name": "Purchased snapshot", "credit_grant": "5"},
            )
        )
        db.add(PaymentLog(order_id=60, user_id=1, action="create", detail="invoice"))
        if status == "refunded":
            db.add(
                PaymentReceipt(provider="tron", event_id="on-chain-paid", order_id=60)
            )
            db.add_all(
                [
                    BillingTransaction(
                        id=60,
                        user_id=1,
                        kind="purchase",
                        delta_units=5_000_000,
                        order_id=60,
                        idempotency_key="hidden:purchase",
                        snapshot={"credit_grant": "5"},
                    ),
                    BillingTransaction(
                        id=61,
                        user_id=1,
                        kind="refund",
                        delta_units=-5_000_000,
                        order_id=60,
                        reference_transaction_id=60,
                        idempotency_key="hidden:refund",
                        snapshot={},
                    ),
                ]
            )
        await db.commit()
    assert (await client.get("/api/v1/billing/orders")).json()["total"] == 1
    page = await client.get("/billing/credits")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert (await client.post("/billing/orders/60/delete")).status_code == 403
    await client.post(
        "/billing/orders/60/delete",
        headers={"x-test-identity": "bob"},
        data={"csrf_token": token},
    )
    assert (await client.get("/api/v1/billing/orders")).json()["total"] == 1
    hidden = await client.post("/billing/orders/60/delete", data={"csrf_token": token})
    assert hidden.status_code == 302
    async with sessions() as db:
        order = await db.get(Order, 60)
        assert order is not None, "User list removal must preserve the original order"
        assert order.hidden_by_user_at is not None
        hidden_at = order.hidden_by_user_at
        assert order.invoice_identity == "tron:permanent-usdt-atomic-amount"
        assert order.plan_snapshot["credit_grant"] == "5"
        assert (
            (await db.execute(select(PaymentLog).where(PaymentLog.order_id == 60)))
            .scalars()
            .all()
        )
        if status == "refunded":
            assert (
                await db.execute(
                    select(PaymentReceipt).where(PaymentReceipt.order_id == 60)
                )
            ).scalar_one()
            assert (await db.get(BillingTransaction, 60)).delta_units == 5_000_000
            assert (await db.get(BillingTransaction, 61)).delta_units == -5_000_000
        assert (await db.get(BillingWallet, 1)).balance_units == 9_500_000
    assert (await client.get("/api/v1/billing/orders")).json() == {
        "total": 0,
        "orders": [],
    }
    assert "PERMANENT-TRON-INVOICE" not in (await client.get("/billing/")).text
    # Hiding is idempotent and retains owner-authorized status access for
    # payment callbacks, reconciliation and immutable ledger source links.
    await client.post("/billing/orders/60/delete", data={"csrf_token": token})
    assert (await client.get("/api/v1/billing/orders/60")).status_code == 200
    async with sessions() as db:
        assert (await db.get(Order, 60)).hidden_by_user_at == hidden_at
        db.add(
            Order(
                order_no="REUSED-TRON-AMOUNT",
                user_id=1,
                amount_cents=1000,
                currency="USD",
                status="pending",
                invoice_identity="tron:permanent-usdt-atomic-amount",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def seed_pending_native_payment(sessions):
    import json

    from backend.models.legacy_entitlement_models import PaymentRefundInboxEvent
    from backend.models.payment_models import Order, Plan
    from backend.services.payment.gateway_base import WebhookEventType
    from backend.services.payment_service import PaymentService

    async with sessions() as db:
        plan = Plan(
            id=50,
            name="Inbox test Credits",
            plan_type="one_time",
            price_cents=1000,
            currency="USD",
            credit_grant=Decimal(5),
        )
        db.add(plan)
        await db.flush()
        snapshot = PaymentService(db)._snapshot_plan(plan)
        db.add(
            Order(
                id=50,
                order_no="INBOX-TEST-ORDER",
                user_id=1,
                plan_id=50,
                amount_cents=1000,
                currency="USD",
                status="pending",
                payment_provider="stripe",
                provider_tx_id="checkout-session",
                plan_snapshot=snapshot,
                metadata_json=json.dumps(
                    {"gateway_amount_cents": 1000, "gateway_currency": "USD"}
                ),
            )
        )
        db.add(
            PaymentRefundInboxEvent(
                id=50,
                provider="stripe",
                event_key="native-payment-50",
                status="pending_reconciliation",
                pending_reason="unknown_order",
                evidence={
                    "type": WebhookEventType.PAYMENT_COMPLETED.value,
                    "order_no": "",
                    "provider_tx_id": "native-paid-50",
                    "payment_reference_id": "native-paid-50",
                    "amount_cents": 1000,
                    "currency": "USD",
                    "contact": "PRIVATE_CONTACT",
                    "raw_payload": "PRIVATE_PAYLOAD",
                    "operator_evidence": "PRIVATE_EVIDENCE",
                },
            )
        )
        db.add(
            PaymentRefundInboxEvent(
                id=51,
                provider="nowpayments",
                event_key="native-unknown-51",
                status="pending_reconciliation",
                pending_reason="refund_evidence_incomplete",
                evidence={
                    "type": WebhookEventType.PAYMENT_REFUNDED.value,
                    "order_no": "",
                    "provider_tx_id": "native-refund-51",
                    "amount_cents": None,
                    "currency": "",
                    "refund_items": [],
                    "refund_evidence_complete": False,
                    "secret": "PRIVATE_SECRET",
                },
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_pending_payment_admin_api_scopes_redacts_and_replays_truthfully(
    billing_client,
):
    client, sessions = billing_client
    await seed_pending_native_payment(sessions)
    assert (await client.get("/api/v1/billing/admin/payment-events")).status_code == 403
    assert (
        await client.post("/api/v1/billing/admin/payment-events/50/replay")
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/billing/admin/payment-events/50/resolve",
            json={"evidence": "forged"},
        )
    ).status_code == 403
    headers = {"x-test-identity": "operator"}
    listed = await client.get(
        "/api/v1/billing/admin/payment-events?limit=1", headers=headers
    )
    assert listed.status_code == 200 and listed.json()["has_more"] is True
    assert len(listed.json()["items"]) == 1 and "PRIVATE" not in listed.text
    assert listed.json()["items"][0]["formatted_amount"] == "10.00"
    unknown = await client.get(
        "/api/v1/billing/admin/payment-events?offset=1", headers=headers
    )
    assert (
        unknown.json()["items"][0]["currency"] == ""
        and unknown.json()["items"][0]["amount_cents"] is None
    )
    replayed = await client.post(
        "/api/v1/billing/admin/payment-events/50/replay", headers=headers
    )
    assert replayed.status_code == 200
    assert (
        replayed.json()["processed"] is False
        and replayed.json()["status"] == "pending_reconciliation"
    )
    assert replayed.json()["pending_reason"] == "unknown_order"


@pytest.mark.asyncio
async def test_admin_payment_resolution_uses_verified_order_and_grants_once(
    billing_client, monkeypatch
):
    from backend.models.legacy_entitlement_models import PaymentRefundInboxAudit

    async def forbidden_gateway(*args, **kwargs):
        raise AssertionError(
            "Incoming event replay must not request any gateway action"
        )

    monkeypatch.setattr("backend.services.payment.get_gateway", forbidden_gateway)
    client, sessions = billing_client
    await seed_pending_native_payment(sessions)
    headers = {"x-test-identity": "operator"}
    payload = {
        "evidence": "Verified provider receipt native-paid-50",
        "order_id": 50,
        "user_id": 2,
    }
    first = await client.post(
        "/api/v1/billing/admin/payment-events/50/resolve", headers=headers, json=payload
    )
    second = await client.post(
        "/api/v1/billing/admin/payment-events/50/resolve", headers=headers, json=payload
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["processed"] is True and first.json()["order_id"] == 50
    assert "PRIVATE" not in first.text and "evidence" not in first.text
    async with sessions() as db:
        assert (await db.get(BillingWallet, 1)).balance_units == 14_500_000
        assert (await db.get(BillingWallet, 2)).balance_units == 80_000_000
        ledger = (
            (
                await db.execute(
                    select(BillingTransaction).where(BillingTransaction.order_id == 50)
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger) == 1 and ledger[0].delta_units == 5_000_000
        audits = (
            (
                await db.execute(
                    select(PaymentRefundInboxAudit).where(
                        PaymentRefundInboxAudit.inbox_event_id == 50
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(row.status == "reviewed" and row.actor_id == 3 for row in audits)


@pytest.mark.asyncio
async def test_pending_payment_webui_renders_unknown_units_and_enforces_csrf(
    billing_client,
):
    import re

    client, sessions = billing_client
    await seed_pending_native_payment(sessions)
    headers = {"x-test-identity": "operator"}
    page = await client.get("/billing/admin/pricing", headers=headers)
    assert page.status_code == 200
    assert "Payment and refund events awaiting reconciliation" in page.text
    assert "Unknown amount" in page.text and "Unknown currency" in page.text
    assert "PRIVATE" not in page.text and "USD 10.00" in page.text
    missing = await client.post(
        "/billing/admin/payment-events/50/resolve",
        headers=headers,
        data={"evidence": "verified proof", "order_id": "50"},
    )
    assert missing.status_code == 403
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    replayed = await client.post(
        "/billing/admin/payment-events/50/replay",
        headers=headers,
        data={"csrf_token": token},
    )
    assert replayed.status_code == 302
    async with sessions() as db:
        assert (await db.get(BillingWallet, 1)).balance_units == 9_500_000
    resolved = await client.post(
        "/billing/admin/payment-events/50/resolve",
        headers=headers,
        data={
            "evidence": "verified provider receipt native-paid-50",
            "order_id": "50",
            "csrf_token": token,
            "checkout_amount_cents": "",
            "checkout_currency": "",
            "refund_reference_id": "",
            "refund_amount_cents": "",
            "refund_currency": "",
        },
    )
    assert resolved.status_code == 302
    async with sessions() as db:
        assert (await db.get(BillingWallet, 1)).balance_units == 14_500_000
    page = await client.get("/billing/admin/pricing", headers=headers)
    assert 'data-payment-event-id="50"' not in page.text
    assert 'data-payment-event-id="51"' in page.text


@pytest.mark.asyncio
async def test_admin_native_refund_evidence_resolution_posts_once_without_gateway_io(
    billing_client, monkeypatch
):
    from backend.models.legacy_entitlement_models import PaymentRefundInboxEvent
    from backend.models.payment_models import Order
    from backend.services.payment.gateway_base import WebhookEventType

    async def forbidden_gateway(*args, **kwargs):
        raise AssertionError("Native refund confirmation cannot start another refund")

    monkeypatch.setattr("backend.services.payment.get_gateway", forbidden_gateway)
    client, sessions = billing_client
    await seed_pending_native_payment(sessions)
    headers = {"x-test-identity": "operator"}
    settled = await client.post(
        "/api/v1/billing/admin/payment-events/50/resolve",
        headers=headers,
        json={"evidence": "verified payment receipt", "order_id": 50},
    )
    assert settled.json()["processed"] is True
    async with sessions() as db:
        db.add(
            PaymentRefundInboxEvent(
                id=52,
                provider="stripe",
                event_key="native-refund-52",
                status="pending_reconciliation",
                pending_reason="refund_evidence_incomplete",
                order_id=50,
                evidence={
                    "type": WebhookEventType.PAYMENT_REFUNDED.value,
                    "order_no": "INBOX-TEST-ORDER",
                    "provider_tx_id": "native-paid-50",
                    "amount_cents": None,
                    "currency": "USD",
                    "refund_evidence_complete": False,
                    "refund_items": [],
                },
            )
        )
        await db.commit()
    proof = {
        "evidence": "verified full refund receipt refund-reference-52",
        "order_id": 50,
        "refund_reference_id": "refund-reference-52",
        "refund_amount_cents": 1000,
        "refund_currency": "USD",
    }
    first = await client.post(
        "/api/v1/billing/admin/payment-events/52/resolve", headers=headers, json=proof
    )
    repeat = await client.post(
        "/api/v1/billing/admin/payment-events/52/resolve", headers=headers, json=proof
    )
    assert first.status_code == repeat.status_code == 200
    assert first.json()["processed"] is True and repeat.json()["processed"] is True
    async with sessions() as db:
        assert (await db.get(BillingWallet, 1)).balance_units == 9_500_000
        assert (await db.get(Order, 50)).refunded_amount_cents == 1000
        rows = (
            (
                await db.execute(
                    select(BillingTransaction).where(BillingTransaction.order_id == 50)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2 and sum(row.delta_units for row in rows) == 0
