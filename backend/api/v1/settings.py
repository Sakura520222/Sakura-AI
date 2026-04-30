"""API v1 个人设置端点"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import WebUIConfig
from backend.webui.deps import get_db, invalidate_user_prefs_cache

from backend.api.v1.deps import require_api_auth
from backend.api.v1.responses import success_response

router = APIRouter(prefix="/settings", tags=["Settings"])

VALID_THEMES = ("light", "dark", "system")
VALID_ITEMS_PER_PAGE = (10, 20, 50, 100)


@router.get("")
async def get_settings(
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取个人偏好设置"""
    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
    )
    config = result.scalar_one_or_none()

    data = {
        "theme": config.theme if config else "system",
        "language": config.language if config else "zh-CN",
        "items_per_page": config.items_per_page if config else 20,
    }

    return success_response(data=data)


@router.patch("")
async def update_settings(
    body: dict,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新个人偏好设置"""
    theme = body.get("theme")
    items_per_page = body.get("items_per_page")
    language = body.get("language")

    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
    )
    config = result.scalar_one_or_none()

    if config:
        if theme is not None and theme in VALID_THEMES:
            config.theme = theme
        if items_per_page is not None and int(items_per_page) in VALID_ITEMS_PER_PAGE:
            config.items_per_page = int(items_per_page)
        if language is not None:
            config.language = language
    else:
        config = WebUIConfig(
            user_id=user["user_id"],
            theme=theme if theme in VALID_THEMES else "system",
            language=language or "zh-CN",
            items_per_page=(
                int(items_per_page)
                if items_per_page and int(items_per_page) in VALID_ITEMS_PER_PAGE
                else 20
            ),
        )
        db.add(config)

    await db.commit()
    invalidate_user_prefs_cache(user["user_id"])

    logger.info(f"API 设置已更新: user={user['sub']}")
    return success_response(message="设置已更新")


@router.get("/about")
async def get_about(
    user: dict = Depends(require_api_auth),
):
    """获取系统版本信息"""
    from backend.webui.routes.auth import APP_VERSION

    return success_response(
        data={
            "version": APP_VERSION,
            "build_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )
