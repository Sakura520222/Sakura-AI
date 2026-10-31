"""React shell admission, bootstrap and compiled-asset boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.v1 import repos, webui
from backend.api.v1.deps import require_api_admin, require_api_auth
from backend.webui.auth import (
    WEBUI_TOKEN_COOKIE_NAME,
    WEBUI_TOKEN_RENEWAL_STATE_KEY,
    WebUITokenRenewalMiddleware,
)
from backend.webui.deps import generate_csrf_token, get_db, require_auth
from backend.webui.routes import spa


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(webui.router, prefix="/api/v1")
    app.include_router(repos.router, prefix="/api/v1")
    app.include_router(spa.router)
    app.add_middleware(WebUITokenRenewalMiddleware)
    monkeypatch.setattr(spa, "WEB_DIST", tmp_path / "dist")
    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url="https://testserver")


def authorize(app, role="user", config=None):
    user = {
        "user_id": 7,
        "sub": "sakura",
        "role": role,
        "avatar_url": None,
        "arbitrary_secret": "must-not-be-returned",
    }
    app.dependency_overrides[require_api_auth] = lambda: user
    app.dependency_overrides[require_api_admin] = lambda: user
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[spa.require_app_session] = lambda: user
    db = AsyncMock()
    result = Mock()
    result.scalar_one_or_none.return_value = config
    db.execute.return_value = result
    app.dependency_overrides[get_db] = lambda: db
    return db


def test_bootstrap_and_index_require_authentication(client):
    assert client.get("/api/v1/webui/bootstrap").status_code == 401
    for url in ("/app", "/app/settings/ai", "/app/security"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/app/login"


@pytest.mark.parametrize(
    ("role", "admin", "super_admin"),
    [("user", False, False), ("admin", True, False), ("super_admin", True, True)],
)
def test_bootstrap_is_typed_safe_and_role_scoped(app, client, role, admin, super_admin):
    authorize(app, role)
    response = client.get("/api/v1/webui/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {
        "id": 7,
        "username": "sakura",
        "display_name": "sakura",
        "role": role,
        "avatar_url": None,
    }
    assert body["permissions"] == {
        "admin": admin,
        "super_admin": super_admin,
        "manage_settings": super_admin,
    }
    assert body["preferences"] == {
        "theme": "system",
        "language": "zh-CN",
        "items_per_page": 20,
    }
    assert "must-not-be-returned" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert body["csrf"]
    schema = app.openapi()
    assert schema["paths"]["/api/v1/webui/bootstrap"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/WebUIBootstrap"}


def test_bootstrap_reads_saved_preferences_and_safely_recovers_invalid_values(
    app, client
):
    authorize(
        app, config=SimpleNamespace(theme="dark", language="en", items_per_page=50)
    )
    assert client.get("/api/v1/webui/bootstrap").json()["preferences"] == {
        "theme": "dark",
        "language": "en",
        "items_per_page": 50,
    }
    authorize(
        app, config=SimpleNamespace(theme="invalid", language=None, items_per_page=0)
    )
    assert client.get("/api/v1/webui/bootstrap").json()["preferences"] == {
        "theme": "system",
        "language": "zh-CN",
        "items_per_page": 20,
    }


def test_bootstrap_uses_frozen_application_timezone(app, client, monkeypatch):
    authorize(app)
    monkeypatch.setattr(webui, "get_time_service", lambda: SimpleNamespace(resolved_timezone="Pacific/Kiritimati"))
    assert client.get("/api/v1/webui/bootstrap").json()["timezone"] == "Pacific/Kiritimati"


def test_bootstrap_retains_mfa_enrollment_guard(app, client, monkeypatch):
    from backend.api.v1 import deps

    monkeypatch.setattr(
        deps, "get_api_current_user", AsyncMock(return_value={"user_id": 7})
    )
    session_factory = Mock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(deps.db_module, "async_session", session_factory)
    monkeypatch.setattr(
        deps, "user_requires_mfa_enrollment", AsyncMock(return_value=True)
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    assert client.get("/api/v1/webui/bootstrap").status_code == 428


def test_logout_requires_csrf_and_clears_expired_session_without_renewal(app, client):
    @app.middleware("http")
    async def queue_old_renewal(request, call_next):
        setattr(request.state, WEBUI_TOKEN_RENEWAL_STATE_KEY, "queued-old-token")
        return await call_next(request)

    client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "expired-token")
    assert client.post("/api/v1/webui/logout").status_code == 422
    assert (
        client.post("/api/v1/webui/logout", headers={"X-CSRF-Token": "bad"}).status_code
        == 403
    )
    response = client.post(
        "/api/v1/webui/logout", headers={"X-CSRF-Token": generate_csrf_token()}
    )
    assert response.status_code == 204
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("Max-Age=0" in value for value in cookies)
    assert all("HttpOnly" in value and "Secure" in value for value in cookies)
    assert "queued-old-token" not in " ".join(cookies)


def test_spa_deep_links_assets_and_missing_build(app, client):
    authorize(app)
    response = client.get("/app/settings/ai")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    spa.WEB_DIST.mkdir()
    (spa.WEB_DIST / "index.html").write_text(
        "<main>React shell</main>", encoding="utf-8"
    )
    (spa.WEB_DIST / "assets").mkdir()
    (spa.WEB_DIST / "assets" / "app-123.js").write_text("export {};", encoding="utf-8")
    response = client.get("/app/settings/ai")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "React shell" in response.text
    assert client.get("/app/repositories/owner/repo.name").status_code == 200
    assert client.get("/app/assets/missing.js").status_code == 404
    assert client.get("/api/v1/not-a-route").status_code == 404
    response = client.get("/app/assets/app-123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"].endswith("immutable")


@pytest.mark.parametrize(
    "path", ["..%2findex.html", "..%5cindex.html", ".hidden", "%2fetc%2fpasswd"]
)
def test_assets_cannot_escape_build_assets(app, client, path):
    spa.WEB_DIST.mkdir()
    (spa.WEB_DIST / "index.html").write_text("private boundary", encoding="utf-8")
    assert client.get(f"/app/assets/{path}").status_code == 404


def test_asset_symlinks_cannot_escape_build(app, client, tmp_path):
    assets = spa.WEB_DIST / "assets"
    assets.mkdir(parents=True)
    outside = tmp_path / "outside.js"
    outside.write_text("private", encoding="utf-8")
    try:
        (assets / "escape.js").symlink_to(outside)
    except OSError:
        pytest.skip("Creating a symbolic link requires platform privileges")
    assert client.get("/app/assets/escape.js").status_code == 404


def test_repositories_serialize_aware_activity_times(app, client, monkeypatch):
    authorize(app, "admin")
    monkeypatch.setattr(
        repos,
        "_get_installations_with_stats",
        AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "repositories": [
                        {
                            "full_name": "sakura/ai",
                            "last_activity": datetime(2026, 10, 29, tzinfo=UTC),
                        }
                    ],
                }
            ]
        ),
    )
    response = client.get("/api/v1/repos")
    assert response.status_code == 200
    assert (
        response.json()["data"][0]["repositories"][0]["last_activity"]
        == "2026-10-29T00:00:00+00:00"
    )
