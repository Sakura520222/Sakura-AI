"""API v1 个人设置端点"""

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_cookie_csrf
from backend.api.v1.responses import success_response
from backend.core.time_service import get_time_service
from backend.models.database import WebUIConfig
from backend.webui.deps import get_db

router = APIRouter(
    prefix="/settings", tags=["Settings"], dependencies=[Depends(require_cookie_csrf)]
)

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
        "theme": (config.theme or "system") if config else "system",
        "language": (config.language or "zh-CN") if config else "zh-CN",
        "items_per_page": (config.items_per_page or 20) if config else 20,
    }

    return success_response(data=data)


@router.patch("")
async def update_settings(
    body: dict,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新个人偏好设置"""
    from backend.api.v1.responses import error_response
    from backend.services.config_center_service import (
        ConfigError,
        config_center_service,
    )

    if set(body) - {"theme", "language", "items_per_page"}:
        return error_response("unknown_field")
    # This legacy endpoint accepted int-compatible values before the typed centre.
    # Keep coercion here; the config-centre JSON contract remains strict.
    if body.get("items_per_page") is not None:
        try:
            body = {**body, "items_per_page": int(body["items_per_page"])}
        except ValueError, TypeError, OverflowError:
            return error_response("invalid_type")
    try:
        await config_center_service.save_preferences(db, user, body)
    except ConfigError as exc:
        return error_response(exc.code)

    logger.info(f"API 设置已更新: user={user['sub']}")
    return success_response(message="设置已更新")


@router.get("/about")
async def get_about(
    user: dict = Depends(require_api_auth),
):
    """获取系统版本信息"""
    from backend.core.branding import SAKURA_AI_REPO_URL
    from backend.core.build_info import get_build_info
    from backend.webui.routes.auth import APP_VERSION

    build_info = get_build_info()
    return success_response(
        data={
            "version": APP_VERSION,
            "build_date": get_time_service()
            .to_app_timezone(get_time_service().now_utc())
            .strftime("%Y-%m-%d"),
            "channel": build_info["channel"],
            "revision": build_info["revision"],
            "repo_url": SAKURA_AI_REPO_URL,
        }
    )
