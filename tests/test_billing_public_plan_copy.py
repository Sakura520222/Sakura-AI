"""Customer plan cards contain product copy, not administrator field help."""

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from backend.models.payment_models import Plan
from backend.webui import deps
from tests.billing_webui_fixtures import pricing_app as pricing_fixture
from tests.billing_webui_fixtures import seed_language
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

pricing_app = pricing_fixture
sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["zh-CN", "en"])
@pytest.mark.parametrize("role", ["user", "super_admin"])
@pytest.mark.parametrize("description", [None, "Description written for customers"])
async def test_customer_card_never_inserts_admin_help_as_description(
    pricing_app, monkeypatch, lang, role, description
):
    app, factory, _ = pricing_app
    await seed_language(factory, lang)

    async def enabled():
        return True

    monkeypatch.setattr(deps, "is_payment_enabled", enabled)
    async with factory() as db:
        db.add(
            Plan(
                name="Plus",
                plan_type="subscription",
                duration_days=30,
                price_cents=500,
                currency="USD",
                credit_grant=Decimal(1000),
                concurrency_limit=3,
                description=description,
            )
        )
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get(
            "/api/v1/billing/plans", headers={"x-test-role": role}
        )
        assert response.status_code == 200, response.text
        row = next(row for row in response.json() if row["name"] == "Plus")
        assert row["description"] == description
        assert row["concurrency_limit"] == 3
        assert row["currency"] == "USD" and row["formatted_price"] == "5.00"
        assert Decimal(row["credit_grant"]) == Decimal(1000)
        admin = await client.get(
            "/api/v1/billing/admin/plans", headers={"x-test-role": role}
        )
        assert admin.status_code == (200 if role == "super_admin" else 403)
    async with factory() as db:
        saved = (await db.execute(select(Plan))).scalar_one()
        assert saved.description == description
