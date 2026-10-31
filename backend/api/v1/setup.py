"""API v1 Setup Wizard 端点（免认证，仅在 bootstrap 模式下可用）"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool

from backend.api.v1.deps import limiter
from backend.api.v1.responses import error_response, success_response
from backend.core.bootstrap import (
    get_current_step,
    get_missing_fields,
    is_bootstrap_mode,
)
from backend.core.setup_database import (
    DatabaseSetupError,
    check_database,
    has_setup_database_token,
    repair_database,
    setup_database_status,
)
from backend.core.setup_service import setup_service

router = APIRouter(prefix="/setup", tags=["Setup"])


@router.post("/browser/verify")
@limiter.limit("5/minute")
async def verify_browser_setup(request: Request):
    if not is_bootstrap_mode() or not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    return JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})


@router.get("/browser")
async def browser_setup_state(request: Request):
    from backend.core.config import get_settings

    if not is_bootstrap_mode():
        return {"state": "completed"}
    if not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    settings = get_settings()
    return {
        "state": "in_progress",
        "missing_fields": await get_missing_fields(),
        "configured": {
            key: bool(getattr(settings, key.lower(), None))
            for key in (
                "DATABASE_URL",
                "REDIS_URL",
                "GITHUB_APP_ID",
                "GITHUB_PRIVATE_KEY",
                "GITHUB_OAUTH_CLIENT_ID",
                "GITHUB_OAUTH_CLIENT_SECRET",
                "GITHUB_OAUTH_REDIRECT_URI",
            )
        },
    }


@router.post("/browser/backup")
async def browser_setup_backup(request: Request):
    from backend.webui.routes.setup import inspect_config_backup

    if not is_bootstrap_mode() or not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    return await inspect_config_backup(request)


@router.post("/browser/complete")
@limiter.limit("3/minute")
async def browser_setup_complete(request: Request):
    from backend.webui.routes.setup import complete_setup as complete_browser

    if not is_bootstrap_mode() or not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    return await complete_browser(request)


class TestConnectionRequest(BaseModel):
    """连接测试请求"""

    model_config = ConfigDict(extra="ignore")

    type: str  # database, redis, github, telegram
    # database
    database_url: str | None = None
    # redis
    redis_url: str | None = None
    # github
    app_id: str | None = None
    private_key: str | None = None
    # telegram
    bot_token: str | None = None


class SaveStepRequest(BaseModel):
    """保存配置步骤请求"""

    values: dict[str, str]


class DatabasePreflightRequest(BaseModel):
    database_url: str


class DatabaseRepairRequest(DatabasePreflightRequest):
    username: str
    password: SecretStr
    confirmed: StrictBool = False


def _database_response(result):
    if result["success"]:
        return success_response(data=result)
    return JSONResponse(
        {
            "success": False,
            "error": result["message"],
            "code": result.get("code", "database_connection_failed"),
            "data": result,
        },
        status_code=setup_database_status(result),
    )


@router.post("/database-preflight")
@limiter.limit("10/minute")
async def database_preflight(request: Request, body: DatabasePreflightRequest):
    if not is_bootstrap_mode() or not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    return _database_response(await check_database(body.database_url))


@router.post("/database-repair")
@limiter.limit("3/minute")
async def database_repair(request: Request, body: DatabaseRepairRequest):
    if not is_bootstrap_mode() or not has_setup_database_token(request):
        return error_response("Setup authorization required", status_code=403)
    try:
        return _database_response(
            await repair_database(
                body.database_url,
                body.username,
                body.password.get_secret_value(),
                confirmed=body.confirmed,
            )
        )
    except ValueError:
        return error_response("Invalid database repair request", status_code=400)


class CompleteSetupRequest(BaseModel):
    """完成 Setup 请求；未知旧字段必须被忽略而不是写入 AppConfig。"""

    model_config = ConfigDict(extra="ignore")

    DATABASE_URL: str | None = None
    REDIS_URL: str | None = None
    GITHUB_APP_ID: str | None = None
    GITHUB_PRIVATE_KEY: str | None = None
    GITHUB_WEBHOOK_SECRET: str | None = None
    TELEGRAM_ENABLED: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    EMAIL_ENABLED: str | None = None
    SMTP_HOST: str | None = None
    SMTP_PORT: str | None = None
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str | None = None
    SMTP_FROM_NAME: str | None = None
    SMTP_SECURITY: str | None = None
    NOTIFICATION_MAX_CONCURRENCY: str | None = None
    NOTIFICATION_RETRY_MAX_ATTEMPTS: str | None = None
    NOTIFICATION_RETRY_INITIAL_DELAY_SECONDS: str | None = None
    NOTIFICATION_RETRY_BACKOFF_FACTOR: str | None = None
    NOTIFICATION_RATE_LIMIT_SECONDS: str | None = None
    APP_DOMAIN: str | None = None
    APP_PORT: str | None = None
    LOG_LEVEL: str | None = None
    ADMIN_GITHUB_USERNAME: str | None = None
    ADMIN_TELEGRAM_ID: str | None = None
    GITHUB_OAUTH_CLIENT_ID: str | None = None
    GITHUB_OAUTH_CLIENT_SECRET: str | None = None
    GITHUB_OAUTH_REDIRECT_URI: str | None = None
    EMBEDDING_API_KEY: str | None = None
    EMBEDDING_BASE_URL: str | None = None
    EMBEDDING_MODEL: str | None = None
    EMBEDDING_PROVIDER: str | None = None
    EMBEDDING_DIMENSION: str | None = None
    RERANK_API_KEY: str | None = None
    RERANK_BASE_URL: str | None = None
    RERANK_MODEL: str | None = None
    RERANK_PROVIDER: str | None = None


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

    # Readiness is defined by the shared bootstrap helpers.  Optional
    # notification fields remain visible in ``field_groups`` for old clients,
    # but an absent Telegram token/admin ID must not block Setup completion.
    missing = await get_missing_fields()
    current_step = await get_current_step()

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

    if not result["success"]:
        return error_response(result["message"], status_code=400)
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

        # Validate the complete step before testing or initializing the
        # database.  Step 1 performs initialization below, so relying only on
        # ``save_configs_to_db`` would allow an invalid notification batch to
        # trigger setup side effects first.
        setup_service.validate_config_values(values)

        # 如果包含 DATABASE_URL，先初始化数据库
        if "DATABASE_URL" in values:
            db_url = values["DATABASE_URL"]
            test_result = await setup_service.test_database_connection(db_url)
            if not test_result["success"]:
                return error_response(test_result["message"], status_code=400)
            await setup_service.init_database(db_url)

        saved = await setup_service.save_configs_to_db(values)
        result = {"saved_count": saved}
        if "DATABASE_URL" in values:
            result.update(
                stage="initialized", connected=True, schema_ready=True, initialized=True
            )
        return success_response(
            data=result,
            message=f"已保存 {saved} 项配置",
        )
    except DatabaseSetupError as exc:
        return _database_response(exc.report)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception:
        logger.exception("Setup 配置保存失败 / setup save failed")
        return error_response("配置保存失败，请稍后重试", status_code=500)


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
        if result.get("stage"):
            return _database_response(result)
        return error_response(result["message"], status_code=400)
