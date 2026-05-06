"""WebUI 个人设置路由"""

from fastapi import APIRouter, Request, Depends, Form
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import WebUIConfig
from backend.webui.deps import (
    require_auth,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    toast_redirect,
    invalidate_user_prefs_cache,
    render_template,
)
from backend.webui.i18n import detect_language

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
    return render_template(
        "settings.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="settings",
        items_per_page=user_prefs["items_per_page"],
        language=user_prefs["language"],
    )


@router.post("/")
async def save_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    items_per_page: int = Form(...),
    language: str = Form(default="zh-CN"),
):
    """保存个人设置"""
    # 验证参数范围
    if items_per_page not in (10, 20, 50, 100):
        return toast_redirect(
            "/webui/settings/",
            "toast.invalid_param",
            "error",
            lang=detect_language({"language": language}),
        )

    # 验证语言参数
    if language not in ("zh-CN", "en"):
        language = "zh-CN"

    # Upsert 配置
    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
    )
    config = result.scalar_one_or_none()
    if config:
        config.items_per_page = items_per_page
        config.language = language
    else:
        config = WebUIConfig(
            user_id=user["user_id"],
            items_per_page=items_per_page,
            language=language,
        )
        db.add(config)
    await db.commit()

    invalidate_user_prefs_cache(user["user_id"])

    logger.info(
        f"WebUI 设置已更新: user={user['sub']}, items_per_page={items_per_page}, language={language}"
    )
    return toast_redirect(
        "/webui/settings/",
        "toast.settings_saved",
        lang=detect_language({"language": language}),
    )


@router.get("/about")
async def about_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """关于页面"""
    from datetime import datetime
    from backend.webui.routes.auth import APP_VERSION

    return render_template(
        "about.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="about",
        app_version=APP_VERSION,
        build_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
