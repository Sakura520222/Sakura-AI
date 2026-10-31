"""Billing JSON adapters retain existing service authority and cookie protection."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import billing_webui as routes
from backend.api.v1 import deps
from backend.api.v1.billing import PlanCreateRequest
from backend.core.time_service import now_utc
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME
from backend.webui.deps import get_csrf_serializer, get_db


@pytest.fixture
def billing_app(monkeypatch):
    user = {"user_id": 7, "role": "super_admin", "sub": "alice"}

    async def auth(request=None):
        return user

    # Preserve the actual require_api_super_admin role decision while replacing
    # only its authenticated identity (no credentials or gateways in this test).
    monkeypatch.setattr(deps, "require_api_auth", auth)
    session = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=lambda row: None,
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.require_api_auth] = auth
    app.dependency_overrides[get_db] = lambda: session
    return app, user, session


@pytest.mark.asyncio
async def test_admin_catalog_uses_current_role_and_no_store(billing_app, monkeypatch):
    app, user, _ = billing_app
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(
        routes, "PaymentService", lambda db: SimpleNamespace(list_plans=listing)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        user["role"] = "admin"
        denied = await client.get("/api/v1/billing/admin/plans")
        assert denied.status_code == 403
        listing.assert_not_awaited()
        user["role"] = "super_admin"
        allowed = await client.get("/api/v1/billing/admin/plans")
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"
        listing.assert_awaited_once_with(active_only=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer "])
async def test_cookie_refund_write_requires_csrf_before_service(
    billing_app, monkeypatch, authorization
):
    app, _, _ = billing_app
    submit = AsyncMock()
    monkeypatch.setattr(
        routes,
        "PaymentService",
        lambda db: SimpleNamespace(submit_refund_request=submit),
    )
    headers = {"Authorization": authorization} if authorization else {}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "session"},
    ) as client:
        response = await client.post(
            "/api/v1/billing/orders/9/refund-request",
            json={"reason": "unused"},
            headers=headers,
        )
        assert response.status_code == 403
        submit.assert_not_awaited()


def refund_row(status="pending"):
    return SimpleNamespace(
        id=12,
        order_id=9,
        order=SimpleNamespace(order_no="ORDER-9"),
        user_id=7,
        status=status,
        reason="unused",
        review_note=None,
        amount_cents=123,
        currency="USD",
        requested_at=now_utc(),
        reviewed_at=None,
    )


@pytest.mark.asyncio
async def test_refund_uses_authenticated_owner_and_rejects_owner_injection(
    billing_app, monkeypatch
):
    app, _, db = billing_app
    submit = AsyncMock(return_value=refund_row())
    monkeypatch.setattr(
        routes,
        "PaymentService",
        lambda db: SimpleNamespace(submit_refund_request=submit),
    )
    headers = {"X-CSRF-Token": get_csrf_serializer().dumps({})}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "session"},
    ) as client:
        injected = await client.post(
            "/api/v1/billing/orders/9/refund-request",
            json={"reason": "unused", "user_id": 99},
            headers=headers,
        )
        assert injected.status_code == 422
        submit.assert_not_awaited()
        response = await client.post(
            "/api/v1/billing/orders/9/refund-request",
            json={"reason": "unused"},
            headers=headers,
        )
        assert response.status_code == 200
        submit.assert_awaited_once_with(order_id=9, user_id=7, reason="unused")
        db.commit.assert_awaited_once()
        assert response.json()["status"] == "pending"
        assert response.json()["formatted_amount"] == "1.23"


@pytest.mark.asyncio
async def test_refund_approval_keeps_unknown_outcome_pending(billing_app, monkeypatch):
    app, _, db = billing_app
    approve = AsyncMock(return_value=refund_row("pending"))
    monkeypatch.setattr(
        routes,
        "PaymentService",
        lambda db: SimpleNamespace(
            approve_refund_request=approve, reject_refund_request=AsyncMock()
        ),
    )
    audits = []
    monkeypatch.setattr(
        routes, "add_billing_admin_audit", lambda db, **kwargs: audits.append(kwargs)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/v1/billing/admin/refund-requests/12/approve",
            json={"review_note": "verified"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        approve.assert_awaited_once_with(
            request_id=12, reviewer_id=7, review_note="verified"
        )
        assert audits[0]["detail"]["status"] == "pending"
        db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_hide_order_filters_owner_status_and_takes_current_lock(billing_app):
    app, _, db = billing_app
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post("/api/v1/billing/orders/9/hide")
        assert response.status_code == 404
        query = db.execute.call_args.args[0]
        compiled = query.compile()
        assert 7 in compiled.params.values()
        assert 9 in compiled.params.values()
        assert [
            "pending",
            "cancelled",
            "expired",
            "refunded",
        ] in compiled.params.values()
        assert query._for_update_arg is not None
        assert query.get_execution_options()["populate_existing"] is True
        db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_sales_keep_billing_context_without_provider_discovery(
    billing_app, monkeypatch
):
    app, _, _ = billing_app
    monkeypatch.setattr(routes, "get_dynamic_config", AsyncMock(return_value=False))
    discover = AsyncMock()
    monkeypatch.setattr(routes, "get_configured_providers", discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/v1/billing/context")
        assert response.status_code == 200
        assert response.json()["payment_enabled"] is False
        assert response.json()["providers"] == []
        assert response.json()["currency_minor_exponents"]["JPY"] == 0
        discover.assert_not_awaited()


def test_plan_projection_preserves_exact_credits():
    values = PlanCreateRequest(
        name="Precise", plan_type="one_time", credit_grant="9007199254.000001"
    ).model_dump()
    row = SimpleNamespace(
        **{**values, "credit_grant": Decimal("9007199254.000001")}, id=1, is_active=True
    )
    projected = routes._plan_view(row)
    assert projected["credit_grant"] == "9007199254.000001"
    assert projected["formatted_price"] == "0.00"


@pytest.mark.asyncio
async def test_entitlements_use_authenticated_account_and_effective_limits(
    billing_app, monkeypatch
):
    from backend.services import legacy_entitlement_service, quota_service

    app, _, db = billing_app
    account = SimpleNamespace(
        id=7,
        daily_used=1,
        weekly_used=2,
        monthly_used=3,
        issue_daily_used=4,
        issue_weekly_used=5,
        issue_monthly_used=6,
        agent_daily_used=7,
        agent_weekly_used=8,
        agent_monthly_used=9,
    )
    db.get = AsyncMock(return_value=account)
    reset = AsyncMock()
    monkeypatch.setattr(
        quota_service,
        "QuotaService",
        lambda db: SimpleNamespace(reset_user_quotas_if_expired=reset),
    )
    limits = AsyncMock(return_value=(10, 20, 30))
    remaining = AsyncMock(return_value={"pr": 5, "issue": 6, "agent": 7})
    monkeypatch.setattr(
        legacy_entitlement_service,
        "LegacyEntitlementService",
        lambda db: SimpleNamespace(effective_limits=limits, remaining=remaining),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/v1/billing/entitlements?user_id=99")
        assert response.status_code == 200
        assert db.get.call_args.args[1] == 7
        reset.assert_awaited_once_with(account)
        remaining.assert_awaited_once_with(7)
        assert response.json()["features"]["agent"] == [
            {"period": "daily", "limit": 10, "used": 7},
            {"period": "weekly", "limit": 20, "used": 8},
            {"period": "monthly", "limit": 30, "used": 9},
        ]
