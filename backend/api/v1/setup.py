"""API v1 Setup Wizard 端点（免认证，仅在 bootstrap 模式下可用）"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from typing import Optional

from backend.core.bootstrap import is_bootstrap_mode
from backend.core.setup_service import setup_service

from backend.api.v1.responses import success_response, error_response
from backend.api.v1.deps import limiter

router = APIRouter(prefix="/setup", tags=["Setup"])


class TestConnectionRequest(BaseModel):
    """连接测试请求"""

    model_config = ConfigDict(extra="ignore")

    type: str  # database, redis, github, telegram
    # database
    database_url: Optional[str] = None
    # redis
    redis_url: Optional[str] = None
    # github
    app_id: Optional[str] = None
    private_key: Optional[str] = None
    # telegram
    bot_token: Optional[str] = None


class SaveStepRequest(BaseModel):
    """保存配置步骤请求"""

    values: dict[str, str]


class CompleteSetupRequest(BaseModel):
    """完成 Setup 请求；未知旧字段必须被忽略而不是写入 AppConfig。"""

    model_config = ConfigDict(extra="ignore")

    DATABASE_URL: Optional[str] = None
    REDIS_URL: Optional[str] = None
    GITHUB_APP_ID: Optional[str] = None
    GITHUB_PRIVATE_KEY: Optional[str] = None
    GITHUB_WEBHOOK_SECRET: Optional[str] = None
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    APP_DOMAIN: Optional[str] = None
    APP_PORT: Optional[str] = None
    LOG_LEVEL: Optional[str] = None
    ADMIN_GITHUB_USERNAME: Optional[str] = None
    ADMIN_TELEGRAM_ID: Optional[str] = None
    GITHUB_OAUTH_CLIENT_ID: Optional[str] = None
    GITHUB_OAUTH_CLIENT_SECRET: Optional[str] = None
    GITHUB_OAUTH_REDIRECT_URI: Optional[str] = None
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_BASE_URL: Optional[str] = None
    EMBEDDING_MODEL: Optional[str] = None
    EMBEDDING_PROVIDER: Optional[str] = None
    EMBEDDING_DIMENSION: Optional[str] = None
    RERANK_API_KEY: Optional[str] = None
    RERANK_BASE_URL: Optional[str] = None
    RERANK_MODEL: Optional[str] = None
    RERANK_PROVIDER: Optional[str] = None


def _check_bootstrap():
    """检查是否处于 bootstrap 模式"""
    if not is_bootstrap_mode():
        return error_response("系统已完成初始化，Setup Wizard 不可用", status_code=403)
    return None


@router.get("/state")
async def get_setup_state():
    """获取当前 Setup 状态"""
    from backend.core.setup_service import ENV_FIELD_GROUPS

    if not is_bootstrap_mode():
        return success_response(
            data={
                "state": "completed",
                "current_step": -1,
                "missing_fields": [],
            }
        )

    # 获取缺失字段
    from backend.core.config import get_settings

    settings = get_settings()
    missing = []
    for group, fields in ENV_FIELD_GROUPS.items():
        for field in fields:
            val = getattr(settings, field.lower(), None)
            if not val:
                missing.append(field)

    # 确定当前步骤
    step_order = ["database", "github", "ai", "rag", "admin"]
    current_step = 0
    for i, step in enumerate(step_order):
        step_fields = ENV_FIELD_GROUPS.get(step, [])
        if any(f in missing for f in step_fields):
            current_step = i
            break
    else:
        current_step = len(step_order) - 1

    return success_response(
        data={
            "state": "in_progress",
            "current_step": current_step,
            "missing_fields": missing,
            "field_groups": ENV_FIELD_GROUPS,
        }
    )


@router.post("/test-connection")
@limiter.limit("10/minute")
async def test_connection(request: Request, body: TestConnectionRequest):
    """测试各类连接配置"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    if body.type == "database":
        result = await setup_service.test_database_connection(body.database_url or "")
    elif body.type == "redis":
        result = await setup_service.test_redis_connection(body.redis_url or "")
    elif body.type == "github":
        result = await setup_service.test_github_app(
            body.app_id or "", body.private_key or ""
        )
    elif body.type == "telegram":
        result = await setup_service.test_telegram_bot(body.bot_token or "")
    else:
        return error_response(f"不支持的测试类型: {body.type}", status_code=400)

    return success_response(data=result)


@router.get("/ai-providers")
async def get_ai_providers():
    """获取内置 AI 厂商列表。"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error
    return success_response(data={"providers": setup_service.list_ai_providers()})


@router.post("/save-step")
async def save_step(body: SaveStepRequest):
    """保存单步配置"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    try:
        values = body.values

        # 如果包含 DATABASE_URL，先初始化数据库
        if "DATABASE_URL" in values:
            db_url = values["DATABASE_URL"]
            test_result = await setup_service.test_database_connection(db_url)
            if not test_result["success"]:
                return error_response(test_result["message"], status_code=400)
            await setup_service.init_database(db_url)

        saved = await setup_service.save_configs_to_db(values)
        return success_response(
            data={"saved_count": saved},
            message=f"已保存 {saved} 项配置",
        )
    except Exception as e:
        return error_response(f"保存失败: {e}", status_code=500)


@router.post("/complete")
@limiter.limit("3/minute")
async def complete_setup(request: Request, body: CompleteSetupRequest):
    """完成 Setup 全流程"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    all_config = {k: str(v) for k, v in body.model_dump().items() if v is not None}

    result = await setup_service.complete_setup(all_config)

    if result["success"]:
        # 异步触发重启（延迟 2 秒）
        import asyncio

        asyncio.get_running_loop().call_later(2, setup_service.trigger_restart)
        return success_response(data=result, message=result["message"])
    else:
        return error_response(result["message"], status_code=400)
