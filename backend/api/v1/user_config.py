"""API v1 用户级配置端点"""

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_cookie_csrf
from backend.api.v1.responses import error_response, success_response
from backend.api.v1.schemas import UserConfigUpdateRequest
from backend.core.config import (
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    USER_DYNAMIC_CONFIG_KEYS,
    get_user_dynamic_config_state,
)
from backend.webui.deps import get_db

router = APIRouter(
    prefix="/user-config",
    tags=["UserConfig"],
    dependencies=[Depends(require_cookie_csrf)],
)


@router.get("")
async def get_user_config(
    user: dict = Depends(require_api_auth),
):
    """获取当前用户可覆盖的配置项。"""
    user_id = int(user["user_id"])
    items = []
    for key in sorted(USER_DYNAMIC_CONFIG_KEYS):
        items.append(await get_user_dynamic_config_state(key, user_id))
    return success_response(data={"configs": items})


@router.patch("")
async def update_user_config(
    body: UserConfigUpdateRequest,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的配置覆盖。"""
    from backend.services.config_center_service import (
        ConfigError,
        config_center_service,
    )

    if not body.configs or set(body.configs) - USER_DYNAMIC_CONFIG_KEYS:
        return error_response("unknown_field")
    try:
        await config_center_service.save_preferences(
            db, user, {key: value or None for key, value in body.configs.items()}
        )
    except ConfigError as exc:
        await db.rollback()
        logger.opt(exception=True).warning("User preference validation failed")
        return error_response(
            f"配置项 {exc.field_id.removeprefix('personal/')} 的值不合法"
        )
    except Exception:
        logger.exception("User preference update failed")
        return error_response("更新用户配置失败")
    return success_response(message="配置已更新")


@router.get("/metadata")
async def get_user_config_metadata(user: dict = Depends(require_api_auth)):
    """获取用户可配置项元数据。"""
    return success_response(
        data={
            "allowed_keys": sorted(USER_DYNAMIC_CONFIG_KEYS),
            "options": {
                key: DYNAMIC_CONFIG_SELECT_OPTIONS.get(key, [])
                for key in USER_DYNAMIC_CONFIG_KEYS
            },
        }
    )
