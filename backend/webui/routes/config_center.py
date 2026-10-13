"""Transitional shell for the versioned Settings API (WebUI 2.0 boundary)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.config_metadata import CATEGORIES
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    render_template,
    require_auth,
)

router = APIRouter(tags=["Configuration Centre"])
ASSET_DIR = Path(__file__).parent.parent / "assets" / "settings"


@router.get("/settings-assets/{name}", include_in_schema=False)
async def settings_asset(name: str):
    if name not in {"center.js", "field-controller.js", "client.js", "center.css"}:
        raise HTTPException(404)
    return FileResponse(
        ASSET_DIR / name,
        media_type="text/css" if name.endswith(".css") else "text/javascript",
    )


@router.get("/settings-center", include_in_schema=False)
async def center_index(user: dict = Depends(require_auth)):
    return RedirectResponse("/settings-center/personal", status_code=302)


@router.get("/settings-center/{category}", include_in_schema=False)
async def center_page(
    request: Request,
    category: str,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    db: AsyncSession = Depends(get_db),
):
    if category not in CATEGORIES or (
        category != "personal" and user.get("role") != "super_admin"
    ):
        raise HTTPException(404)
    if category == "personal":
        from backend.webui.routes.settings import _render_settings_page

        return await _render_settings_page(request, db, user, user_prefs)
    template = {
        "ai": "config_ai.html",
        "system": "system_config.html",
        "backup": "config_backup.html",
    }.get(category, "config_center.html")
    context = {}
    if category == "system":
        from backend.services.database_reset_service import DATABASE_RESET_CONFIRMATION

        context["database_reset_confirmation"] = DATABASE_RESET_CONFIRMATION
    return render_template(
        template,
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="config_center",
        category=category,
        csrf_token=get_csrf_serializer().dumps({}),
        **context,
    )
