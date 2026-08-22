"""API v1 配置管理端点"""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import limiter, require_api_super_admin
from backend.api.v1.responses import error_response, success_response
from backend.api.v1.schemas import (
    ConfigGeneralUpdateRequest,
    ConfigLabelRecommendationUpdateRequest,
    ConfigLabelsUpdateRequest,
    ConfigStrategyUpdateRequest,
)
from backend.core.config import (
    AI_STRATEGY_CONFIG_KEYS,
    get_label_config,
    get_settings,
    update_settings_field,
)
from backend.core.config_sections import SECTION_REGISTRY
from backend.core.setup_service import setup_service
from backend.models.database import AppConfig
from backend.services.label_service import label_service
from backend.services.section_config_service import section_config_service
from backend.webui.deps import get_db

router = APIRouter(prefix="/config", tags=["Config"])

_config_lock = asyncio.Lock()

# 策略 section 名 → 统一配置节键（与 SECTION_REGISTRY 对齐）
_STRATEGY_SECTION_KEY_MAP = {
    spec["section"]: key
    for key, spec in SECTION_REGISTRY.items()
    if spec["target"] == "strategy"
}


def _normalize_label_definitions(
    labels: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Convert the legacy list payload to the keyed section representation."""
    normalized: dict[str, dict[str, Any]] = {}
    for index, label in enumerate(labels, start=1):
        if not isinstance(label, dict):
            raise ValueError(f"第 {index} 个标签定义必须是对象")
        name = label.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 个标签定义缺少有效的 name")
        name = name.strip()
        if name in normalized:
            raise ValueError(f"标签名称重复: {name}")
        normalized[name] = {
            key: value for key, value in label.items() if key != "name"
        }
    return normalized


class AIModelsRequest(BaseModel):
    """AI 模型列表请求。使用已保存账号 ID，不接受旧扁平凭据。"""

    account_id: str


def _mask_sensitive(value: str, key: str) -> str:
    """对敏感配置字段进行脱敏"""
    sensitive_keys = ("secret", "key", "token", "password", "credential")
    if key.startswith("ai_account."):
        try:
            account = json.loads(value)
        except json.JSONDecodeError, TypeError:
            return "****"
        if not isinstance(account, dict):
            return "****"
        account["has_key"] = bool(account.get("api_key"))
        account["api_key"] = "****" if account.get("api_key") else ""
        return json.dumps(account, ensure_ascii=False)
    if any(s in key.lower() for s in sensitive_keys):
        if value and len(value) > 8:
            return value[:4] + "****" + value[-4:]
        return "****"
    return value


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
    """按已保存账号获取模型列表，不读取旧 AppConfig provider/key/base 键。"""
    account_id = body.account_id.strip()
    account = await account_store.get_account(account_id)
    if account is None:
        return error_response("AI 账号不存在")
    if provider.strip().lower() != account.provider_id:
        return error_response("请求厂商与账号不匹配")

    result = await probe_account(
        provider_id=account.provider_id,
        protocol=account.protocol,
        api_base=account.api_base,
        api_key=account.api_key,
        model=account.default_model,
    )
    return success_response(data=result)


# =========================================================================
# AI 账号管理（多厂商持久化）/ AI account management (multi-vendor persistence)
# =========================================================================


from backend.core.ai_protocol import account_store
from backend.core.ai_protocol.account_probe import probe_account
from backend.core.ai_protocol.endpoint_security import (
    validate_provider_base_url,
)
from backend.core.ai_providers import (
    list_builtin_providers,
    list_provider_catalog,
)


class AccountSaveRequest(BaseModel):
    """创建/更新账号请求 / Account create-or-update request."""

    id: str | None = None
    name: str
    provider_id: str
    protocol: str = "openai-compatible"
    api_base: str = ""
    api_key: str = ""  # 空值或含 **** 表示不更新现有 key / empty keeps existing
    region: str = ""
    models: list[str] = Field(default_factory=list)
    default_model: str = ""
    enabled: bool = True
    notes: str = ""


class RoleBindingSaveRequest(BaseModel):
    """角色绑定保存请求 / Role-binding save request."""

    bindings: dict  # {main: {primary: {account, model}, fallback: [...]}, ...}


@router.get("/ai/catalog")
async def get_ai_catalog(user: dict = Depends(require_api_super_admin)):
    """返回完整提供商目录（含模型元数据）/ Return the full provider catalog."""
    return success_response(data={"providers": list_provider_catalog()})


@router.get("/ai/accounts")
async def list_ai_accounts(user: dict = Depends(require_api_super_admin)):
    """列出所有已保存的 AI 账号（API Key 脱敏）/ List saved accounts."""
    accounts = await account_store.list_accounts()
    return success_response(data={"accounts": [a.to_public_dict() for a in accounts]})


@router.post("/ai/accounts")
async def save_ai_account(
    body: AccountSaveRequest,
    user: dict = Depends(require_api_super_admin),
):
    """创建或更新一个 AI 账号 / Create or update an account.

    ``api_key`` 为空或含 ``****`` 时保留数据库中原有 key 不覆盖。
    """
    from backend.core.ai_protocol.models import ProtocolFamily
    from backend.core.ai_providers import get_builtin_provider

    # 校验协议族 / validate protocol family
    try:
        ProtocolFamily(body.protocol)
    except ValueError:
        return error_response(f"未知的协议族 / unknown protocol: {body.protocol}")

    provider_id = body.provider_id.strip().lower()
    if provider_id not in {provider.id for provider in list_builtin_providers()}:
        return error_response(
            f"未知的 AI 厂商 / unknown AI provider: {body.provider_id}"
        )

    decl = get_builtin_provider(provider_id)
    if ProtocolFamily(body.protocol) not in decl.supported_families():
        return error_response(
            f"厂商 {decl.id} 不支持协议 / unsupported protocol: {body.protocol}"
        )

    ok, message = validate_provider_base_url(
        decl.id,
        body.api_base.strip(),
        protocol=body.protocol,
    )
    if not ok:
        return error_response(message)

    account_id = (body.id or "").strip()
    existing = await account_store.get_account(account_id) if account_id else None

    # API key 处理：空或脱敏 → 保留原值；新 key 去除首尾空白
    # / keep existing when blank or masked; strip surrounding whitespace from new keys
    api_key = body.api_key.strip()
    if (not api_key or "****" in api_key) and existing is not None:
        api_key = existing.api_key

    account = account_store.ProviderAccount(
        id=account_id,
        name=body.name.strip(),
        provider_id=decl.id,
        protocol=body.protocol,
        api_base=body.api_base.strip(),
        api_key=api_key,
        region=body.region.strip(),
        models=list(body.models),
        default_model=body.default_model.strip(),
        enabled=body.enabled,
        notes=body.notes.strip(),
        created_at=existing.created_at if existing else 0.0,
    )
    saved = await account_store.save_account(account)
    logger.info(
        f"AI 账号已保存 / account saved: {saved.id} ({saved.name}), by={user['sub']}"
    )
    return success_response(data={"account": saved.to_public_dict()})


@router.delete("/ai/accounts/{account_id}")
async def delete_ai_account(
    account_id: str,
    user: dict = Depends(require_api_super_admin),
):
    """删除一个 AI 账号（若被角色引用则拒绝）/ Delete an account."""
    ok = await account_store.delete_account(account_id)
    if not ok:
        return error_response("账号不存在或正被角色绑定引用，无法删除")
    logger.info(f"AI 账号已删除 / account deleted: {account_id}, by={user['sub']}")
    return success_response(data={"deleted": account_id})


@router.post("/ai/accounts/{account_id}/test")
@limiter.limit("10/minute")
async def test_ai_account(
    request: Request,
    account_id: str,
    user: dict = Depends(require_api_super_admin),
):
    """测试已保存账号的连接 / Test a saved account's connection."""
    account = await account_store.get_account(account_id)
    if account is None:
        return error_response("账号不存在")
    result = await probe_account(
        provider_id=account.provider_id,
        protocol=account.protocol,
        api_base=account.api_base,
        api_key=account.api_key,
        model=account.default_model,
    )
    return success_response(data=result)


@router.post("/ai/accounts/{account_id}/models")
@limiter.limit("10/minute")
async def discover_ai_account_models(
    request: Request,
    account_id: str,
    user: dict = Depends(require_api_super_admin),
):
    """发现账号可用模型列表 / Discover models available to an account."""
    account = await account_store.get_account(account_id)
    if account is None:
        return error_response("账号不存在")
    result = await probe_account(
        provider_id=account.provider_id,
        protocol=account.protocol,
        api_base=account.api_base,
        api_key=account.api_key,
    )
    return success_response(data=result)


@router.get("/ai/bindings")
async def get_ai_bindings(user: dict = Depends(require_api_super_admin)):
    """读取角色→账号绑定 / Read role→account bindings."""
    raw = await account_store.get_role_bindings_raw()
    accounts = await account_store.list_accounts()
    return success_response(
        data={
            "bindings": raw,
            "accounts": [a.to_public_dict() for a in accounts],
            "roles": ["main", "summary", "agent_team"],
        }
    )


@router.put("/ai/bindings")
async def save_ai_bindings(
    body: RoleBindingSaveRequest,
    user: dict = Depends(require_api_super_admin),
):
    """保存角色→账号绑定 / Persist role→account bindings."""
    if not isinstance(body.bindings, dict):
        return error_response("bindings 必须是对象")
    accounts = await account_store.list_accounts()
    bindings, error_message = account_store.validate_role_bindings_payload(
        body.bindings,
        {account.id for account in accounts},
    )
    if error_message:
        return error_response(error_message)
    await account_store.save_role_bindings(bindings)
    logger.info(f"AI 角色绑定已更新 / role bindings saved, by={user['sub']}")
    normalized = {role: binding.to_dict() for role, binding in bindings.items()}
    return success_response(data={"bindings": normalized})


# =========================================================================
# AI 调用策略（超时/重试/故障转移）/ AI call-strategy settings
# =========================================================================

AI_STRATEGY_KEYS: list[str] = list(AI_STRATEGY_CONFIG_KEYS)

_AI_STRATEGY_RANGES = {
    "ai_api_timeout_seconds": (1.0, 3600.0),
    "ai_api_max_retries": (0, 20),
    "ai_api_initial_retry_delay_seconds": (0.0, 60.0),
    "ai_api_total_timeout_seconds": (1.0, 7200.0),
    "ai_fallback_max_candidates": (1, 10),
    "context_compression_threshold": (0.1, 1.0),
    "activity_artifact_retention_days": (1, 3650),
}


class AIStrategyRequest(BaseModel):
    """AI 调用策略保存请求 / AI call-strategy save request."""

    ai_api_timeout_seconds: float | None = None
    ai_api_max_retries: int | None = Field(default=None, ge=0, le=20)
    ai_api_initial_retry_delay_seconds: float | None = None
    ai_api_total_timeout_seconds: float | None = None
    ai_fallback_enabled: bool | None = None
    ai_fallback_max_candidates: int | None = None
    ai_fallback_sticky_candidate: bool | None = None
    enable_context_compression: bool | None = None
    context_compression_threshold: float | None = None
    activity_reasoning_capture_enabled: bool | None = None
    activity_request_response_capture_enabled: bool | None = None
    activity_reasoning_provider_allowlist: str | None = None
    activity_reasoning_protocol_allowlist: str | None = None
    activity_artifact_retention_days: int | None = None
    activity_artifact_encryption_key_id: str | None = None
    activity_artifact_super_admin_read_enabled: bool | None = None


@router.get("/ai/settings")
async def get_ai_strategy_settings(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """读取 AI 调用策略（超时/重试/故障转移）/ Read AI call-strategy settings."""
    settings = get_settings()
    result = await db.execute(
        select(AppConfig).where(AppConfig.key_name.in_(AI_STRATEGY_KEYS))
    )
    db_map = {c.key_name: c.key_value for c in result.scalars().all()}
    data = {}
    for key in AI_STRATEGY_KEYS:
        val = db_map.get(key)
        if val is None:
            val = str(getattr(settings, key))
        data[key] = val
    return success_response(data=data)


@router.put("/ai/settings")
async def put_ai_strategy_settings(
    body: AIStrategyRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """保存 AI 调用策略，即时生效 / Persist AI call-strategy settings live.

    写入 AppConfig 并即时更新 Settings 单例，无需重启。
    """
    payload = body.model_dump(exclude_none=True)
    if not payload:
        return error_response("没有需要更新的调用策略参数")

    for key, value in payload.items():
        if key in _AI_STRATEGY_RANGES:
            lo, hi = _AI_STRATEGY_RANGES[key]
            try:
                numeric = float(value)
            except TypeError, ValueError:
                return error_response(f"{key} 取值无效")
            if not (lo <= numeric <= hi):
                return error_response(f"{key} 取值需在 {lo}~{hi} 之间")
        str_val = (
            "true" if value is True else ("false" if value is False else str(value))
        )
        result = await db.execute(select(AppConfig).where(AppConfig.key_name == key))
        cfg = result.scalar_one_or_none()
        if cfg is None:
            db.add(AppConfig(key_name=key, key_value=str_val, description=key))
        else:
            cfg.key_value = str_val
        update_settings_field(key, str_val)

    await db.commit()
    logger.info(
        f"AI 调用策略已更新 / ai strategy saved: {list(payload.keys())}, by={user['sub']}"
    )
    return success_response(data=payload)


# =========================================================================
# 单模型高级覆盖 / Per-model capability & reasoning override
# =========================================================================

_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def _model_override_key(provider: str, model: str) -> str:
    return f"ai_model_override.{provider}.{model}"


class ModelOverrideRequest(BaseModel):
    """单模型高级覆盖请求 / Per-model override request."""

    provider: str
    model: str
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    vision: bool | None = None
    thinking: bool | None = None
    thinking_mode: str | None = None
    effort_enabled: bool | None = None
    reasoning_content: bool | None = None
    temperature_enabled: bool | None = None
    top_p_enabled: bool | None = None
    top_k_enabled: bool | None = None
    effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None


@router.get("/ai/model-override")
async def get_model_override(
    provider: str,
    model: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """读取单个模型的用户覆盖 / Read a single model override."""
    result = await db.execute(
        select(AppConfig.key_value).where(
            AppConfig.key_name == _model_override_key(provider, model)
        )
    )
    raw = result.scalar_one_or_none()
    data = json.loads(raw) if raw else {}
    return success_response(data=data)


@router.put("/ai/model-override")
async def put_model_override(
    body: ModelOverrideRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """保存单个模型的用户覆盖 / Persist a single model override.

    覆盖键为 ai_model_override.<provider>.<model>，role_config 解析时优先于
    内置目录元数据生效。
    """
    if body.effort and body.effort not in _VALID_EFFORTS:
        return error_response(f"无效的思考等级 / invalid effort: {body.effort}")
    if body.thinking_mode and body.thinking_mode not in {"adaptive", "disabled"}:
        return error_response(
            f"无效的思考模式 / invalid thinking mode: {body.thinking_mode}"
        )
    if body.thinking_mode and not body.thinking:
        return error_response("启用思考模式前必须启用 thinking 能力")

    payload: dict[str, Any] = {
        "context_window_tokens": int(body.context_window_tokens or 0),
        "max_output_tokens": int(body.max_output_tokens or 0),
        "capabilities": {
            "vision": bool(body.vision),
            "tools": True,
            "streaming": True,
            "reasoning_content": bool(body.reasoning_content),
            "thinking": bool(body.thinking),
            "effort": bool(body.effort_enabled),
            "temperature": body.temperature_enabled
            if body.temperature_enabled is not None
            else True,
            "top_p": body.top_p_enabled if body.top_p_enabled is not None else True,
            "top_k": bool(body.top_k_enabled),
        },
        "reasoning_params": {
            "max_output_tokens": int(body.max_output_tokens or 4096),
        },
    }
    if body.effort:
        payload["reasoning_params"]["effort"] = body.effort
    if body.thinking_mode:
        payload["reasoning_params"]["thinking"] = {"type": body.thinking_mode}
    if body.temperature is not None:
        payload["reasoning_params"]["temperature"] = body.temperature
    if body.top_p is not None:
        payload["reasoning_params"]["top_p"] = body.top_p
    if body.top_k is not None:
        payload["reasoning_params"]["top_k"] = body.top_k

    key = _model_override_key(body.provider, body.model)
    result = await db.execute(select(AppConfig).where(AppConfig.key_name == key))
    cfg = result.scalar_one_or_none()
    serialized = json.dumps(payload, ensure_ascii=False)
    if cfg is None:
        db.add(AppConfig(key_name=key, key_value=serialized, description=key))
    else:
        cfg.key_value = serialized
    await db.commit()
    logger.info(f"模型覆盖已保存 / model override saved: {key}, by={user['sub']}")
    return success_response(data=payload)


@router.delete("/ai/model-override")
async def delete_model_override(
    provider: str,
    model: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """清除单个模型的用户覆盖，回退到内置/自动元数据 / Clear a model override."""
    from sqlalchemy import delete as sa_delete

    key = _model_override_key(provider, model)
    await db.execute(sa_delete(AppConfig).where(AppConfig.key_name == key))
    await db.commit()
    logger.info(f"模型覆盖已清除 / model override cleared: {key}, by={user['sub']}")
    return success_response(data={"deleted": key})


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
    section_keys = sorted(set(configs).intersection(SECTION_REGISTRY))
    if section_keys:
        return error_response(
            "策略与标签配置节必须通过专用配置接口更新",
            detail=", ".join(section_keys),
        )
    reserved_keys = {
        key
        for key in configs
        if key == "ai_role_bindings" or key.startswith("ai_account.")
    }
    if reserved_keys:
        return error_response(
            "AI 账号与角色绑定必须通过专用配置接口更新",
            detail=", ".join(sorted(reserved_keys)),
        )

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
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """更新策略配置的某个 section（统一节配置存储，PATCH 合并语义）"""
    data = body.data
    if not data:
        return error_response("配置内容不能为空")

    section_key = _STRATEGY_SECTION_KEY_MAP.get(section)
    if section_key is None:
        return error_response(f"未知的策略 section: {section}")

    try:
        result = await section_config_service.save_section(
            db, section_key, data, mode="patch"
        )
        logger.info(
            f"API 更新策略配置: section={section}, changed={result['changed']}, "
            f"by={user['sub']}"
        )
        return success_response(message=f"策略配置 {section} 已更新")
    except ValueError as e:
        logger.warning(f"API 更新策略配置校验失败: section={section}, error={e}")
        return error_response(f"配置校验失败: {e}")
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
                "labels": config.get_labels(),
                "recommendation": config.get_recommendation_settings(),
            }
        )
    except Exception as e:
        logger.error(f"读取标签配置失败: {e}")
        return error_response("读取标签配置失败")


@router.put("/labels")
async def update_labels(
    body: ConfigLabelsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """更新标签定义（全量覆盖，统一节配置存储）"""
    labels = body.labels
    if not labels:
        return error_response("标签列表不能为空")

    try:
        label_definitions = _normalize_label_definitions(labels)
        await section_config_service.save_section(
            db, "label.definitions", label_definitions
        )
        label_service.reload_labels()
        logger.info(f"API 更新标签定义, by={user['sub']}")
        return success_response(message="标签定义已更新")
    except ValueError as e:
        logger.warning(f"API 更新标签定义校验失败: {e}")
        return error_response(f"标签校验失败: {e}")
    except Exception as e:
        logger.error(f"更新标签配置失败: {e}")
        return error_response("更新标签配置失败")


@router.patch("/labels/recommendation")
async def update_label_recommendation(
    body: ConfigLabelRecommendationUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """更新标签推荐设置（统一节配置存储）"""
    recommendation = body.recommendation
    if not recommendation:
        return error_response("推荐设置不能为空")

    try:
        await section_config_service.save_section(
            db, "label.recommendation", recommendation
        )
        logger.info(f"API 更新标签推荐设置, by={user['sub']}")
        return success_response(message="标签推荐设置已更新")
    except ValueError as e:
        logger.warning(f"API 更新标签推荐设置校验失败: {e}")
        return error_response(f"推荐设置校验失败: {e}")
    except Exception as e:
        logger.error(f"更新推荐设置失败: {e}")
        return error_response("更新推荐设置失败")
