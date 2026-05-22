"""Mobile GitHub OAuth authorization coverage."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from backend.api.v1 import auth
from backend.core.config import Settings


def _github_mobile_authorize_endpoint():
    return getattr(auth.github_mobile_authorize, "__wrapped__", auth.github_mobile_authorize)


@pytest.mark.anyio
async def test_github_mobile_authorize_accepts_configured_custom_redirect(monkeypatch):
    settings = Settings(
        github_oauth_client_id="client-id",
        github_oauth_redirect_uri="https://example.com/auth/callback",
        mobile_oauth_allowed_redirect_uris="myapp://oauth/callback, otherapp://callback",
    )
    saved = {}

    async def fake_save_oauth_state(state: str, uri: str):
        saved["state"] = state
        saved["uri"] = uri

    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_save_oauth_state", fake_save_oauth_state)

    response = await _github_mobile_authorize_endpoint()(
        None,
        redirect_uri="myapp://oauth/callback",
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["success"] is True
    assert saved["uri"] == "myapp://oauth/callback"

    authorization_url = body["data"]["authorization_url"]
    query = parse_qs(urlparse(authorization_url).query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["myapp://oauth/callback"]
    assert query["state"] == [saved["state"]]


@pytest.mark.anyio
async def test_github_mobile_authorize_rejects_unconfigured_custom_redirect(monkeypatch):
    settings = Settings(
        github_oauth_client_id="client-id",
        github_oauth_redirect_uri="https://example.com/auth/callback",
        mobile_oauth_allowed_redirect_uris="otherapp://callback",
    )

    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    response = await _github_mobile_authorize_endpoint()(
        None,
        redirect_uri="myapp://oauth/callback",
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["success"] is False
    assert body["error"] == "不支持的回调地址"
