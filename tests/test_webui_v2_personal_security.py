"""Security enrollment and notification adapters never broaden account scope."""

from unittest.mock import AsyncMock

import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import notifications
from backend.api.v1 import personal_security as security
from backend.api.v1.deps import require_api_auth
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME, create_mfa_pending_token
from backend.webui.deps import generate_csrf_token, get_db


@pytest_asyncio.fixture
async def api(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(security.router, prefix="/api/v1")
    app.include_router(notifications.router, prefix="/api/v1")
    user = {"user_id": 1, "sub": "alice", "role": "user"}
    app.dependency_overrides[security.security_user] = lambda: user
    app.dependency_overrides[require_api_auth] = lambda: user

    async def request_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    monkeypatch.setattr(
        security, "user_requires_mfa_enrollment", AsyncMock(return_value=False)
    )
    async with engine.begin() as connection:
        for model in (TelegramUser, UserRecoveryCode, UserWebAuthnCredential):
            await connection.run_sync(model.__table__.create)
    async with sessions() as db:
        db.add_all(
            [
                TelegramUser(
                    id=1, github_username="alice", is_active=True, mfa_required=True
                ),
                TelegramUser(id=2, github_username="bob", is_active=True),
                UserWebAuthnCredential(
                    id=1,
                    user_id=2,
                    credential_id="private-credential",
                    public_key="private-public-key",
                    device_name="Bob's key",
                ),
            ]
        )
        await db.commit()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            yield app, client, user, sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_totp_setup_enable_replay_and_one_time_recovery_codes(api, monkeypatch):
    _, client, _, sessions = api
    headers = {"X-CSRF-Token": generate_csrf_token()}
    saved = {}

    async def save(user_id, secret):
        saved[user_id] = secret

    async def pop(user_id):
        return saved.pop(user_id, None)

    monkeypatch.setattr(security.legacy, "_save_totp_setup_secret", save)
    monkeypatch.setattr(security.legacy, "_pop_totp_setup_secret", pop)
    monkeypatch.setattr(security.totp, "get_current_totp_step", lambda: 123456)
    response = await client.post("/api/v1/webui/security/totp/setup", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    secret = response.json()["data"]["secret"]
    code = pyotp.TOTP(secret).at(123456 * 30)
    enabled = await client.post(
        "/api/v1/webui/security/totp/enable", json={"code": code}, headers=headers
    )
    assert enabled.status_code == 200
    codes = enabled.json()["data"]["recovery_codes"]
    assert codes
    async with sessions() as db:
        alice = await db.get(TelegramUser, 1)
        assert alice.totp_enabled and not alice.mfa_required
        assert alice.totp_secret_encrypted != secret
        assert alice.totp_last_used_step == 123456
    status = await client.get("/api/v1/webui/security")
    assert status.status_code == 200
    assert status.json()["data"]["totp_enabled"] is True
    assert secret not in status.text and all(item not in status.text for item in codes)
    assert (
        "private-credential" not in status.text
        and "private-public-key" not in status.text
    )
    replay = await client.post(
        "/api/v1/webui/security/recovery-codes", json={"code": code}, headers=headers
    )
    assert replay.status_code == 400
    assert replay.json()["detail"] == "invalid_code"
    disabled = await client.post(
        "/api/v1/webui/security/totp/disable", json={"code": codes[0]}, headers=headers
    )
    assert disabled.status_code == 200
    async with sessions() as db:
        alice = await db.get(TelegramUser, 1)
        assert not alice.totp_enabled and alice.totp_secret_encrypted is None
        assert (
            await db.execute(
                select(UserRecoveryCode).where(UserRecoveryCode.user_id == 1)
            )
        ).scalars().all() == []


@pytest.mark.asyncio
async def test_passkey_deletion_never_deletes_another_users_credential(api):
    _, client, _, sessions = api
    headers = {"X-CSRF-Token": generate_csrf_token()}
    assert (await client.delete("/api/v1/webui/security/passkeys/1")).status_code == 422
    assert (
        await client.delete("/api/v1/webui/security/passkeys/1", headers=headers)
    ).status_code == 200
    async with sessions() as db:
        assert await db.get(UserWebAuthnCredential, 1) is not None


@pytest.mark.asyncio
async def test_security_rejects_incomplete_mfa_login(api):
    app, client, _, _ = api
    app.dependency_overrides.pop(security.security_user)
    assert (await client.get("/api/v1/webui/security")).status_code == 401
    token = create_mfa_pending_token({"user_id": 1, "sub": "alice", "role": "user"})
    client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, token)
    assert (await client.get("/api/v1/webui/security")).status_code == 401


@pytest.mark.asyncio
async def test_security_enrollment_exemption_still_uses_live_authenticator(
    api, monkeypatch
):
    app, client, user, _ = api
    app.dependency_overrides.pop(security.security_user)
    auth = AsyncMock(return_value=user)
    monkeypatch.setattr(security, "get_api_current_user", auth)
    response = await client.get("/api/v1/webui/security")
    assert response.status_code == 200
    auth.assert_awaited_once()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_passkey_options_and_verification_keep_shared_challenge_handlers(
    api, monkeypatch
):
    _, client, user, _ = api
    options = AsyncMock(
        return_value={"success": True, "data": {"challenge_id": "one-use"}}
    )
    verify = AsyncMock(return_value={"success": True, "data": {"id": 5}})
    monkeypatch.setattr(security.legacy, "passkey_register_options", options)
    monkeypatch.setattr(security.legacy, "passkey_register_verify", verify)
    headers = {"X-CSRF-Token": generate_csrf_token()}
    assert (
        await client.post("/api/v1/webui/security/passkeys/options")
    ).status_code == 422
    options.assert_not_awaited()
    response = await client.post(
        "/api/v1/webui/security/passkeys/options", headers=headers
    )
    assert response.json()["data"]["challenge_id"] == "one-use"
    payload = {
        "challenge_id": "one-use",
        "credential": {"id": "assertion"},
        "device_name": "Laptop",
    }
    response = await client.post(
        "/api/v1/webui/security/passkeys/verify", json=payload, headers=headers
    )
    assert response.status_code == 200
    assert verify.await_args.kwargs["user"] is user
    assert verify.await_args.kwargs["body"] == payload


@pytest.mark.asyncio
async def test_notification_adapter_requires_csrf_and_passes_only_authenticated_owner(
    api, monkeypatch
):
    _, client, user, _ = api
    add = AsyncMock(return_value={"id": 11, "provider": "email", "verified": False})
    monkeypatch.setattr(notifications.notification, "add_endpoint", add)
    payload = {
        "provider": "email",
        "address": "alice@example.test",
        "name": "Private email",
    }
    assert (
        await client.post("/api/v1/notifications/endpoints", json=payload)
    ).status_code == 422
    add.assert_not_awaited()
    response = await client.post(
        "/api/v1/notifications/endpoints",
        json=payload,
        headers={"X-CSRF-Token": generate_csrf_token()},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert add.await_args.kwargs["user"] is user
    assert add.await_args.args[0].address == "alice@example.test"
