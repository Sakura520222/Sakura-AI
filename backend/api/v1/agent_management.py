"""Versioned Skills/workspace management with the existing service boundaries."""

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.api.v1.responses import success_response
from backend.core.time_service import format_rfc3339, from_unix_timestamp
from backend.models.agent_skill_models import AgentSkill
from backend.models.agent_team_models import AgentTeamTask
from backend.services.agent_team.skill_service import (
    MAX_SKILL_BYTES,
    MAX_SKILL_DIR_BYTES,
    AgentSkillService,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.routes import agent_skills as skills
from backend.webui.routes import agent_team as team

router = APIRouter(
    prefix="/agent", tags=["Agent Management"], route_class=PrivateAPIRoute
)


class SkillView(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    when_to_use: str | None
    version: str | None
    source_type: str
    source_url: str | None
    source_ref: str | None
    install_path: str
    enabled: bool
    file_count: int
    allowed_tools: list[str]
    arguments: list[str]
    requires: str | None
    has_error: bool
    updated_at: str


class SkillList(BaseModel):
    items: list[SkillView]
    total: int
    enabled: int
    sources: list[str]
    root: str
    upload_limit: int = MAX_SKILL_DIR_BYTES
    markdown_limit: int = MAX_SKILL_BYTES


class GithubSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=2048)


class SkillState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def skill_view(skill: AgentSkill) -> SkillView:
    return SkillView(
        **{key: getattr(skill, key) for key in (
            "id", "name", "slug", "description", "when_to_use", "version",
            "source_type", "source_url", "source_ref", "install_path", "file_count",
            "requires",
        )},
        enabled=bool(skill.enabled),
        allowed_tools=skills._metadata_list(skill.allowed_tools),
        arguments=skills._metadata_list(skill.arguments),
        has_error=bool(skill.error_message),
        updated_at=format_rfc3339(skill.updated_at),
    )


@router.get("/skills", response_model=SkillList)
async def list_skills(
    search: str = Query("", max_length=200),
    status: str = Query("", max_length=20),
    source: str = Query("", max_length=50),
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    service = AgentSkillService()
    rows = await service.list_skills(db)
    filtered = skills._filter_skills(rows, query=search, status=status, source=source)
    return SkillList(
        items=[skill_view(row) for row in filtered],
        total=len(rows), enabled=sum(bool(row.enabled) for row in rows),
        sources=sorted({row.source_type for row in rows}),
        root=str(await service.resolve_root()),
    )


@router.get("/skills/{skill_id}/files")
async def skill_files(
    skill_id: int, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    skill = await db.get(AgentSkill, skill_id)
    if skill is None:
        raise HTTPException(404, "skill_not_found")
    return {"files": await AgentSkillService().list_skill_files(skill.slug)}


async def audit_skill(db, user, action: str, skill: AgentSkill):
    await log_admin_action(db, user["user_id"], action, "agent_skill", skill.id,
                           {"slug": skill.slug, "name": skill.name, "enabled": bool(skill.enabled)})


@router.post("/skills/upload", dependencies=[Depends(require_csrf_header)])
async def upload_skill(
    skill_file: UploadFile = File(...), name: str = Form("", max_length=255),
    user: dict = Depends(require_api_super_admin), db: AsyncSession = Depends(get_db),
):
    filename = skill_file.filename or ""
    if not filename.lower().endswith((".md", ".zip")):
        raise HTTPException(422, "unsupported_skill_file")
    limit = MAX_SKILL_DIR_BYTES if filename.lower().endswith(".zip") else MAX_SKILL_BYTES
    content = await skill_file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, "skill_upload_too_large")
    try:
        skill = await AgentSkillService().install_from_upload(
            db, content=content, filename=filename, name=name, created_by=str(user["user_id"])
        )
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid_skill_package") from None
    except Exception:
        logger.exception("Agent Skill upload failed")
        raise HTTPException(503, "skill_install_failed") from None
    await audit_skill(db, user, "agent_skill_upload", skill)
    return success_response(data={"id": skill.id, "slug": skill.slug})


@router.post("/skills/github", dependencies=[Depends(require_csrf_header)])
async def install_github_skill(
    body: GithubSkill, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        skill = await AgentSkillService().install_from_github_url(
            db, url=body.url, created_by=str(user["user_id"])
        )
    except ValueError:
        raise HTTPException(422, "invalid_skill_source") from None
    except Exception:
        logger.exception("Agent Skill GitHub installation failed")
        raise HTTPException(503, "skill_install_failed") from None
    await audit_skill(db, user, "agent_skill_install_github", skill)
    return success_response(data={"id": skill.id, "slug": skill.slug})


@router.patch("/skills/{skill_id}", dependencies=[Depends(require_csrf_header)])
async def toggle_skill(
    skill_id: int, body: SkillState, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(AgentSkill, skill_id) is None:
        raise HTTPException(404, "skill_not_found")
    skill = await AgentSkillService().set_enabled(db, skill_id, body.enabled)
    await audit_skill(db, user, "agent_skill_toggle", skill)
    return success_response(data={"id": skill.id, "enabled": bool(skill.enabled)})


@router.delete("/skills/{skill_id}", dependencies=[Depends(require_csrf_header)])
async def delete_skill(
    skill_id: int, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(AgentSkill, skill_id) is None:
        raise HTTPException(404, "skill_not_found")
    try:
        skill = await AgentSkillService().delete_skill(db, skill_id)
    except ValueError:
        raise HTTPException(422, "invalid_skill_path") from None
    await audit_skill(db, user, "agent_skill_delete", skill)
    return success_response()


@router.get("/workspaces")
async def list_workspaces(user: dict = Depends(require_api_super_admin)):
    service = AgentTeamWorkspaceService()
    rows = await asyncio.to_thread(service.list_workspaces)
    return {"root": str(service.base_dir), "items": jsonable_encoder([
        team._workspace_info_to_dict(row) for row in rows
    ]), "total_size_bytes": sum(row.total_size_bytes for row in rows)}


@router.get("/workspaces/{owner}/{name}/worktrees")
async def list_worktrees(
    owner: str, name: str, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        worktrees = await asyncio.to_thread(AgentTeamWorkspaceService().list_worktrees, owner, name)
    except ValueError:
        raise HTTPException(422, "invalid_workspace_path") from None
    ids = [row.task_id for row in worktrees if row.task_id is not None]
    statuses = {}
    if ids:
        result = await db.execute(select(AgentTeamTask.id, AgentTeamTask.status).where(AgentTeamTask.id.in_(ids)))
        statuses = {row.id: row.status for row in result.all()}
    return {"items": [{
        "dir_name": row.dir_name, "task_id": row.task_id,
        "branch_slug": row.branch_slug, "file_count": row.file_count,
        "total_size_bytes": row.total_size_bytes,
        "modified_at": format_rfc3339(from_unix_timestamp(row.modified_at)) if row.modified_at is not None else None,
        "task_status": statuses.get(row.task_id),
        "is_active": statuses.get(row.task_id) in team.AGENT_TEAM_ACTIVE_STATUSES,
    } for row in worktrees]}


@router.delete("/workspaces/{owner}/{name}")
async def delete_workspace(
    owner: str, name: str, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db), csrf: str = Depends(require_csrf_header),
):
    return await team.delete_workspace(db=db, user=user, csrf_token=csrf, repo_owner=owner, repo_name=name)


@router.delete("/workspaces/{owner}/{name}/worktrees/{directory}")
async def delete_worktree(
    owner: str, name: str, directory: str,
    user: dict = Depends(require_api_super_admin), db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await team.delete_worktree(db=db, user=user, csrf_token=csrf, repo_owner=owner, repo_name=name, dir_name=directory)


@router.post("/workspaces/{owner}/{name}/clean")
async def clean_worktrees(
    owner: str, name: str, user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db), csrf: str = Depends(require_csrf_header),
):
    return await team.clean_orphan_worktrees(db=db, user=user, csrf_token=csrf, repo_owner=owner, repo_name=name)
