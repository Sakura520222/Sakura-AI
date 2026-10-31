"""The React sign-in boundary keeps cookies and MFA authorities server-side."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import auth_webui
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME, create_mfa_pending_token
from backend.webui.deps import generate_csrf_token, get_db
from backend.webui.routes import spa

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def browser_api():
    app = FastAPI()
    app.include_router(auth_webui.router, prefix="/api/v1")
    app.include_router(spa.router)
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield app, client, db


async def test_anonymous_state_contains_capabilities_without_credentials(browser_api):
    _, client, _ = browser_api
    response = await client.get("/api/v1/webui/auth/state")
    assert response.status_code == 200
    body = response.json()
    assert not body["authenticated"] and not body["mfa_pending"]
    assert body["csrf"]
    assert "access_token" not in body and "client_secret" not in body
    assert response.headers["cache-control"] == "no-store"


async def test_state_checks_live_account_and_retains_required_enrollment(
    browser_api, monkeypatch
):
    _, client, _ = browser_api
    user = {"user_id": 17, "sub": "alice", "role": "user"}
    actor = AsyncMock(return_value=user)
    enrollment = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_webui, "get_api_current_user", actor)
    monkeypatch.setattr(auth_webui, "user_requires_mfa_enrollment", enrollment)
    client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "full-session")
    response = await client.get("/api/v1/webui/auth/state")
    assert response.json()["authenticated"]
    assert response.json()["enrollment_required"]
    actor.assert_awaited_once()
    enrollment.assert_awaited_once()
    monkeypatch.setattr(
        auth_webui, "get_api_current_user", AsyncMock(side_effect=HTTPException(401))
    )
    response = await client.get("/api/v1/webui/auth/state")
    assert not response.json()["authenticated"]


async def test_github_entry_binds_new_ui_target_to_server_side_state(
    browser_api, monkeypatch
):
    _, client, _ = browser_api
    saved = AsyncMock()
    monkeypatch.setattr(auth_webui.legacy, "_save_oauth_state", saved)
    monkeypatch.setattr(
        auth_webui,
        "get_settings",
        lambda: SimpleNamespace(
            github_oauth_client_id="public-client-id",
            github_oauth_redirect_uri="https://testserver/auth/callback",
            github_oauth_auth_url="https://github.com/login/oauth/authorize",
        ),
    )
    response = await client.get("/api/v1/webui/auth/github", follow_redirects=False)
    assert response.status_code in {302, 307}
    destination = urlparse(response.headers["location"])
    params = parse_qs(destination.query)
    assert destination.netloc == "github.com"
    assert params["redirect_uri"] == ["https://testserver/auth/callback"]
    saved.assert_awaited_once_with(params["state"][0], "/app/")
    assert len(params["state"][0]) >= 32


async def test_totp_requires_pending_cookie_and_csrf_before_verifying(browser_api, monkeypatch):
    _, client, _ = browser_api
    verify = AsyncMock()
    monkeypatch.setattr(auth_webui.token_auth, "verify_two_factor", verify)
    response = await client.post("/api/v1/webui/auth/mfa/totp", json={"code": "123456"})
    assert response.status_code == 422
    response = await client.post(
        "/api/v1/webui/auth/mfa/totp",
        json={"code": "123456"},
        headers={"X-CSRF-Token": generate_csrf_token()},
    )
    assert response.status_code == 401
    verify.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 400, 429])
async def test_totp_delegates_to_existing_authority_without_exposing_issued_token(
    browser_api, monkeypatch, status
):
    _, client, _ = browser_api
    pending = create_mfa_pending_token({"user_id": 17, "sub": "alice", "role": "user"})
    client.cookies.set(auth_webui.legacy.MFA_PENDING_COOKIE_NAME, pending)
    result = JSONResponse(
        {"success": status == 200, "data": {"access_token": "issued-server-only-token"} if status == 200 else None},
        status_code=status,
    )
    verify = AsyncMock(return_value=result)
    monkeypatch.setattr(auth_webui.token_auth, "verify_two_factor", verify)
    response = await client.post(
        "/api/v1/webui/auth/mfa/totp",
        json={"code": "RECOVERY-CODE"},
        headers={"X-CSRF-Token": generate_csrf_token()},
    )
    assert response.status_code == status
    body = verify.await_args.args[1]
    assert body.code == "RECOVERY-CODE" and body.mfa_token == pending
    assert "issued-server-only-token" not in response.text
    cookies = response.headers.get_list("set-cookie")
    if status == 200:
        assert response.json()["data"]["redirect"] == "/app/"
        assert any("issued-server-only-token" in cookie and "Secure" in cookie and "HttpOnly" in cookie for cookie in cookies)
        assert any(auth_webui.legacy.MFA_PENDING_COOKIE_NAME in cookie and "Max-Age=0" in cookie for cookie in cookies)
    else:
        assert not cookies


@pytest.mark.parametrize("pending", [False, True])
async def test_passkey_boundary_keeps_native_challenge_and_cookie_results(
    browser_api, monkeypatch, pending
):
    _, client, _ = browser_api
    expected = JSONResponse({"success": True, "data": {"redirect": "/"}})
    expected.set_cookie(WEBUI_TOKEN_COOKIE_NAME, "server-credential", httponly=True, secure=True)
    target = "two_factor_passkey_verify" if pending else "passkey_verify_discover"
    verify = AsyncMock(return_value=expected)
    monkeypatch.setattr(auth_webui.legacy, target, verify)
    body = {"challenge_id": "test-challenge", "credential": {"id": "credential-id", "response": {"signature": "opaque-bytes"}}}
    response = await client.post(
        f"/api/v1/webui/auth/{'mfa/' if pending else ''}passkeys/verify",
        json=body,
        headers={"X-CSRF-Token": generate_csrf_token()},
    )
    assert response.status_code == 200
    assert verify.await_args.kwargs["body"] == body
    assert "server-credential" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]


async def test_spa_login_is_public_but_only_enrollment_may_bypass_mfa(
    browser_api, monkeypatch, tmp_path
):
    _, client, _ = browser_api
    monkeypatch.setattr(spa, "WEB_DIST", tmp_path)
    (tmp_path / "index.html").write_text("React fixture", encoding="utf-8")
    full_user = AsyncMock(return_value={"user_id": 17})
    guarded = AsyncMock(side_effect=HTTPException(428))
    monkeypatch.setattr(spa, "get_current_user", full_user)
    monkeypatch.setattr(spa, "require_auth", guarded)
    assert (await client.get("/app/login")).status_code == 200
    full_user.assert_not_awaited()
    guarded.assert_not_awaited()
    assert (await client.get("/app/security")).status_code == 200
    full_user.assert_awaited_once()
    response = await client.get("/app/settings/ai", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/app/security"
    guarded.assert_awaited_once()
