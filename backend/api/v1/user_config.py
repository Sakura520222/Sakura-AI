"""API v1 用户级配置端点"""

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth
from backend.api.v1.responses import error_response, success_response
from backend.api.v1.schemas import UserConfigUpdateRequest
from backend.core.config import (
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    USER_DYNAMIC_CONFIG_KEYS,
    get_user_dynamic_config_state,
    invalidate_user_dynamic_config_cache,
    validate_user_dynamic_config_value,
)
from backend.models.database import UserConfig
from backend.webui.deps import get_db

router = APIRouter(prefix="/user-config", tags=["UserConfig"])


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
    configs = body.configs
    if len(configs) == 0:
        return error_response("配置内容不能为空")

    user_id = int(user["user_id"])
    updated_keys: list[str] = []

    try:
        for key, raw_value in configs.items():
            if key not in USER_DYNAMIC_CONFIG_KEYS:
                return error_response(f"配置项不允许用户覆盖: {key}")

            # null 表示删除用户覆盖，回退全局配置。
            if raw_value is None:
                result = await db.execute(
                    select(UserConfig).where(
                        UserConfig.user_id == user_id,
                        UserConfig.config_key == key,
                    )
                )
                existing = result.scalar_one_or_none()
                if existing:
                    await db.delete(existing)
                updated_keys.append(key)
                continue

            value = validate_user_dynamic_config_value(key, raw_value)
            result = await db.execute(
                select(UserConfig).where(
                    UserConfig.user_id == user_id,
                    UserConfig.config_key == key,
                )
            )
            config = result.scalar_one_or_none()
            if config:
                config.config_value = value
                config.description = DYNAMIC_CONFIG_LABELS.get(key, key)
            else:
                db.add(
                    UserConfig(
                        user_id=user_id,
                        config_key=key,
                        config_value=value,
                        description=DYNAMIC_CONFIG_LABELS.get(key, key),
                    )
                )
            updated_keys.append(key)

        await db.commit()
    except ValueError:
        await db.rollback()
        logger.warning("用户配置更新校验失败 / user config validation failed", exc_info=True)
        return error_response("配置数据无效，请检查后重试")
    except Exception as e:
        await db.rollback()
        logger.error(f"更新用户配置失败: {e}", exc_info=True)
        return error_response("更新用户配置失败")

    invalidate_user_dynamic_config_cache(user_id, updated_keys)
    logger.info(f"用户配置已更新: user={user['sub']}, keys={updated_keys}")
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
