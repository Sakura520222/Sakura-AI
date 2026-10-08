"""Admin plan configuration remains available while real payments are disabled."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Request
from sqlalchemy import select

from backend.api.v1 import deps as api_deps
from backend.api.v1.billing import router as api_router
from backend.core import config as core_config
from backend.models.billing_models import BillingTransaction
from backend.models.payment_models import Plan
from backend.webui import deps
from backend.webui.i18n import i18n
from tests.test_billing_pricing_editor import pricing_app as pricing_fixture
from tests.test_billing_pricing_editor import seed_language
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture
pricing_app = pricing_fixture


@pytest.fixture
def disabled_payments_app(pricing_app, monkeypatch):
    app, factory, csrf_token = pricing_app

    async def disabled():
        return False

    async def api_identity(request: Request):
        return {
            "user_id": 1,
            "sub": "owner1",
            "role": request.headers.get("x-test-role", "super_admin"),
        }

    monkeypatch.setattr(deps, "is_payment_enabled", disabled)
    monkeypatch.setattr(api_deps, "get_api_current_user", api_identity)
    app.include_router(api_router, prefix="/api/v1")
    assert deps.require_payment_enabled not in app.dependency_overrides
    return app, factory, csrf_token


def plan_fields(csrf_token, **extra):
    return {
        "csrf_token": csrf_token,
        "name": "TEST plan definition",
        "plan_type": "one_time",
        "price_cents": "12345",
        "currency": "JPY",
        "credit_grant": "2",
        "rate_limits": "{}",
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("language,global_language", [("zh-CN", "en"), ("en", "zh-CN")])
async def test_plan_configuration_is_available_without_enabling_payments(
    disabled_payments_app, monkeypatch, language, global_language
):
    app, factory, csrf_token = disabled_payments_app
    await seed_language(factory, language)
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: global_language)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get("/billing/admin/plans")
        assert response.status_code == 200, response.text
        response = await client.post(
            "/billing/admin/plans", data=plan_fields(csrf_token)
        )
        assert response.status_code == 302, response.text
        toast = parse_qs(urlsplit(response.headers["location"]).query)
        assert toast["_toast"] == [i18n.t("toast.plan_created", lang=language)]
        async with factory() as db:
            plan = (await db.execute(select(Plan))).scalar_one()
            plan_id = plan.id
        response = await client.post(
            f"/billing/admin/plans/{plan_id}/edit",
            data={"csrf_token": csrf_token, "price_cents": "23456"},
        )
        assert response.status_code == 302, response.text
        toast = parse_qs(urlsplit(response.headers["location"]).query)
        assert toast["_toast"] == [i18n.t("toast.plan_updated", lang=language)]
        for path, fields in (
            (f"/billing/admin/plans/{plan_id}/toggle", {}),
            ("/billing/admin/plans/batch-toggle", {"plan_ids": str(plan_id)}),
            ("/billing/admin/plans/batch-delete", {"plan_ids": str(plan_id)}),
            (f"/billing/admin/plans/{plan_id}/delete", {}),
        ):
            response = await client.post(
                path, data={"csrf_token": csrf_token, **fields}
            )
            assert response.status_code == 302, response.text
            assert parse_qs(urlsplit(response.headers["location"]).query)[
                "_toast_type"
            ] == ["success"]
    async with factory() as db:
        plan = (await db.execute(select(Plan))).scalar_one()
        assert plan.price_cents == 23456 and plan.currency == "JPY"
        assert plan.credit_grant == 2 and plan.is_active is False
        assert (await db.execute(select(BillingTransaction))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_plan_configuration_still_requires_superadmin(
    disabled_payments_app, role
):
    app, factory, csrf_token = disabled_payments_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
        headers={"x-test-role": role},
    ) as client:
        response = await client.get("/billing/admin/plans")
        assert response.status_code == 403, response.text
        response = await client.post(
            "/billing/admin/plans", data=plan_fields(csrf_token)
        )
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
        for path in (
            "/billing/admin/plans/1/edit",
            "/billing/admin/plans/1/toggle",
            "/billing/admin/plans/1/delete",
            "/billing/admin/plans/batch-toggle",
            "/billing/admin/plans/batch-delete",
        ):
            response = await client.post(
                path, data={"csrf_token": csrf_token, "plan_ids": "1"}
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
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/plans", data=plan_fields("invalid-csrf")
        )
        assert response.status_code == 403, response.text
        for path in (
            "/billing/admin/plans/1/edit",
            "/billing/admin/plans/1/toggle",
            "/billing/admin/plans/1/delete",
            "/billing/admin/plans/batch-toggle",
            "/billing/admin/plans/batch-delete",
        ):
            response = await client.post(
                path, data={"csrf_token": "invalid-csrf", "plan_ids": "1"}
            )
            assert response.status_code == 403, response.text
        for method, path, kwargs, expected_status in (
            ("GET", "/billing/", {}, 200),
            (
                "POST",
                "/billing/purchase/1",
                {"data": {"csrf_token": "invalid-csrf"}},
                404,
            ),
            (
                "POST",
                "/api/v1/billing/orders",
                {"json": {"plan_id": 1, "provider": "stripe"}},
                404,
            ),
            ("POST", "/api/v1/billing/orders/1/refund", {"json": {}}, 400),
            (
                "POST",
                "/billing/admin/refund-requests/1/approve",
                {"data": {"csrf_token": "invalid-csrf"}},
                403,
            ),
            ("POST", "/billing/admin/codes/generate", {"data": {}}, 404),
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
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("language,global_language", [("zh-CN", "en"), ("en", "zh-CN")])
async def test_plan_validation_failure_follows_user_locale(
    disabled_payments_app, monkeypatch, language, global_language
):
    app, factory, csrf_token = disabled_payments_app
    await seed_language(factory, language)
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: global_language)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/plans",
            data=plan_fields(csrf_token, rate_limits='{"unknown":1}'),
        )
    assert response.status_code == 302, response.text
    toast = parse_qs(urlsplit(response.headers["location"]).query)
    field_error = i18n.t(
        "toast.value_invalid",
        lang=language,
        field_key=i18n.t("billing.rate_limits_config", lang=language),
    )
    assert toast["_toast"] == [
        i18n.t("toast.payment_error", lang=language, error=field_error)
    ]
    async with factory() as db:
        assert (await db.execute(select(Plan))).scalars().all() == []
