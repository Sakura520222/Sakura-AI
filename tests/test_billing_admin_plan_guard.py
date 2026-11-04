"""Admin plan configuration remains available while real payments are disabled."""

import httpx
import pytest
from sqlalchemy import select

from backend.api.v1 import billing_webui
from backend.models.billing_models import BillingTransaction
from backend.models.payment_models import Plan
from backend.webui import deps
from tests.billing_webui_fixtures import pricing_app as pricing_fixture
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture
pricing_app = pricing_fixture


@pytest.fixture
def disabled_payments_app(pricing_app, monkeypatch):
    app, factory, csrf_token = pricing_app

    async def disabled():
        return False

    monkeypatch.setattr(deps, "is_payment_enabled", disabled)
    monkeypatch.setattr(billing_webui, "get_dynamic_config", lambda key: disabled())
    assert deps.require_payment_enabled not in app.dependency_overrides
    return app, factory, csrf_token


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_plan_configuration_still_requires_superadmin(
    disabled_payments_app, role
):
    app, factory, _csrf_token = disabled_payments_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-test-role": role},
    ) as client:
        response = await client.get("/api/v1/billing/admin/plans")
        assert response.status_code == 403, response.text
        response = await client.post(
            "/api/v1/billing/admin/plans",
            json={
                "name": "TEST",
                "plan_type": "one_time",
                "price_cents": 10,
                "credit_grant": "2",
            },
        )
        assert response.status_code == 403, response.text
        response = await client.put(
            "/api/v1/billing/admin/plans/1", json={"price_cents": 20}
        )
        assert response.status_code == 403, response.text
        response = await client.delete("/api/v1/billing/admin/plans/1")
        assert response.status_code == 403, response.text
    async with factory() as db:
        assert (await db.execute(select(Plan))).scalars().all() == []


@pytest.mark.asyncio
async def test_disabled_payment_guard_preserves_purchase_refund_and_csrf_boundaries(
    disabled_payments_app,
):
    app, factory, _ = disabled_payments_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path, kwargs, expected_status in (
            ("GET", "/api/v1/billing/context", {}, 200),
            (
                "POST",
                "/api/v1/billing/orders",
                {"json": {"plan_id": 1, "provider": "stripe"}},
                404,
            ),
            ("POST", "/api/v1/billing/orders/1/refund", {"json": {}}, 400),
        ):
            response = await client.request(method, path, **kwargs)
            assert response.status_code == expected_status, response.text
    async with factory() as db:
        assert (await db.execute(select(Plan))).scalars().all() == []
        assert (await db.execute(select(BillingTransaction))).scalars().all() == []


@pytest.mark.asyncio
async def test_api_plan_crud_is_configuration_only_with_payments_disabled(
    disabled_payments_app,
):
    app, factory, _ = disabled_payments_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/billing/admin/plans")
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/v1/billing/admin/plans",
            json={
                "name": "TEST API",
                "plan_type": "one_time",
                "price_cents": 10,
                "credit_grant": "2",
            },
        )
        assert response.status_code == 200, response.text
        plan_id = response.json()["id"]
        response = await client.put(
            f"/api/v1/billing/admin/plans/{plan_id}", json={"price_cents": 20}
        )
        assert response.status_code == 200, response.text
        response = await client.delete(f"/api/v1/billing/admin/plans/{plan_id}")
        assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.execute(select(Plan))).scalar_one().price_cents == 20
        assert (await db.execute(select(BillingTransaction))).scalars().all() == []
