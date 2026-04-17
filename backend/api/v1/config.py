"""API v1 配置管理端点"""

import threading

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import AppConfig
from backend.webui.deps import get_db

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.responses import success_response, error_response

router = APIRouter(prefix="/config", tags=["Config"])

# 配置读写锁（复用 WebUI config 路由的锁模式）
_config_lock = threading.Lock()


def _mask_sensitive(value: str, key: str) -> str:
    """对敏感配置字段进行脱敏"""
    sensitive_keys = ("secret", "key", "token", "password", "credential")
    if any(s in key.lower() for s in sensitive_keys):
        if value and len(value) > 8:
            return value[:4] + "****" + value[-4:]
        return "****"
    return value


@router.get("/general")
async def get_general_config(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """获取全局配置"""
    from sqlalchemy import select

    result = await db.execute(select(AppConfig).order_by(AppConfig.key_name))
    configs = {}
    for row in result.scalars().all():
        configs[row.key_name] = _mask_sensitive(row.key_value, row.key_name)

    return success_response(data={"configs": configs})


@router.patch("/general")
async def update_general_config(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """更新全局配置"""
    from sqlalchemy import select

    configs = body.get("configs", {})
    if not configs:
        return error_response("配置内容不能为空")

    with _config_lock:
        for key, value in configs.items():
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            config = result.scalar_one_or_none()
            if config:
                config.key_value = str(value)
            else:
                db.add(AppConfig(key_name=key, key_value=str(value)))

        await db.commit()

    # 重新加载动态配置
    try:
        from backend.core.config import load_dynamic_configs_to_settings
        await load_dynamic_configs_to_settings()
    except Exception as e:
        logger.warning(f"重载动态配置失败: {e}")

    logger.info(f"API 更新全局配置: {list(configs.keys())}, by={user['sub']}")
    return success_response(message="配置已更新")


@router.get("/strategies")
async def get_strategies(user: dict = Depends(require_api_super_admin)):
    """获取策略配置"""
    try:
        from backend.core.config import get_strategy_config

        config = get_strategy_config()
        return success_response(data={"strategies": config})
    except Exception as e:
        return error_response(f"读取策略配置失败: {e}")


@router.patch("/strategies/{section}")
async def update_strategy_section(
    section: str,
    body: dict,
    user: dict = Depends(require_api_super_admin),
):
    """更新策略配置的某个 section"""
    data = body.get("data", body)
    if not data:
        return error_response("配置内容不能为空")

    try:
        import yaml
        from pathlib import Path

        config_path = Path("config/strategies.yaml")

        with _config_lock:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            if section not in config:
                return error_response(f"未知的策略 section: {section}")

            config[section].update(data)

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        # 重载
        from backend.core.config import reload_strategy_config
        reload_strategy_config()

        logger.info(f"API 更新策略配置: section={section}, by={user['sub']}")
        return success_response(message=f"策略配置 {section} 已更新")
    except Exception as e:
        return error_response(f"更新策略配置失败: {e}")


@router.get("/labels")
async def get_labels(user: dict = Depends(require_api_super_admin)):
    """获取标签配置"""
    try:
        from backend.core.config import get_label_config

        config = get_label_config()
        return success_response(data={"labels": config.get("labels", []), "recommendation": config.get("recommendation", {})})
    except Exception as e:
        return error_response(f"读取标签配置失败: {e}")


@router.put("/labels")
async def update_labels(
    body: dict,
    user: dict = Depends(require_api_super_admin),
):
    """更新标签定义（全量覆盖）"""
    labels = body.get("labels", [])
    if not labels:
        return error_response("标签列表不能为空")

    try:
        import yaml
        from pathlib import Path
        from backend.core.config import reload_label_config

        config_path = Path("config/labels.yaml")

        with _config_lock:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            config["labels"] = labels

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        reload_label_config()

        logger.info(f"API 更新标签定义, by={user['sub']}")
        return success_response(message="标签定义已更新")
    except Exception as e:
        return error_response(f"更新标签配置失败: {e}")


@router.patch("/labels/recommendation")
async def update_label_recommendation(
    body: dict,
    user: dict = Depends(require_api_super_admin),
):
    """更新标签推荐设置"""
    recommendation = body.get("recommendation", body)
    if not recommendation:
        return error_response("推荐设置不能为空")

    try:
        import yaml
        from pathlib import Path
        from backend.core.config import reload_label_config

        config_path = Path("config/labels.yaml")

        with _config_lock:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            config["recommendation"] = recommendation

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        reload_label_config()

        logger.info(f"API 更新标签推荐设置, by={user['sub']}")
        return success_response(message="标签推荐设置已更新")
    except Exception as e:
        return error_response(f"更新推荐设置失败: {e}")
