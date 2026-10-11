"""Real signed access tokens must obey retained accounts' live auth state."""

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from backend.api.v1 import deps as api_deps
from backend.api.v1 import users as users_api
from backend.api.v1.billing import router as billing_router
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingService
from backend.webui import auth
from backend.webui import deps as web_deps
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest_asyncio.fixture
async def token_client(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        (await db.get(TelegramUser, 2)).role = "super_admin"
        await BillingService(db).grant(1, "5", "TEST-retained-wallet")
        await db.commit()

    async def sessions():
        async with factory() as db:
            yield db

    async def no_global_mfa(db):
        return False

    monkeypatch.setattr(
        "backend.services.security_admin_service.is_global_mfa_required", no_global_mfa
    )
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api/v1")
    app.include_router(billing_router, prefix="/api/v1")
    app.dependency_overrides[web_deps.get_db] = sessions
    app.dependency_overrides[web_deps.require_payment_enabled] = lambda: None

    @app.get("/probe/direct")
    async def direct(user=Depends(api_deps.get_api_current_user)):
        return user

    @app.get("/probe/admin")
    async def admin(user=Depends(api_deps.require_api_admin)):
        return user

    @app.get("/probe/super")
    async def super_admin(user=Depends(api_deps.require_api_super_admin)):
        return user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield client, factory


def signed(user_id=1, **extra):
    return auth.create_access_token(
        {"user_id": user_id, "sub": f"owner{user_id}", "role": "super_admin", **extra}
    )


def credential(client, token, mode):
    client.cookies.clear()
    if mode == "cookie":
        client.cookies.set(auth.WEBUI_TOKEN_COOKIE_NAME, token)
        return {}
    return (
        {"headers": {"Authorization": f"Bearer {token}"}}
        if mode == "bearer"
        else {"params": {"token": token}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bearer", "query", "cookie"])
async def test_retiring_financial_user_invalidates_existing_tokens_on_all_consumers(
    token_client, mode
):
    client, factory = token_client
    old_token = signed()
    params = credential(client, old_token, mode)
    assert (await client.get("/api/v1/billing/wallet", **params)).status_code == 200
    client.cookies.clear()
    removed = await client.delete(
        "/api/v1/users/1", headers={"Authorization": f"Bearer {signed(2)}"}
    )
    assert removed.status_code == 200 and removed.json()["success"]
    assert auth.decode_access_token(old_token) is not None
    for path in (
        "/probe/direct",
        "/probe/admin",
        "/probe/super",
        "/api/v1/billing/wallet",
    ):
        params = credential(client, old_token, mode)
        response = await client.get(path, **params)
        assert response.status_code == 401 and old_token not in response.text
    async with factory() as db:
        assert not (await db.get(TelegramUser, 1)).is_active
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bearer", "query", "cookie"])
async def test_active_tokens_use_latest_identity_and_role_without_non_cookie_renewal(
    token_client, mode
):
    client, factory = token_client
    token = signed()
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        user.role, user.github_username = "user", "renamed-owner"
        await db.commit()
    response = await client.get("/probe/direct", **credential(client, token, mode))
    assert response.status_code == 200
    assert (
        response.json()["role"] == "user" and response.json()["sub"] == "renamed-owner"
    )
    assert "set-cookie" not in response.headers
    assert (
        await client.get("/probe/admin", **credential(client, token, mode))
    ).status_code == 403
    assert (
        await client.get("/probe/super", **credential(client, token, mode))
    ).status_code == 403
    wallet = await client.get(
        "/api/v1/billing/wallet", **credential(client, token, mode)
    )
    assert wallet.status_code == 200
    if mode != "cookie":
        assert "set-cookie" not in wallet.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bearer", "query", "cookie"])
async def test_missing_identity_and_mfa_pending_tokens_are_rejected(token_client, mode):
    client, _ = token_client
    for token in (
        signed(999),
        auth.create_mfa_pending_token(
            {"user_id": 1, "sub": "owner1", "role": "super_admin"}
        ),
    ):
        assert (
            await client.get("/probe/direct", **credential(client, token, mode))
        ).status_code == 401
