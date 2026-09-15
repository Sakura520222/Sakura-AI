"""Sliding renewal coverage for WebUI cookie sessions."""

from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.responses import RedirectResponse
from starlette.testclient import TestClient

from backend.api.v1 import deps as api_deps
from backend.webui import auth, deps


class _Request:
    def __init__(
        self,
        *,
        cookie_token: str | None = None,
        authorization: str | None = None,
        query_token: str | None = None,
    ):
        self.cookies = (
            {auth.WEBUI_TOKEN_COOKIE_NAME: cookie_token} if cookie_token else {}
        )
        self.headers = {"authorization": authorization} if authorization else {}
        self.query_params = {"token": query_token} if query_token else {}
        self.state = SimpleNamespace()


class _Result:
    def __init__(self, user):
        self.user = user

    def scalar_one_or_none(self):
        return self.user


class _Session:
    def __init__(self, user):
        self.user = user
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.exited += 1
        return False

    async def execute(self, _statement):
        return _Result(self.user)


def _db_user(**overrides):
    values = {
        "id": 7,
        "role": "admin",
        "email": "fresh@example.com",
        "email_verified": True,
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _token_payload(**overrides) -> dict:
    payload = {
        "sub": "octocat",
        "role": "user",
        "user_id": 7,
        "github_id": 42,
        "avatar_url": "https://example.com/avatar.png",
        "email": "stale@example.com",
        "email_verified": False,
    }
    payload.update(overrides)
    return payload


def _set_cookie_values(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _cookie_morsel(set_cookie: str):
    cookies = SimpleCookie()
    cookies.load(set_cookie)
    return cookies[auth.WEBUI_TOKEN_COOKIE_NAME]


def _cookie_token(response) -> str | None:
    values = _set_cookie_values(response)
    if not values:
        return None
    morsel = _cookie_morsel(values[0])
    return morsel.value


def _get_with_cookie(client, path: str, token: str):
    client.cookies.set(auth.WEBUI_TOKEN_COOKIE_NAME, token)
    return client.get(path, follow_redirects=False)


def _assert_webui_cookie(set_cookie: str) -> None:
    morsel = _cookie_morsel(set_cookie)
    assert morsel["max-age"] == str(auth.WEBUI_TOKEN_COOKIE_MAX_AGE)
    assert morsel["secure"]
    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "lax"


def _install_webui_db(monkeypatch, user):
    session = _Session(user)
    monkeypatch.setattr(deps.db_module, "async_session", lambda: session)

    async def skip_mfa_enforcement(_request, _user, _db):
        return None

    monkeypatch.setattr(deps, "enforce_mfa_enrollment", skip_mfa_enforcement)
    return session


def _build_webui_app(monkeypatch, user):
    _install_webui_db(monkeypatch, user)
    app = FastAPI()
    app.add_middleware(auth.WebUITokenRenewalMiddleware)

    @app.get("/redirect")
    async def redirect_route(user: dict = Depends(deps.require_auth)):
        return RedirectResponse("/dashboard", status_code=302)

    @app.get("/explicit")
    async def explicit_response_route(user: dict = Depends(deps.require_auth)):
        return Response(content="ok")

    @app.get("/dict")
    async def dict_route(user: dict = Depends(deps.require_auth)):
        return {"ok": True}

    @app.get("/existing-cookie")
    async def existing_cookie_route(user: dict = Depends(deps.require_auth)):
        response = RedirectResponse("/dashboard", status_code=302)
        auth.set_webui_token_cookie(response, "logout-token")
        return response

    return app


def test_session_lifetime_constants():
    assert auth.ACCESS_TOKEN_EXPIRE_DAYS == 7
    assert auth.ACCESS_TOKEN_EXPIRE_HOURS == 168
    assert auth.WEBUI_TOKEN_COOKIE_MAX_AGE == 604800


@pytest.mark.parametrize("path", ["/redirect", "/explicit", "/dict"])
def test_middleware_renews_all_response_kinds(monkeypatch, path):
    app = _build_webui_app(monkeypatch, _db_user())
    token = auth.create_access_token(_token_payload(), expires_delta=timedelta(hours=1))

    with TestClient(app) as client:
        response = _get_with_cookie(client, path, token)

    assert response.status_code in (200, 302)
    set_cookie_values = _set_cookie_values(response)
    assert len(set_cookie_values) == 1
    _assert_webui_cookie(set_cookie_values[0])

    renewed_token = _cookie_token(response)
    assert renewed_token is not None
    assert renewed_token != token
    renewed_payload = auth.decode_access_token(renewed_token)
    assert renewed_payload is not None
    assert renewed_payload["role"] == "admin"
    assert renewed_payload["email"] == "fresh@example.com"
    assert renewed_payload["email_verified"] is True
    assert renewed_payload["sub"] == "octocat"
    remaining = renewed_payload["exp"] - auth.now_utc().timestamp()
    assert abs(remaining - auth.WEBUI_TOKEN_COOKIE_MAX_AGE) <= 10


def test_middleware_does_not_duplicate_existing_webui_cookie(monkeypatch):
    app = _build_webui_app(monkeypatch, _db_user())
    token = auth.create_access_token(_token_payload())

    with TestClient(app) as client:
        response = _get_with_cookie(client, "/existing-cookie", token)

    set_cookie_values = _set_cookie_values(response)
    assert len(set_cookie_values) == 1
    assert _cookie_morsel(set_cookie_values[0]).value == "logout-token"


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: auth.create_access_token(
            _token_payload(), expires_delta=timedelta(seconds=-1)
        ),
        lambda: auth.create_mfa_pending_token({"user_id": 7}),
        lambda: auth.create_access_token({"sub": "missing-user"}),
    ],
)
def test_invalid_or_incomplete_sessions_do_not_renew(monkeypatch, token_factory):
    app = _build_webui_app(monkeypatch, _db_user())

    with TestClient(app) as client:
        response = _get_with_cookie(client, "/redirect", token_factory())

    assert response.status_code == 401
    assert _set_cookie_values(response) == []


@pytest.mark.asyncio
async def test_refresh_login_claims_uses_active_database_user(monkeypatch):
    session = _Session(_db_user())
    monkeypatch.setattr(deps.db_module, "async_session", lambda: session)

    claims = await deps.refresh_login_claims(_token_payload())

    assert claims == {
        "sub": "octocat",
        "role": "admin",
        "user_id": 7,
        "github_id": 42,
        "avatar_url": "https://example.com/avatar.png",
        "email": "fresh@example.com",
        "email_verified": True,
    }
    assert session.entered == 1
    assert session.exited == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [None, _db_user(is_active=False)],
)
async def test_refresh_login_claims_rejects_missing_or_inactive_user(monkeypatch, user):
    session = _Session(user)
    monkeypatch.setattr(deps.db_module, "async_session", lambda: session)

    assert await deps.refresh_login_claims(_token_payload()) is None


@pytest.mark.asyncio
async def test_refresh_login_claims_rejects_invalid_user_id(monkeypatch):
    def unexpected_session():
        pytest.fail("invalid user_id must not open a database session")

    monkeypatch.setattr(deps.db_module, "async_session", unexpected_session)

    assert await deps.refresh_login_claims(_token_payload(user_id="not-an-int")) is None


@pytest.mark.asyncio
async def test_api_cookie_mode_refreshes_authoritative_claims_and_queues_renewal(
    monkeypatch,
):
    token = auth.create_access_token(_token_payload())
    refreshed_claims = _token_payload(
        role="super_admin",
        email="authoritative@example.com",
        email_verified=True,
    )

    async def refresh_claims(payload):
        assert payload["role"] == "user"
        return refreshed_claims

    monkeypatch.setattr(api_deps, "refresh_login_claims", refresh_claims)
    request = _Request(cookie_token=token)

    user = await api_deps.get_api_current_user(request)

    assert user["role"] == "super_admin"
    assert user["email"] == "authoritative@example.com"
    renewed_token = getattr(request.state, auth.WEBUI_TOKEN_RENEWAL_STATE_KEY)
    renewed_payload = auth.decode_access_token(renewed_token)
    assert renewed_payload is not None
    assert renewed_payload["role"] == "super_admin"
    assert renewed_payload["email"] == "authoritative@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_factory",
    [
        lambda token: _Request(authorization=f"Bearer {token}"),
        lambda token: _Request(query_token=token),
    ],
)
async def test_api_non_cookie_modes_keep_stateless_claims(monkeypatch, request_factory):
    token = auth.create_access_token(_token_payload(role="admin"))

    async def unexpected_refresh(_payload):
        pytest.fail("non-cookie API clients must not refresh database claims")

    monkeypatch.setattr(api_deps, "refresh_login_claims", unexpected_refresh)
    request = request_factory(token)

    user = await api_deps.get_api_current_user(request)

    assert user["role"] == "admin"
    assert not hasattr(request.state, auth.WEBUI_TOKEN_RENEWAL_STATE_KEY)
