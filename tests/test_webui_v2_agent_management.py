"""Skills and workspace APIs retain file admission, roles and active-task guards."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import agent_management as management
from backend.api.v1 import deps
from backend.models.admin_action_log import AdminActionLog
from backend.models.agent_skill_models import AgentSkill
from backend.models.agent_team_models import AgentTeamTask
from backend.models.database import Base
from backend.models.telegram_models import TelegramUser
from backend.services.agent_team.skill_service import MAX_SKILL_BYTES, AgentSkillService
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.deps import generate_csrf_token, get_db

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[
            TelegramUser.__table__, AgentSkill.__table__, AgentTeamTask.__table__, AdminActionLog.__table__,
        ]))
    async with sessions() as db:
        db.add(TelegramUser(id=1, github_username="admin", role="super_admin"))
        await db.commit()
    actor = {"user_id": 1, "sub": "admin", "role": "super_admin"}
    monkeypatch.setattr(deps, "require_api_auth", AsyncMock(side_effect=lambda _: actor))
    skill_service = AgentSkillService(tmp_path / "skills")
    workspace_service = AgentTeamWorkspaceService(tmp_path / "workspaces")
    monkeypatch.setattr(management, "AgentSkillService", lambda: skill_service)
    monkeypatch.setattr(management, "AgentTeamWorkspaceService", lambda: workspace_service)
    monkeypatch.setattr(management.team, "AgentTeamWorkspaceService", lambda: workspace_service)
    app = FastAPI()
    app.include_router(management.router, prefix="/api/v1")

    async def db_dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client, actor, sessions, skill_service, workspace_service, tmp_path
    await engine.dispose()


def csrf():
    return {"X-CSRF-Token": generate_csrf_token()}


async def test_upload_list_manifest_toggle_and_delete_use_actual_skill_service(api):
    client, _, sessions, service, _, _ = api
    response = await client.post(
        "/api/v1/agent/skills/upload", headers=csrf(),
        files={"skill_file": ("SKILL.md", b"---\nname: Test skill\ndescription: Fixture only\n---\n# Test skill\nInspect a test fixture.")},
    )
    assert response.status_code == 200
    skill_id = response.json()["data"]["id"]
    root = await service.resolve_root()
    assert (root / "test-skill" / "SKILL.md").is_file()
    listed = await client.get("/api/v1/agent/skills?search=Fixture")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert listed.json()["items"][0]["enabled"] is True
    assert listed.json()["markdown_limit"] == MAX_SKILL_BYTES
    files = await client.get(f"/api/v1/agent/skills/{skill_id}/files")
    assert files.json() == {"files": ["SKILL.md"]}
    updated = await client.patch(f"/api/v1/agent/skills/{skill_id}", json={"enabled": False}, headers=csrf())
    assert updated.status_code == 200
    assert not (await client.get("/api/v1/agent/skills?status=enabled")).json()["items"]
    assert (await client.get("/api/v1/agent/skills?status=disabled")).json()["items"]
    removed = await client.delete(f"/api/v1/agent/skills/{skill_id}", headers=csrf())
    assert removed.status_code == 200 and not (root / "test-skill").exists()
    async with sessions() as db:
        assert await db.get(AgentSkill, skill_id) is None


async def test_skill_upload_rejects_missing_csrf_and_invalid_or_oversize_files(api):
    client, _, _, _, _, _ = api
    url = "/api/v1/agent/skills/upload"
    assert (await client.post(url, files={"skill_file": ("SKILL.md", b"# Example")})).status_code == 422
    assert (await client.post(url, headers=csrf(), files={"skill_file": ("run.exe", b"# Example")})).status_code == 422
    assert (await client.post(url, headers=csrf(), files={"skill_file": ("SKILL.md", b"a" * (MAX_SKILL_BYTES + 1))})).status_code == 413


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_management_requires_super_admin_for_reads_and_writes(api, role):
    client, actor, _, _, _, _ = api
    actor["role"] = role
    for path in ("/skills", "/workspaces"):
        response = await client.get("/api/v1/agent" + path)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    response = await client.post("/api/v1/agent/skills/github", headers=csrf(), json={"url": "https://github.com/example/skill/blob/main/SKILL.md"})
    assert response.status_code == 403


async def test_github_install_uses_actor_and_never_echoes_upstream_failure(api, monkeypatch):
    client, _, _, service, _, _ = api
    install = AsyncMock(side_effect=RuntimeError("private-upstream-payload"))
    monkeypatch.setattr(service, "install_from_github_url", install)
    response = await client.post("/api/v1/agent/skills/github", headers=csrf(), json={"url": "https://github.com/example/skill/blob/main/SKILL.md"})
    assert response.status_code == 503
    assert "private-upstream-payload" not in response.text
    assert install.await_args.kwargs["created_by"] == "1"


async def test_skill_delete_cannot_remove_files_outside_skill_root(api):
    client, _, sessions, _, _, temporary = api
    protected = temporary / "protected"
    protected.mkdir()
    (protected / "SKILL.md").write_text("Do not delete", encoding="utf-8")
    async with sessions() as db:
        db.add(AgentSkill(id=7, name="Legacy", slug="legacy", install_path=str(protected / "SKILL.md"), content_hash="a" * 64))
        await db.commit()
    response = await client.delete("/api/v1/agent/skills/7", headers=csrf())
    assert response.status_code == 422
    assert (protected / "SKILL.md").read_text(encoding="utf-8") == "Do not delete"


async def test_workspace_cleanup_retains_active_tasks_and_rejects_path_escape(api):
    client, _, sessions, _, workspaces, temporary = api
    paths = []
    async with sessions() as db:
        for task_id, state in ((11, "editing"), (12, "completed")):
            db.add(AgentTeamTask(id=task_id, title="Fixture", source_type="manual_issue", repo_full_name="owner/repo", repo_owner="owner", repo_name="repo", status=state))
            path = workspaces.get_task_worktree_path("owner", "repo", task_id, "work")
            path.mkdir(parents=True)
            (path / "fixture.txt").write_text("fixture", encoding="utf-8")
            paths.append(path)
        await db.commit()
    listing = await client.get("/api/v1/agent/workspaces/owner/repo/worktrees")
    assert [item["is_active"] for item in listing.json()["items"]] == [True, False]
    denied = await client.delete("/api/v1/agent/workspaces/owner/repo", headers=csrf())
    assert not denied.json()["success"]
    denied = await client.delete("/api/v1/agent/workspaces/owner/repo/worktrees/11-work", headers=csrf())
    assert not denied.json()["success"] and paths[0].is_dir()
    cleaned = await client.post("/api/v1/agent/workspaces/owner/repo/clean", headers=csrf())
    assert cleaned.json() == {"success": True, "cleaned": 1}
    assert paths[0].is_dir() and not paths[1].exists()
    outside = temporary / "outside.txt"
    outside.write_text("protected", encoding="utf-8")
    escaped = await client.delete("/api/v1/agent/workspaces/owner/repo/worktrees/..%5C..%5Coutside.txt", headers=csrf())
    assert escaped.status_code == 400
    assert outside.read_text(encoding="utf-8") == "protected"
