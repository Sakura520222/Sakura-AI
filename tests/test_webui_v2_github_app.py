from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import github_app
from backend.api.v1.deps import require_api_auth
from backend.services.github_user_authorization_service import (
    GitHubUserAuthorization,
    GitHubUserInstallation,
)
from backend.webui.deps import get_db

pytestmark = pytest.mark.asyncio


async def test_authorization_api_preserves_user_scope_and_returns_only_safe_dto(monkeypatch):
    app = FastAPI()
    app.include_router(github_app.router, prefix="/api/v1")
    user = {"user_id": 7, "sub": "member", "role": "user"}
    app.dependency_overrides[require_api_auth] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    state = AsyncMock(return_value=(GitHubUserAuthorization(
        status="connected", github_username="member", installations=[
            GitHubUserInstallation(installation_id=9, account_login="member", account_type="User", repositories=[{"id": 2, "full_name": "member/repo"}])
        ]
    ), True, "https://github.com/apps/example/installations/new"))
    monkeypatch.setattr(github_app, "_authorization_state", state)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/github-app")
    assert response.status_code == 200
    state.assert_awaited_once_with(None, user)
    assert response.json()["repository_count"] == 1
    assert response.json()["authorization_url"].endswith("return_to=/app/github-app")
    assert "no-store" in response.headers["cache-control"]
    assert "access_token" not in response.text


async def test_authorization_failure_is_retryable_and_does_not_echo_exception(monkeypatch):
    app = FastAPI()
    app.include_router(github_app.router, prefix="/api/v1")
    app.dependency_overrides[require_api_auth] = lambda: {"user_id": 7, "sub": "member", "role": "user"}
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(github_app, "_authorization_state", AsyncMock(side_effect=RuntimeError("private diagnostic")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/github-app")
    assert response.status_code == 503
    assert "private diagnostic" not in response.text
    assert "no-store" in response.headers["cache-control"]
