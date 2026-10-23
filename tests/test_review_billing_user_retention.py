"""Protected deletion uses real FK enforcement and keeps financial provenance."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from backend.api.v1 import deps as api_deps
from backend.api.v1 import users as api_users
from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import (
    BillingOperation,
    BillingTransaction,
    BillingWallet,
)
from backend.models.database import WebUIConfig
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    RateLimitAdmission,
)
from backend.models.payment_models import Order, Plan
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingService
from backend.webui import deps as web_deps
from backend.webui.routes import users as web_users
from tests.test_billing_usage_attribution import DurableSQLSession
from tests.test_billing_usage_attribution import sql_runtime as real_runtime_fixture

sql_runtime = real_runtime_fixture


class RetentionSession(DurableSQLSession):
    async def delete(self, instance):
        self.session.delete(instance)


@pytest_asyncio.fixture
async def retention_client(sql_runtime, monkeypatch):
    _, engine, _ = sql_runtime

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    engine.dispose()

    def sessions():
        return RetentionSession(Session(engine, expire_on_commit=False))

    async with sessions() as db:
        assert (await db.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        db.add(TelegramUser(id=3, github_username="operator", role="super_admin"))
        db.add(TelegramUser(id=4, github_username="unused", role="user"))
        await db.commit()
        db.add(WebUIConfig(user_id=3, language="en"))
        await db.commit()

    async def current_user(request: Request):
        identity = request.headers.get("x-test-identity", "operator")
        return {
            "operator": {"user_id": 3, "sub": "operator", "role": "super_admin"},
            "owner": {"user_id": 1, "sub": "owner1", "role": "user"},
        }[identity]

    async def no_mfa(*args, **kwargs):
        return False

    async def db_dependency():
        async with sessions() as db:
            yield db

    monkeypatch.setattr(web_deps, "get_current_user", current_user)
    monkeypatch.setattr(web_deps, "user_requires_mfa_enrollment", no_mfa)
    monkeypatch.setattr(api_deps, "get_api_current_user", current_user)
    monkeypatch.setattr(api_deps, "user_requires_mfa_enrollment", no_mfa)
    monkeypatch.setattr(api_deps.db_module, "async_session", sessions)
    app = FastAPI()
    app.include_router(api_users.router, prefix="/api/v1")
    app.include_router(web_users.router)
    app.dependency_overrides[web_deps.get_db] = db_dependency
    app.dependency_overrides[web_deps.get_user_preferences] = lambda: {"language": "en"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, sessions


async def delete_through(client, surface, user_id, *, identity="operator", csrf=True):
    headers = {"x-test-identity": identity}
    if surface == "api":
        return await client.delete(f"/api/v1/users/{user_id}", headers=headers)
    data = {"csrf_token": web_deps.get_csrf_serializer().dumps({})} if csrf else {}
    return await client.post(f"/users/{user_id}/delete", headers=headers, data=data)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
@pytest.mark.parametrize(
    "source",
    [
        "wallet",
        "purchase",
        "order_only",
        "legacy_only",
        "operation_only",
        "admission_only",
    ],
)
async def test_protected_delete_deactivates_financial_user_and_keeps_sources(
    retention_client, surface, source
):
    client, sessions = retention_client
    async with sessions() as db:
        if source == "wallet":
            await BillingService(db).get_wallet(1)
        elif source == "purchase":
            await BillingService(db).grant(1, "7", "TEST-owned-source")
        elif source == "legacy_only":
            db.add(
                LegacyEntitlement(
                    user_id=1, source_key="TEST-legacy", snapshot={}, pr_remaining=2
                )
            )
        elif source == "operation_only":
            db.add(
                BillingOperation(
                    operation_id="TEST-restored-operation", user_id=1, feature="agent"
                )
            )
        elif source == "admission_only":
            db.add(
                RateLimitAdmission(
                    event_key="TEST-rate-admission",
                    user_id=1,
                    feature="pr_review",
                    source_kind="periodic",
                    repo_name="TEST/repo",
                    number=1,
                )
            )
        else:
            plan = Plan(name="TEST history", plan_type="one_time", price_cents=100)
            db.add(plan)
            await db.flush()
            db.add(
                Order(
                    order_no="TEST-owned-history",
                    user_id=1,
                    plan_id=plan.id,
                    amount_cents=100,
                    status="fulfilled",
                )
            )
        await db.commit()
        before = list(
            (
                await db.execute(
                    select(BillingTransaction.id, BillingTransaction.delta_units)
                )
            ).all()
        )

    response = await delete_through(client, surface, 1)
    assert response.status_code == (200 if surface == "api" else 302)
    if surface == "api":
        assert response.json()["success"] is True
        assert "financial" in response.json()["message"]
    else:
        toast = parse_qs(urlparse(response.headers["location"]).query)["_toast"][0]
        assert "financial" in toast
    async with sessions() as db:
        target = await db.get(TelegramUser, 1)
        assert target is not None and not target.is_active
        assert (
            list(
                (
                    await db.execute(
                        select(BillingTransaction.id, BillingTransaction.delta_units)
                    )
                ).all()
            )
            == before
        )
        if source == "wallet":
            assert (await db.get(BillingWallet, 1)).balance_units == 0
        elif source == "purchase":
            assert (await BillingService(db).reconcile_wallet(1))["consistent"]
        elif source == "order_only":
            assert (
                await db.execute(select(Order.order_no))
            ).scalar_one() == "TEST-owned-history"
        elif source == "legacy_only":
            assert (
                await db.execute(select(LegacyEntitlement.pr_remaining))
            ).scalar_one() == 2
        elif source == "operation_only":
            assert (
                await db.get(BillingOperation, "TEST-restored-operation")
            ).user_id == 1
        else:
            assert (
                await db.execute(select(RateLimitAdmission.event_key))
            ).scalar_one() == "TEST-rate-admission"
        audit = (await db.execute(select(AdminActionLog))).scalar_one()
        assert audit.target_id == "1" and audit.action == "user_delete"
        assert json.loads(audit.detail)["retained_financial_records"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
async def test_user_without_financial_history_can_still_be_deleted(
    retention_client, surface
):
    client, sessions = retention_client
    response = await delete_through(client, surface, 4)
    assert response.status_code == (200 if surface == "api" else 302)
    async with sessions() as db:
        assert await db.get(TelegramUser, 4) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
async def test_retention_keeps_delete_auth_self_and_missing_user_guards(
    retention_client, surface
):
    client, sessions = retention_client
    assert (
        await delete_through(client, surface, 4, identity="owner")
    ).status_code == 403
    self_delete = await delete_through(client, surface, 3)
    assert self_delete.status_code == (400 if surface == "api" else 302)
    if surface == "web":
        assert (await delete_through(client, surface, 4, csrf=False)).status_code == 403
    missing = await delete_through(client, surface, 999)
    assert missing.status_code == 404
    async with sessions() as db:
        assert (await db.get(TelegramUser, 3)).is_active
        assert (await db.get(TelegramUser, 4)).is_active
        assert (await db.execute(select(AdminActionLog))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["zh-CN", "en"])
async def test_user_removal_page_explains_financial_retention(retention_client, lang):
    client, _ = retention_client
    client._transport.app.dependency_overrides[web_deps.get_user_preferences] = lambda: {
        "language": lang
    }
    page = await client.get("/users/1")
    assert page.status_code == 200
    if lang == "en":
        assert "financial and entitlement records are retained" in page.text
        assert "permanently delete user" not in page.text
        assert "Remove User" in page.text
    else:
        assert "账务及权益记录会保留" in page.text
        assert "永久删除用户" not in page.text
        assert "移除用户" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
@pytest.mark.parametrize("lang", ["zh-CN", "en"])
async def test_retained_delete_reply_uses_admin_language(
    retention_client, surface, lang
):
    client, sessions = retention_client
    client._transport.app.dependency_overrides[web_deps.get_user_preferences] = lambda: {
        "language": lang
    }
    async with sessions() as db:
        preferences = (
            await db.execute(select(WebUIConfig).where(WebUIConfig.user_id == 3))
        ).scalar_one()
        preferences.language = lang
        await BillingService(db).get_wallet(1)
        await db.commit()
    response = await delete_through(client, surface, 1)
    message = (
        response.json()["message"]
        if surface == "api"
        else parse_qs(urlparse(response.headers["location"]).query)["_toast"][0]
    )
    assert message == (
        "用户 owner1 已停用，账务及权益记录已保留"
        if lang == "zh-CN"
        else "User owner1 deactivated; financial and entitlement records retained"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
async def test_failed_retention_commit_rolls_back_without_success(
    retention_client, monkeypatch, surface
):
    client, sessions = retention_client
    async with sessions() as db:
        await BillingService(db).grant(1, "7", "TEST-write-failure-source")
        await db.commit()
    original = RetentionSession.commit

    async def fail_retention(self):
        if any(
            isinstance(row, TelegramUser) and not row.is_active
            for row in self.session.dirty
        ):
            raise RuntimeError("TEST retention write interrupted")
        await original(self)

    monkeypatch.setattr(RetentionSession, "commit", fail_retention)
    response = await delete_through(client, surface, 1)
    if surface == "api":
        assert response.status_code == 400 and response.json()["success"] is False
        assert response.json()["error"] == "User deletion failed"
    else:
        assert parse_qs(urlparse(response.headers["location"]).query)[
            "_toast_type"
        ] == ["error"]
    async with sessions() as db:
        assert (await db.get(TelegramUser, 1)).is_active
        assert (await db.get(BillingWallet, 1)).balance_units == 7_000_000
        assert (await db.execute(select(AdminActionLog))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "web"])
async def test_retention_targets_id_even_when_username_is_unbound_or_case_ambiguous(
    retention_client, surface
):
    client, sessions = retention_client
    async with sessions() as db:
        (await db.get(TelegramUser, 1)).github_username = None
        (await db.get(TelegramUser, 2)).github_username = "caseTwin"
        (await db.get(TelegramUser, 4)).github_username = "CaseTwin"
        await BillingService(db).get_wallet(1)
        await BillingService(db).get_wallet(2)
        await db.commit()
    for user_id in (1, 2):
        assert (await delete_through(client, surface, user_id)).status_code == (
            200 if surface == "api" else 302
        )
    async with sessions() as db:
        assert not (await db.get(TelegramUser, 1)).is_active
        assert not (await db.get(TelegramUser, 2)).is_active
        assert (await db.get(TelegramUser, 4)).is_active
