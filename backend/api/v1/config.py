"""API v1 配置管理端点"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    get_label_config,
    reload_label_config,
    reload_strategy_config,
)
from backend.models.database import AppConfig
from backend.webui.deps import get_db

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.responses import success_response, error_response
from backend.api.v1.schemas import (
    ConfigGeneralUpdateRequest,
    ConfigStrategyUpdateRequest,
    ConfigLabelsUpdateRequest,
    ConfigLabelRecommendationUpdateRequest,
)
from backend.api.v1.deps import limiter
from backend.core.setup_service import setup_service

router = APIRouter(prefix="/config", tags=["Config"])

_config_lock = asyncio.Lock()

# 配置文件绝对路径（不依赖工作目录）
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_STRATEGIES_PATH = _CONFIG_DIR / "strategies.yaml"
_LABELS_PATH = _CONFIG_DIR / "labels.yaml"


class AIModelsRequest(BaseModel):
    """AI 模型列表请求。"""

    api_key: str | None = None
    api_base: str | None = None
    # 配置项名（openai_api_key / summary_api_key），用于回退数据库读取真实 Key
    key_name: str | None = None


def _mask_sensitive(value: str, key: str) -> str:
    """对敏感配置字段进行脱敏"""
    sensitive_keys = ("secret", "key", "token", "password", "credential")
    if any(s in key.lower() for s in sensitive_keys):
        if value and len(value) > 8:
            return value[:4] + "****" + value[-4:]
        return "****"
    return value


def _is_masked_value(value: str) -> bool:
    """判断配置值是否为脱敏后的占位值（前端配置页回显的掩码）。"""
    return "****" in value


async def _resolve_provider_credentials(
    api_key: str,
    api_base: str,
    key_name: str | None,
    db: AsyncSession,
) -> tuple[str, str]:
    """解析获取模型列表所需的真实 api_key / api_base。

    配置页表单回显的敏感字段为脱敏占位值（含 ``****``），不可直接用于请求；
    当传入空值或脱敏占位值时，回退读取数据库中的真实值。
    ``key_name`` 须以 ``_api_key`` 结尾，用于定位正确的配置项，默认 openai_api_key。
    """
    key_name = (key_name or "").strip()
    if not key_name.endswith("_api_key"):
        key_name = "openai_api_key"
    base_name = key_name.removesuffix("_api_key") + "_api_base"

    if not api_key or _is_masked_value(api_key):
        result = await db.execute(
            select(AppConfig.key_value).where(AppConfig.key_name == key_name)
        )
        api_key = result.scalar_one_or_none() or ""
    if not api_base:
        result = await db.execute(
            select(AppConfig.key_value).where(AppConfig.key_name == base_name)
        )
        api_base = result.scalar_one_or_none() or ""
    return api_key, api_base


@router.get("/ai-providers")
async def get_ai_providers(user: dict = Depends(require_api_super_admin)):
    """获取内置 AI 厂商列表。"""
    return success_response(data={"providers": setup_service.list_ai_providers()})


@router.post("/ai-providers/{provider}/models")
@limiter.limit("10/minute")
async def get_ai_provider_models(
    request: Request,
    provider: str,
    body: AIModelsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """按厂商获取模型列表。

    配置页表单回显的 API Key 为脱敏占位值，故空值或脱敏值时回退数据库真实值；
    ``body.key_name`` 指定配置项（openai_api_key / summary_api_key）。
    """
    api_key, api_base = await _resolve_provider_credentials(
        (body.api_key or "").strip(),
        (body.api_base or "").strip(),
        body.key_name,
        db,
    )

    result = await setup_service.fetch_provider_models(provider, api_key, api_base)
    return success_response(data=result)


@router.get("/general")
async def get_general_config(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """获取全局配置"""
    result = await db.execute(select(AppConfig).order_by(AppConfig.key_name))
    configs = {}
    for row in result.scalars().all():
        configs[row.key_name] = _mask_sensitive(row.key_value, row.key_name)

    return success_response(data={"configs": configs})


@router.patch("/general")
async def update_general_config(
    body: ConfigGeneralUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """更新全局配置"""
    from sqlalchemy import select

    configs = body.configs
    if not configs:
        return error_response("配置内容不能为空")

    async with _config_lock:
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
        logger.error(f"读取策略配置失败: {e}")
        return error_response("读取策略配置失败")


@router.patch("/strategies/{section}")
async def update_strategy_section(
    section: str,
    body: ConfigStrategyUpdateRequest,
    user: dict = Depends(require_api_super_admin),
):
    """更新策略配置的某个 section"""
    data = body.data
    if not data:
        return error_response("配置内容不能为空")

    try:
        import yaml

        async with _config_lock:
            config_content = await asyncio.to_thread(
                _STRATEGIES_PATH.read_text, encoding="utf-8"
            )
            config = yaml.safe_load(config_content) or {}

            if section not in config:
                return error_response(f"未知的策略 section: {section}")

            config[section].update(data)

            dump = yaml.dump(config, default_flow_style=False, allow_unicode=True)
            await asyncio.to_thread(_STRATEGIES_PATH.write_text, dump, encoding="utf-8")

            # 重载（在锁内保证原子性）
            reload_strategy_config()

        logger.info(f"API 更新策略配置: section={section}, by={user['sub']}")
        return success_response(message=f"策略配置 {section} 已更新")
    except Exception as e:
        logger.error(f"更新策略配置失败: {e}")
        return error_response("更新策略配置失败")


@router.get("/labels")
async def get_labels(user: dict = Depends(require_api_super_admin)):
    """获取标签配置"""
    try:
        config = get_label_config()
        return success_response(
            data={
                "labels": config.get("labels", []),
                "recommendation": config.get("recommendation", {}),
            }
        )
    except Exception as e:
        logger.error(f"读取标签配置失败: {e}")
        return error_response("读取标签配置失败")


@router.put("/labels")
async def update_labels(
    body: ConfigLabelsUpdateRequest,
    user: dict = Depends(require_api_super_admin),
):
    """更新标签定义（全量覆盖）"""
    labels = body.labels
    if not labels:
        return error_response("标签列表不能为空")

    try:
        import yaml

        async with _config_lock:
            config_content = await asyncio.to_thread(
                _LABELS_PATH.read_text, encoding="utf-8"
            )
            config = yaml.safe_load(config_content) or {}

            config["labels"] = labels

            dump = yaml.dump(config, default_flow_style=False, allow_unicode=True)
            await asyncio.to_thread(_LABELS_PATH.write_text, dump, encoding="utf-8")

        reload_label_config()

        logger.info(f"API 更新标签定义, by={user['sub']}")
        return success_response(message="标签定义已更新")
    except Exception as e:
        logger.error(f"更新标签配置失败: {e}")
        return error_response("更新标签配置失败")


@router.patch("/labels/recommendation")
async def update_label_recommendation(
    body: ConfigLabelRecommendationUpdateRequest,
    user: dict = Depends(require_api_super_admin),
):
    """更新标签推荐设置"""
    recommendation = body.recommendation
    if not recommendation:
        return error_response("推荐设置不能为空")

    try:
        import yaml

        async with _config_lock:
            config_content = await asyncio.to_thread(
                _LABELS_PATH.read_text, encoding="utf-8"
            )
            config = yaml.safe_load(config_content) or {}

            config["recommendation"] = recommendation

            dump = yaml.dump(config, default_flow_style=False, allow_unicode=True)
            await asyncio.to_thread(_LABELS_PATH.write_text, dump, encoding="utf-8")

        reload_label_config()

        logger.info(f"API 更新标签推荐设置, by={user['sub']}")
        return success_response(message="标签推荐设置已更新")
    except Exception as e:
        logger.error(f"更新推荐设置失败: {e}")
        return error_response("更新推荐设置失败")
