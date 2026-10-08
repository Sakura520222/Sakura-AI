"""Super-admin deployment plugin management; no model-controlled configuration."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.models.database import AppConfig
from backend.services.agent_team.capability_policy import PermissionProfile
from backend.services.agent_team.plugin_config import (
    PluginConfigError,
    public_plugin_config,
    read_plugin_config,
    save_plugin_config,
)
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

router = APIRouter(prefix="/agent-plugins", tags=["WebUI Agent Plugins"])


async def _public_state(db):
    config = await read_plugin_config(db)
    result = await db.execute(
        select(AppConfig).where(AppConfig.key_name == "agent_team_permission_profile")
    )
    row = result.scalar_one_or_none()
    profile = row.key_value if row else get_settings().agent_team_permission_profile
    try:
        PermissionProfile(profile)
    except ValueError:
        raise PluginConfigError("invalid_permission_profile") from None
    return {"plugins": public_plugin_config(config), "profile": profile}


@router.get("/")
async def plugins_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    try:
        state = await _public_state(db)
    except PluginConfigError:
        raise HTTPException(503, "plugin_configuration_unavailable") from None
    skills = await AgentSkillService().list_skills(db)
    return render_template(
        "agent_plugins.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="agent_plugins",
        csrf_token=get_csrf_serializer().dumps({}),
        plugin_json=json.dumps(state["plugins"], indent=2, ensure_ascii=False),
        plugins=state["plugins"],
        profile=state["profile"],
        profiles=[p.value for p in PermissionProfile],
        skill_count=len(skills),
        enabled_skill_count=sum(bool(s.enabled) for s in skills),
    )


@router.get("/config")
async def plugins_config(
    db: AsyncSession = Depends(get_db), user: dict = Depends(require_super_admin)
):
    try:
        return JSONResponse(await _public_state(db))
    except PluginConfigError:
        raise HTTPException(503, "plugin_configuration_unavailable") from None


@router.post("/save")
async def save_plugins(
    request: Request,
    plugins: str = Form(...),
    profile: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    try:
        config = await save_plugin_config(db, plugins, profile)
    except PluginConfigError:
        await db.rollback()
        return toast_redirect(
            "/agent-plugins/",
            "agent_plugins.invalid",
            "error",
            lang=detect_language(user_prefs),
        )
    # Only identifiers/counts are audited, never headers, endpoint text or argv.
    await log_admin_action(
        db,
        user["user_id"],
        "config_save",
        "agent_plugins",
        None,
        {
            "mcp_count": len(config.mcp.servers),
            "hook_count": len(config.hooks),
            "profile": profile,
        },
    )
    return toast_redirect(
        "/agent-plugins/", "agent_plugins.saved", lang=detect_language(user_prefs)
    )
