"""WebUI Agent Skills 管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
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
    return render_template(
        "agent_skills.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="agent_skills",
        skills=skills,
        skills_root=str(root),
    )


@router.get("/list-fragment")
async def agent_skills_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Agent Skills 列表片段。"""
    skills = await AgentSkillService().list_skills(db)
    return render_template(
        "components/agent_skills_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        skills=skills,
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
