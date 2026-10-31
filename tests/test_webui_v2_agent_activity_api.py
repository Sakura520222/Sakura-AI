"""Versioned Agent/Activity APIs preserve resource ACLs and admission owners."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import activity, agent
from backend.api.v1.deps import require_api_auth
from backend.models.agent_team_models import (
    AgentTeamFeedback,
    AgentTeamIteration,
    AgentTeamPatchFile,
    AgentTeamTask,
)
from backend.services.activity_observability.access_service import ActivityNotFoundError
from backend.webui.deps import generate_csrf_token, get_db


@pytest_asyncio.fixture
async def api():
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(agent.router, prefix="/api/v1")
    app.include_router(activity.router, prefix="/api/v1")
    user = {"user_id": 7, "sub": "alice", "role": "user"}
    app.dependency_overrides[require_api_auth] = lambda: user

    async def db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    async with engine.begin() as connection:
        for model in (
            AgentTeamTask,
            AgentTeamIteration,
            AgentTeamPatchFile,
            AgentTeamFeedback,
        ):
            await connection.run_sync(model.__table__.create)
    async with sessions() as session:
        session.add_all(
            [
                AgentTeamTask(
                    id=1,
                    title="Own task",
                    repo_full_name="organization/ai",
                    repo_owner="organization",
                    repo_name="ai",
                    source_type="manual_issue",
                    status="failed",
                    started_by="ALICE",
                    ai_config_snapshot="secret-config",
                    workspace_path="private-path",
                ),
                AgentTeamTask(
                    id=2,
                    title="Another task",
                    repo_full_name="alice/ai",
                    repo_owner="alice",
                    repo_name="ai",
                    source_type="manual_issue",
                    status="failed",
                    started_by="bob",
                ),
            ]
        )
        await session.commit()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            yield app, client, user
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_lists_only_initiated_tasks_case_insensitively(api):
    _, client, user = api
    response = await client.get("/api/v1/agent/tasks")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["total"] == 1
    assert [task["id"] for task in data["items"]] == [1]
    assert "secret-config" not in response.text and "private-path" not in response.text
    detail = await client.get("/api/v1/agent/tasks/1")
    assert detail.status_code == 200
    assert detail.json()["data"]["iterations"] == []
    assert (await client.get("/api/v1/agent/tasks/2")).status_code == 404
    user["role"] = "admin"
    assert (await client.get("/api/v1/agent/tasks")).json()["data"]["total"] == 2
    assert (await client.get("/api/v1/agent/tasks/2")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix"),
    [("POST", "/retry"), ("POST", "/resume"), ("POST", "/cancel"), ("DELETE", "")],
)
async def test_task_mutations_retain_owner_guard_and_csrf(api, method, suffix):
    _, client, _ = api
    url = f"/api/v1/agent/tasks/2{suffix}"
    assert (await client.request(method, url)).status_code == 422
    denied = await client.request(
        method, url, headers={"X-CSRF-Token": generate_csrf_token()}
    )
    assert denied.status_code == 403
    assert denied.json()["success"] is False
    assert denied.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_json_create_preserves_admission_function_and_rejects_spoofed_identity(
    api, monkeypatch
):
    _, client, user = api
    create = AsyncMock(return_value=JSONResponse({"success": True, "task_id": 3}))
    monkeypatch.setattr(agent.agent, "create_task_from_issue", create)
    headers = {"X-CSRF-Token": generate_csrf_token()}
    payload = {
        "issue_ref": "organization/ai#638",
        "title": "Improve interface",
        "summary": "A task goal",
        "base_branch": "develop",
    }
    assert (await client.post("/api/v1/agent/tasks", json=payload)).status_code == 422
    create.assert_not_awaited()
    spoofed = await client.post(
        "/api/v1/agent/tasks", json={**payload, "started_by": "admin"}, headers=headers
    )
    assert spoofed.status_code == 422
    create.assert_not_awaited()
    response = await client.post("/api/v1/agent/tasks", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["task_id"] == 3
    args = create.await_args.kwargs
    assert args["user"] is user
    assert args["csrf_token"] == headers["X-CSRF-Token"]
    assert args["db"] is not None and args["background_tasks"] is not None
    assert args["source_id"] == "" and args["source_issue_number"] == ""
    assert args["summary"] == "A task goal" and args["base_branch"] == "develop"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_prompt_and_retry_use_same_atomic_handlers(api, monkeypatch):
    _, client, user = api
    retry = AsyncMock(
        return_value=JSONResponse(
            {"success": False, "message": "handoff pending"}, status_code=409
        )
    )
    prompt = AsyncMock(return_value=JSONResponse({"success": True, "prompt_id": 9}))
    monkeypatch.setattr(agent.agent, "retry_task", retry)
    monkeypatch.setattr(agent.agent, "submit_user_prompt", prompt)
    headers = {"X-CSRF-Token": generate_csrf_token()}
    pending = await client.post("/api/v1/agent/tasks/1/retry", headers=headers)
    assert pending.status_code == 409
    assert pending.json()["message"] == "handoff pending"
    assert retry.await_args.args[0] == 1
    assert retry.await_args.kwargs["user"] is user
    assert (
        await client.post("/api/v1/agent/tasks/1/prompts", json={"content": "Continue"})
    ).status_code == 422
    prompt.assert_not_awaited()
    response = await client.post(
        "/api/v1/agent/tasks/1/prompts", json={"content": "Continue"}, headers=headers
    )
    assert response.status_code == 200
    assert prompt.await_args.kwargs["content"] == "Continue"
    assert prompt.await_args.kwargs["user"] is user


@pytest.mark.asyncio
async def test_activity_uses_existing_acl_cursor_and_private_cache_contract(api):
    app, client, user = api
    service = AsyncMock()
    service.list_sessions.return_value = {"sessions": [{"id": 11}]}
    service.create_snapshot.side_effect = ActivityNotFoundError("not found")
    service.list_events_after.return_value = {"events": [], "cursor": "next-cursor"}
    app.state.activity_access_service = service
    response = await client.get("/api/v1/activity/sessions?limit=25")
    assert response.status_code == 200
    assert response.json() == {"sessions": [{"id": 11}]}
    assert response.headers["cache-control"] == "no-store"
    assert service.list_sessions.await_args.args[0] is user
    assert service.list_sessions.await_args.kwargs["limit"] == 25
    assert (
        await client.get("/api/v1/activity/sessions/12/snapshot")
    ).status_code == 404
    events = await client.get(
        "/api/v1/activity/sessions/11/events?cursor=opaque-cursor"
    )
    assert events.json()["cursor"] == "next-cursor"
    assert service.list_events_after.await_args.args == (11, user)
    assert service.list_events_after.await_args.kwargs["cursor"] == "opaque-cursor"
    assert (
        await client.get("/api/v1/activity/sessions?limit=1000000")
    ).status_code == 422


@pytest.mark.asyncio
async def test_activity_diagnostics_retain_super_admin_guard(api):
    app, client, _ = api
    service = AsyncMock()
    app.state.activity_access_service = service
    response = await client.get("/api/v1/activity/sessions/11/logs")
    assert response.status_code == 404
    service.require_session_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_api_routes_cannot_skip_authentication(api):
    app, client, _ = api
    app.dependency_overrides.pop(require_api_auth)
    for url in ("/api/v1/agent/tasks", "/api/v1/activity/sessions"):
        response = await client.get(url)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Cookie, Authorization"


@pytest.mark.asyncio
async def test_private_route_keeps_dependency_and_unavailable_errors_uncached(api):
    (
        _,
        client,
        _,
    ) = api
    denied = await client.post(
        "/api/v1/agent/tasks/1/cancel", headers={"X-CSRF-Token": "invalid"}
    )
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    unavailable = await client.get("/api/v1/activity/sessions")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"
