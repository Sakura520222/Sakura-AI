"""Customer plan cards contain product copy, not administrator field help."""

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from backend.models.payment_models import Plan
from backend.webui import deps
from backend.webui.i18n import i18n
from tests.test_billing_pricing_editor import pricing_app as pricing_fixture
from tests.test_billing_pricing_editor import seed_language
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
        response = await client.get("/billing/", headers={"x-test-role": role})
        assert response.status_code == 200, response.text
        assert i18n.t("billing.concurrency_limit_help", lang=lang) not in response.text
        assert i18n.t("billing.concurrency_limit", lang=lang) not in response.text
        assert (
            "同时任务上限：3" if lang == "zh-CN" else "Concurrent task limit: 3"
        ) in response.text
        assert "USD 5.00" in response.text
        assert "1000.000000 Credits" in response.text
        if description:
            assert description in response.text
        else:
            assert "Description written for customers" not in response.text
        if role == "super_admin":
            admin = await client.get("/billing/admin/plans")
            assert admin.status_code == 200
            assert i18n.t("billing.concurrency_limit_help", lang=lang) in admin.text
    async with factory() as db:
        saved = (await db.execute(select(Plan))).scalar_one()
        assert saved.description == description
