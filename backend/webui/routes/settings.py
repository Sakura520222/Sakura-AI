"""WebUI 个人设置路由"""

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import WebUIConfig
from backend.webui.deps import (
    require_auth, get_db, get_templates, get_csrf_serializer,
    require_csrf, get_user_preferences,
)

router = APIRouter(prefix="/settings", tags=["WebUI Settings"])
templates = get_templates()


@router.get("/")
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染个人设置页面"""
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "settings",
        "user_prefs": user_prefs,
        "current_language": user_prefs["language"],
        "items_per_page": user_prefs["items_per_page"],
    })


@router.post("/")
async def save_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    language: str = Form(...),
    items_per_page: int = Form(...),
) -> RedirectResponse:
    """保存个人设置"""
    # 验证参数范围
    if language not in ("zh-CN", "en") or items_per_page not in (10, 20, 50, 100):
        return RedirectResponse(url="/webui/settings/?error=1", status_code=302)

    # Upsert 配置
    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
    )
    config = result.scalar_one_or_none()
    if config:
        config.language = language
        config.items_per_page = items_per_page
    else:
        config = WebUIConfig(
            user_id=user["user_id"],
            language=language,
            items_per_page=items_per_page,
        )
        db.add(config)
    await db.commit()

    logger.info(f"WebUI 设置已更新: user={user['sub']}, language={language}, items_per_page={items_per_page}")
    return RedirectResponse(url="/webui/settings/?saved=1", status_code=302)


@router.get("/about")
async def about_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """关于页面"""
    from datetime import datetime
    from backend.webui.routes.auth import APP_VERSION

    return templates.TemplateResponse("about.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "about",
        "user_prefs": user_prefs,
        "app_version": APP_VERSION,
        "build_date": datetime.utcnow().strftime('%Y-%m-%d'),
    })
