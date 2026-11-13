"""Real credential lookup and HTTP boundaries for the user authorization center."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import github_app
from backend.api.v1.deps import require_api_auth
from backend.models.star_aid_models import StarAidCredential
from backend.services import github_user_authorization_service as service
from backend.services import star_aid_github_service as credentials
from backend.webui.deps import get_db


@pytest.mark.asyncio
async def test_two_users_discover_only_their_own_installations_and_repositories(
    monkeypatch,
):
    settings = SimpleNamespace(
        star_aid_github_app_client_id="app",
        star_aid_github_app_client_secret="secret",
        star_aid_github_app_callback_url="https://example.test/callback",
        star_aid_github_app_slug="sakura",
        star_aid_github_app_discovery_timeout_seconds=5,
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(
        credentials, "decrypt_secret", lambda value: value.removeprefix("encrypted-")
    )
    seen = []

    def github(request):
        username = request.headers["authorization"].removeprefix("Bearer token-")
        seen.append((username, request.url.path))
        identifier = 101 if username == "alice" else 202
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": identifier,
                            "account": {"login": username},
                            "target_type": "User",
                        }
                    ]
                },
            )
        assert request.url.path == f"/user/installations/{identifier}/repositories"
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "id": identifier,
                        "name": "private",
                        "full_name": f"{username}/private",
                        "private": True,
                        "html_url": f"https://github.com/{username}/private",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=httpx.MockTransport(github), **kwargs),
    )
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(StarAidCredential.__table__.create)
        async with sessions() as db:
            for user_id, username in ((1, "alice"), (2, "bob")):
                db.add(
                    StarAidCredential(
                        user_id=user_id,
                        github_username=username,
                        github_app_client_id="app",
                        encrypted_access_token=f"encrypted-token-{username}",
                    )
                )
            await db.commit()
        app = FastAPI()
        app.include_router(github_app.router, prefix="/api/v1")

        async def authenticated(request: Request):
            username = request.headers["test-user"]
            return {
                "user_id": 1 if username == "alice" else 2,
                "sub": username.upper(),
                "role": "user",
            }

        async def db_session():
            async with sessions() as db:
                yield db

        app.dependency_overrides[require_api_auth] = authenticated
        app.dependency_overrides[get_db] = db_session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.get(
                        "/api/v1/github-app?user_id=999&installation_id=999",
                        headers={"test-user": username},
                    )
                    for username in ("alice", "bob", "alice")
                )
            )
        for response, username, other in zip(
            responses, ("alice", "bob", "alice"), ("bob", "alice", "bob"), strict=True
        ):
            assert response.status_code == 200
            assert response.json()["authorization"]["status"] == "connected"
            assert f"{username}/private" in response.text
            assert other not in response.text.casefold()
            assert "token-" not in response.text
            assert "no-store" in response.headers["cache-control"]
        assert len(seen) == 6
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", ["alice", None, ""])
async def test_unmatched_identity_never_discovers_or_echoes_another_user(
    monkeypatch, expected
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            star_aid_github_app_client_id="app",
            star_aid_github_app_client_secret="secret",
            star_aid_github_app_callback_url="https://example.test/callback",
        ),
    )
    monkeypatch.setattr(
        credentials,
        "get_credential",
        AsyncMock(
            return_value=SimpleNamespace(
                github_username="bob", github_app_client_id="app", revoked_at=None
            )
        ),
    )
    token = AsyncMock(
        side_effect=AssertionError("Unmatched identity must not use a token")
    )
    monkeypatch.setattr(credentials, "get_effective_access_token", token)
    result = await service.GitHubUserAuthorizationService().get_installations(
        None, 1, expected_github_username=expected
    )
    assert result.status == "needs_authorization"
    assert result.github_username == (expected or "")
    assert result.installations == []
    token.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_community_reads_exclude_management_and_other_private_repositories(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from backend.api.v1 import community
    from backend.models.star_aid_models import StarAidMember, StarAidRepository
    from backend.services import star_aid_service

    monkeypatch.setattr(
        star_aid_service, "get_dynamic_config", AsyncMock(return_value=10)
    )
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            for model in (StarAidMember, StarAidCredential, StarAidRepository):
                await conn.run_sync(model.__table__.create)
        async with sessions() as db:
            db.add_all(
                [
                    StarAidMember(user_id=1, github_username="alice"),
                    StarAidMember(user_id=2, github_username="bob"),
                    StarAidRepository(
                        owner_user_id=1, full_name="alice/own", is_displayed=False
                    ),
                    StarAidRepository(
                        owner_user_id=2,
                        full_name="bob/private",
                        is_public=False,
                        is_displayed=False,
                    ),
                ]
            )
            await db.commit()
        app = FastAPI()
        app.include_router(community.router, prefix="/api/v1")
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 1,
            "sub": "alice",
            "role": "user",
        }

        async def db_session():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = db_session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            response = await client.get(
                "/api/v1/community?member_q=bob&status=all&member_page=1"
            )
        assert response.status_code == 200
        data = response.json()
        assert [repo["full_name"] for repo in data["available_repos"]] == ["alice/own"]
        assert all(
            data[key] is None
            for key in (
                "admin_members",
                "admin_member_page",
                "admin_repositories",
                "admin_repository_page",
            )
        )
        assert "bob" not in response.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_community_management_uses_server_role_guards(monkeypatch):
    from unittest.mock import AsyncMock

    from backend.api.v1 import community, deps
    from backend.webui.deps import generate_csrf_token

    actor = {"user_id": 1, "sub": "alice", "role": "user"}
    monkeypatch.setattr(
        deps, "require_api_auth", AsyncMock(side_effect=lambda request: actor)
    )
    app = FastAPI()
    app.include_router(community.router, prefix="/api/v1")
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        for entity, operation in (
            ("members", "ban"),
            ("members", "unban"),
            ("repositories", "disable"),
            ("repositories", "enable"),
        ):
            response = await client.post(
                f"/api/v1/community/admin/{entity}/2/{operation}",
                headers={"X-CSRF-Token": generate_csrf_token()},
                json={"reason": "test"},
            )
            assert response.status_code == 403
        for role in ("user", "admin"):
            actor["role"] = role
            response = await client.put(
                "/api/v1/community/feature",
                headers={"X-CSRF-Token": generate_csrf_token()},
                json={"enabled": True},
            )
            assert response.status_code == 403
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
