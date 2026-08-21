"""WebUI Agent Skills 管理路由。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.agent_team.skill_service import AgentSkillService
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    render_template,
    require_csrf,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/agent-skills", tags=["WebUI Agent Skills"])


def _metadata_list(raw_value: str | None) -> list[str]:
    """Parse a list-like Skill metadata field for compact UI tags."""
    if not raw_value:
        return []
    try:
        value = json.loads(raw_value)
    except TypeError, ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def _skill_rows(
    service: AgentSkillService,
    skills: list[Any],
) -> list[dict[str, Any]]:
    """Build presentation rows, including safe local file manifests."""
    file_lists = await asyncio.gather(
        *(service.list_skill_files(skill.slug) for skill in skills),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    for skill, file_list in zip(skills, file_lists, strict=True):
        rows.append(
            {
                "skill": skill,
                "allowed_tools": _metadata_list(skill.allowed_tools),
                "arguments": _metadata_list(skill.arguments),
                "files": file_list if isinstance(file_list, list) else [],
            }
        )
    return rows


def _filter_skills(
    skills: list[Any],
    *,
    query: str = "",
    status: str = "",
    source: str = "",
) -> list[Any]:
    """Apply the Skills ledger filters without changing persistence order."""
    normalized_query = query.strip().casefold()
    normalized_status = status.strip().casefold()
    normalized_source = source.strip().casefold()

    filtered: list[Any] = []
    for skill in skills:
        if normalized_status == "enabled" and not skill.enabled:
            continue
        if normalized_status == "disabled" and skill.enabled:
            continue
        if normalized_source and str(skill.source_type).casefold() != normalized_source:
            continue
        if normalized_query:
            searchable = "\n".join(
                str(value or "")
                for value in (
                    skill.name,
                    skill.slug,
                    skill.description,
                    skill.when_to_use,
                    skill.version,
                    skill.source_type,
                    skill.source_url,
                    skill.source_ref,
                    skill.allowed_tools,
                    skill.arguments,
                    skill.requires,
                )
            ).casefold()
            if normalized_query not in searchable:
                continue
        filtered.append(skill)
    return filtered


@router.get("/")
async def agent_skills_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Agent Skills 管理页面。"""
    service = AgentSkillService()
    skills = await service.list_skills(db)
    root = await service.resolve_root()
    source_types = sorted(
        {str(skill.source_type).strip() for skill in skills if skill.source_type}
    )
    return render_template(
        "agent_skills.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="agent_skills",
        skills=skills,
        skill_rows=await _skill_rows(service, skills),
        total_skills=len(skills),
        enabled_skills=sum(1 for skill in skills if skill.enabled),
        source_types=source_types,
        skills_root=str(root),
    )


@router.get("/list-fragment")
async def agent_skills_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    q: str = Query("", max_length=200),
    status: str = Query("", max_length=20),
    source: str = Query("", max_length=50),
):
    """Agent Skills 列表片段。"""
    service = AgentSkillService()
    all_skills = await service.list_skills(db)
    skills = _filter_skills(
        all_skills,
        query=q,
        status=status,
        source=source,
    )
    return render_template(
        "components/agent_skills_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        skills=skills,
        skill_rows=await _skill_rows(service, skills),
        result_count=len(skills),
        has_filters=bool(q.strip() or status.strip() or source.strip()),
    )


@router.post("/upload")
async def upload_skill(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
    skill_file: UploadFile = File(...),
    name: str = Form(""),
):
    """上传 SKILL.md 或 ZIP 压缩包并安装 Skill。"""
    lang = detect_language(user_prefs)
    if not skill_file.filename:
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_invalid_file",
            "error",
            lang=lang,
        )

    lower_name = skill_file.filename.lower()
    if not (lower_name.endswith((".md", ".zip"))):
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_invalid_file",
            "error",
            lang=lang,
        )

    try:
        content = await skill_file.read()
        skill = await AgentSkillService().install_from_upload(
            db,
            content=content,
            filename=skill_file.filename,
            name=name,
            created_by=str(user.get("user_id", "")),
        )
        await log_admin_action(
            db,
            user["user_id"],
            "agent_skill_upload",
            "agent_skill",
            skill.id,
            {"slug": skill.slug, "name": skill.name},
        )
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_installed",
            lang=lang,
            slug=skill.slug,
        )
    except Exception as exc:
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_install_failed",
            "error",
            lang=lang,
            error=str(exc),
        )


@router.post("/install-github")
async def install_skill_from_github(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
    github_url: str = Form(...),
):
    """通过 GitHub 链接安装 Skill。"""
    lang = detect_language(user_prefs)
    try:
        skill = await AgentSkillService().install_from_github_url(
            db,
            url=github_url,
            created_by=str(user.get("user_id", "")),
        )
        await log_admin_action(
            db,
            user["user_id"],
            "agent_skill_install_github",
            "agent_skill",
            skill.id,
            {"slug": skill.slug, "source_url": github_url},
        )
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_installed",
            lang=lang,
            slug=skill.slug,
        )
    except Exception as exc:
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_install_failed",
            "error",
            lang=lang,
            error=str(exc),
        )


@router.post("/{skill_id}/toggle")
async def toggle_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
    enabled: str = Form("0"),
):
    """启用或停用 Skill。"""
    lang = detect_language(user_prefs)
    try:
        is_enabled = str(enabled).lower() in {"1", "true", "on", "yes"}
        skill = await AgentSkillService().set_enabled(db, skill_id, is_enabled)
        await log_admin_action(
            db,
            user["user_id"],
            "agent_skill_toggle",
            "agent_skill",
            skill.id,
            {"slug": skill.slug, "enabled": is_enabled},
        )
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_updated",
            lang=lang,
        )
    except Exception as exc:
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_update_failed",
            "error",
            lang=lang,
            error=str(exc),
        )


@router.post("/{skill_id}/delete")
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
):
    """删除 Skill。"""
    lang = detect_language(user_prefs)
    try:
        skill = await AgentSkillService().delete_skill(db, skill_id)
        await log_admin_action(
            db,
            user["user_id"],
            "agent_skill_delete",
            "agent_skill",
            skill_id,
            {"slug": skill.slug, "name": skill.name},
        )
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_deleted",
            lang=lang,
        )
    except Exception as exc:
        return toast_redirect(
            "/agent-skills/",
            "toast.agent_skill_delete_failed",
            "error",
            lang=lang,
            error=str(exc),
        )
