"""Community API adapters preserve existing authorization and service semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import community, console_data
from backend.webui.deps import generate_csrf_token, get_db
from backend.webui.i18n import i18n

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api():
    actor = {"user_id": 7, "sub": "alice", "role": "user"}
    db = AsyncMock()
    app = FastAPI()
    app.include_router(community.router, prefix="/api/v1")
    app.dependency_overrides[community.require_api_auth] = lambda: actor

    async def require_admin():
        if actor["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="权限不足")
        return actor

    async def require_super_admin():
        if actor["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="权限不足")
        return actor

    app.dependency_overrides[community.require_api_admin] = require_admin
    app.dependency_overrides[community.require_api_super_admin] = require_super_admin
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client, actor, db


def csrf_headers():
    return {"X-CSRF-Token": generate_csrf_token()}


async def test_community_membership_uses_service_and_commits_once(api, monkeypatch):
    client, actor, db = api
    join = AsyncMock(return_value={"success": True})
    refresh_summaries = AsyncMock()
    monkeypatch.setattr(community.star_aid_service, "join_plan", join)
    monkeypatch.setattr(community, "_trigger_displayed_summaries", refresh_summaries)

    missing_csrf = await client.post(
        "/api/v1/community/membership/join",
        json={"repositories": ["alice/repo"]},
    )
    assert missing_csrf.status_code == 422
    join.assert_not_awaited()

    response = await client.post(
        "/api/v1/community/membership/join",
        headers=csrf_headers(),
        json={"repositories": ["alice/repo"]},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    join.assert_awaited_once_with(db, actor["user_id"], actor["sub"], ["alice/repo"])
    refresh_summaries.assert_awaited_once_with(db, actor["user_id"])
    db.commit.assert_awaited_once()


async def test_community_moderation_keeps_admin_guard_and_reason(api, monkeypatch):
    client, actor, db = api
    ban = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(community.star_aid_service, "ban_member", ban)
    path = "/api/v1/community/admin/members/42/ban"

    denied = await client.post(path, headers=csrf_headers(), json={"reason": "spam"})
    assert denied.status_code == 403
    ban.assert_not_awaited()

    actor["role"] = "admin"
    allowed = await client.post(path, headers=csrf_headers(), json={"reason": "spam"})
    assert allowed.status_code == 200
    assert allowed.json()["success"] is True
    ban.assert_awaited_once_with(db, actor["user_id"], 42, "spam")
    db.commit.assert_awaited_once()


async def test_community_manual_star_returns_spa_auth_redirect(api, monkeypatch):
    client, _, db = api
    monkeypatch.setattr(
        community.star_aid_service, "is_feature_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        community.star_aid_service,
        "perform_star",
        AsyncMock(return_value={"reauth_required": True}),
    )

    response = await client.post(
        "/api/v1/community/repositories/14/star", headers=csrf_headers()
    )

    assert response.status_code == 200
    assert response.json() == {
        "redirect": "/star-aid/auth/start?intent=manual_star&repo_id=14&return_to=/app/community"
    }
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("action", ["delete", "clear"])
@pytest.mark.parametrize("fails", [False, True])
async def test_storage_actions_keep_localized_feedback_and_metadata(
    monkeypatch, action, fails
):
    metadata = {"fixture": "retained"}
    store = SimpleNamespace(
        client=SimpleNamespace(
            delete_collection=Mock(
                side_effect=RuntimeError("isolated fixture failure") if fails else None
            ),
            get_collection=Mock(return_value=SimpleNamespace(metadata=metadata)),
            create_collection=Mock(),
        )
    )
    audit = AsyncMock()
    monkeypatch.setattr(console_data, "get_vector_store", lambda: store)
    monkeypatch.setattr(console_data, "log_admin_action", audit)
    kwargs = {
        "name": "fixture",
        "action": action,
        "request": None,
        "user": {"user_id": 7, "sub": "admin", "role": "super_admin"},
        "db": AsyncMock(),
        "csrf": "fixture-csrf",
    }
    if fails:
        with pytest.raises(HTTPException) as caught:
            await console_data.collection_action(**kwargs)
        assert caught.value.status_code == 400
        assert caught.value.detail == (
            "操作失败，请查看日志" if action == "delete" else "清空失败，请查看日志"
        )
        audit.assert_not_awaited()
    else:
        result = await console_data.collection_action(**kwargs)
        assert result == {
            "success": True,
            "message": "Collection 已删除"
            if action == "delete"
            else "Collection 已清空",
        }
        audit.assert_awaited_once()
        if action == "clear":
            store.client.create_collection.assert_called_once_with(
                name="fixture", metadata=metadata
            )
    store.client.delete_collection.assert_called_once_with(name="fixture")


@pytest.mark.parametrize("operation", ["ban", "unban"])
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_moderation_failure_feedback_remains_localized(
    api, monkeypatch, operation, language
):
    client, actor, db = api
    actor["role"] = "admin"
    monkeypatch.setattr(community, "detect_language", lambda: language)
    monkeypatch.setattr(
        community.star_aid_service,
        operation + "_member",
        AsyncMock(return_value={"success": False}),
    )
    response = await client.post(
        f"/api/v1/community/admin/members/42/{operation}",
        headers=csrf_headers(),
        json={"reason": "fixture"},
    )
    i18n.reload()
    key = f"star_aid.{operation}_failed"
    assert response.status_code == 400
    assert response.json()["detail"] == i18n.t(key, lang=language)
    assert response.json()["detail"] != key
    db.commit.assert_awaited_once()
