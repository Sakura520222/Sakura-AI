"""Sliding renewal coverage for WebUI cookie sessions."""

from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException, Response

from backend.api.v1.deps import get_api_current_user
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


def _cookie_token(response: Response) -> str | None:
    set_cookie = response.headers.get("set-cookie")
    if not set_cookie:
        return None
    cookies = SimpleCookie()
    cookies.load(set_cookie)
    morsel = cookies.get(auth.WEBUI_TOKEN_COOKIE_NAME)
    return morsel.value if morsel else None


def _assert_cookie_max_age(response: Response) -> None:
    set_cookie = response.headers.get("set-cookie")
    assert set_cookie is not None
    cookies = SimpleCookie()
    cookies.load(set_cookie)
    morsel = cookies[auth.WEBUI_TOKEN_COOKIE_NAME]
    assert morsel["max-age"] == str(auth.WEBUI_TOKEN_COOKIE_MAX_AGE)


def _token_payload() -> dict:
    return {
        "sub": "octocat",
        "role": "admin",
        "user_id": 7,
        "github_id": 42,
        "avatar_url": "https://example.com/avatar.png",
        "email": "octocat@example.com",
        "email_verified": True,
        "custom_claim": "preserved",
    }


def test_session_lifetime_constants():
    assert auth.ACCESS_TOKEN_EXPIRE_DAYS == 7
    assert auth.ACCESS_TOKEN_EXPIRE_HOURS == 168
    assert auth.WEBUI_TOKEN_COOKIE_MAX_AGE == 604800


@pytest.mark.asyncio
async def test_get_current_user_renews_valid_cookie_session():
    payload = _token_payload()
    token = auth.create_access_token(payload, expires_delta=timedelta(hours=1))
    original_payload = auth.decode_access_token(token)
    response = Response()

    user = await deps.get_current_user(_Request(cookie_token=token), response)

    assert user == {
        "sub": payload["sub"],
        "role": payload["role"],
        "user_id": payload["user_id"],
        "github_id": payload["github_id"],
        "avatar_url": payload["avatar_url"],
        "email": payload["email"],
        "email_verified": payload["email_verified"],
    }
    _assert_cookie_max_age(response)

    renewed_token = _cookie_token(response)
    assert renewed_token is not None
    renewed_payload = auth.decode_access_token(renewed_token)
    assert renewed_payload is not None
    assert original_payload is not None
    assert renewed_payload["token_type"] == auth.TOKEN_TYPE_ACCESS
    for key, value in original_payload.items():
        if key not in {"exp", "token_type"}:
            assert renewed_payload[key] == value

    remaining = renewed_payload["exp"] - auth.now_utc().timestamp()
    assert abs(remaining - auth.WEBUI_TOKEN_COOKIE_MAX_AGE) <= 10


@pytest.mark.asyncio
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
async def test_invalid_or_incomplete_sessions_do_not_renew(token_factory):
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        await deps.get_current_user(
            _Request(cookie_token=token_factory()),
            response,
        )

    assert exc_info.value.status_code == 401
    assert _cookie_token(response) is None


@pytest.mark.asyncio
async def test_api_cookie_session_renews():
    token = auth.create_access_token(_token_payload())
    response = Response()

    user = await get_api_current_user(_Request(cookie_token=token), response)

    assert user["user_id"] == 7
    _assert_cookie_max_age(response)
    assert _cookie_token(response) is not None


@pytest.mark.asyncio
async def test_api_bearer_session_does_not_renew():
    token = auth.create_access_token(_token_payload())
    response = Response()

    user = await get_api_current_user(
        _Request(authorization=f"Bearer {token}"),
        response,
    )

    assert user["user_id"] == 7
    assert _cookie_token(response) is None


@pytest.mark.asyncio
async def test_api_query_token_does_not_renew():
    token = auth.create_access_token(_token_payload())
    response = Response()

    user = await get_api_current_user(_Request(query_token=token), response)

    assert user["user_id"] == 7
    assert _cookie_token(response) is None
