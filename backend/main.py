"""Sakura AI Reviewer 主应用"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
import sys
import asyncio

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.bootstrap import (
    BootstrapMiddleware,
    is_bootstrap_mode,
    read_connection_config,
)
from backend.webui.routes.setup import router as setup_router
from backend.api import webhook
from backend.webui.routes import webui_router
from backend.webui.deps import is_webui_request, error_page, toast_redirect
from backend.webui.auth import decode_access_token
from backend.api.v1 import api_v1_router
from backend.api.v1.deps import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from backend.telegram import start_telegram_bot, stop_telegram_bot

# 配置日志
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO",
)
logger.add("logs/app.log", rotation="500 MB", retention="10 days", level="DEBUG")

settings = get_settings()


def _get_allowed_origins(app_settings: Settings) -> list[str]:
    """构造 CORS origin 列表。开发模式下放行本地调试地址。"""
    origins = {f"https://{app_settings.app_domain}"}
    if app_settings.is_development:
        port = app_settings.app_port
        origins.update(
            {
                f"http://localhost:{port}",
                f"http://127.0.0.1:{port}",
                f"https://localhost:{port}",
                f"https://127.0.0.1:{port}",
            }
        )
    return sorted(origins)


def _should_start_background_tasks(app_settings: Settings) -> bool:
    """本地调试 Setup Wizard 时可关闭有外部副作用的后台任务。"""
    return not (
        app_settings.sakura_skip_background_tasks or app_settings.sakura_dev_bootstrap
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("🚀 Sakura AI Reviewer 启动中...")

    telegram_task = None
    redis_listener_task = None
    scan_scheduler = None
    quota_reset_scheduler = None

    if not is_bootstrap_mode():
        # 正常模式：完整启动所有服务
        # 1. 从 connection.json 读取 DATABASE_URL 并设置到 Settings
        conn_config = read_connection_config()
        database_url = conn_config.get("database_url", "")
        if database_url:
            settings.database_url = database_url
            logger.info("📊 从 connection.json 加载 DATABASE_URL")
        else:
            logger.warning(
                "⚠️ connection.json 中无 DATABASE_URL，尝试从 Settings 默认值加载"
            )
            database_url = settings.database_url

        if not database_url:
            logger.error(
                "❌ 无法获取 DATABASE_URL，请检查 config/connection.json 或访问 /setup 完成初始配置"
            )
            # 无法连接数据库，进入 bootstrap 模式引导用户配置
            logger.warning("🔧 因缺少 DATABASE_URL 进入 bootstrap 模式，请访问 /setup")
        else:
            # 2. 初始化数据库
            try:
                from backend.models import init_db

                await init_db()
                logger.info("✅ 数据库初始化成功")
            except Exception as e:
                logger.error(f"❌ 数据库初始化失败: {e}")

            # 3. 从数据库加载全部配置到 Settings 单例
            try:
                from backend.core.config import load_dynamic_configs_to_settings

                await load_dynamic_configs_to_settings()
                logger.info("✅ 配置已从数据库加载到 Settings")
            except Exception as e:
                logger.warning(f"⚠️ 加载配置失败: {e}")

            # 打印关键配置（在动态配置加载后，确保显示实际值）
            logger.info(f"📊 日志级别: {settings.log_level}")
            logger.info(f"🌐 应用域名: {settings.app_domain}")
            logger.info(f"🤖 OpenAI模型: {settings.openai_model}")

            # 检测默认 JWT 密钥（必须在动态配置加载后检查）
            if settings.webui_secret_key == "change-me-in-production":
                logger.warning(
                    "⚠️  WebUI JWT 密钥使用默认值！请通过 WebUI 配置页面设置 WEBUI_SECRET_KEY。"
                )

            # 4. 动态配置加载后再次校验必填字段（仅警告，不阻止启动）
            missing = settings.validate_required_fields()
            if missing:
                logger.warning(
                    f"⚠️ 以下配置项未设置: {', '.join(missing)}，部分功能可能不可用"
                )

            if _should_start_background_tasks(settings):
                # 启动 Telegram Bot（后台任务）
                try:
                    telegram_task = asyncio.create_task(start_telegram_bot())
                    logger.info("✅ Telegram Bot 已启动")
                except Exception as e:
                    logger.error(f"❌ Telegram Bot 启动失败: {e}")

                # 启动 Redis Pub/Sub 监听（SSE 多进程支持）
                try:
                    from backend.webui.sse import start_redis_listener

                    redis_listener_task = asyncio.create_task(start_redis_listener())
                    logger.info("✅ SSE Redis Pub/Sub 监听已启动")
                except Exception as e:
                    logger.error(f"❌ SSE Redis Pub/Sub 监听启动失败: {e}")

                # 启动仓库扫描调度器
                try:
                    from backend.services.scan_scheduler import ScanScheduler

                    scan_scheduler = ScanScheduler()
                    scan_scheduler.start()
                except Exception as e:
                    logger.error(f"❌ 仓库扫描调度器启动失败: {e}")

                # 启动配额重置调度器
                try:
                    from backend.services.quota_scheduler import QuotaResetScheduler

                    quota_reset_scheduler = QuotaResetScheduler()
                    quota_reset_scheduler.start()
                except Exception as e:
                    logger.error(f"❌ 配额重置调度器启动失败: {e}")
            else:
                logger.warning("🧪 本地开发模式：已跳过后台任务启动")
    else:
        logger.warning("🔧 Bootstrap 模式：仅 Setup Wizard 可用")
        logger.info("请访问 /setup 完成初始配置")

    yield

    # 关闭时
    logger.info("👋 Sakura AI Reviewer 关闭中...")

    # 关闭服务客户端（嵌入服务和重排序服务）
    from backend.services.embedding_service import (
        close_embedding_service,
        close_reranker_service,
    )

    try:
        await close_embedding_service()
        await close_reranker_service()
        logger.info("✅ 服务客户端已关闭")
    except Exception as e:
        logger.error(f"❌ 关闭服务客户端时出错: {e}")

    # 停止 Telegram Bot
    try:
        await stop_telegram_bot()
        if telegram_task:
            telegram_task.cancel()
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.error(f"❌ 停止 Telegram Bot 时出错: {e}")

    # 停止 SSE Redis Pub/Sub 监听
    if redis_listener_task:
        redis_listener_task.cancel()
        try:
            await redis_listener_task
        except asyncio.CancelledError:
            pass

    # 停止仓库扫描调度器
    if scan_scheduler:
        scan_scheduler.stop()

    # 停止配额重置调度器
    if quota_reset_scheduler:
        quota_reset_scheduler.stop()


# 创建FastAPI应用
app = FastAPI(
    title="Sakura AI Reviewer",
    description="GitHub AI代码审查机器人",
    version=__version__,
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(settings),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bootstrap 中间件（CORS 之后、路由之前）
app.add_middleware(BootstrapMiddleware)

# 注册路由
app.include_router(setup_router)
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])
app.include_router(webui_router)
app.include_router(api_v1_router, prefix="/api/v1", tags=["API v1"])

# 限流：注册 slowapi 状态 + 异常处理
app.state.limiter = limiter
_WEBUI_RATE_LIMIT_JSON_SUFFIXES = frozenset(
    {
        "/passkey/options",
        "/passkey/verify",
        "/passkeys/register/options",
        "/passkeys/register/verify",
    }
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded):
    """Return WebUI-friendly rate limit feedback instead of raw JSON pages."""
    path = request.url.path
    if is_webui_request(request):
        message = "toast.rate_limit_exceeded"
        if request.headers.get("hx-request") == "true":
            return JSONResponse(
                status_code=429,
                content={"success": False, "message": message, "data": None},
                headers={"HX-Redirect": f"{path}?_toast={message}&_toast_type=error"},
            )
        is_json_endpoint = any(
            path.endswith(suffix) for suffix in _WEBUI_RATE_LIMIT_JSON_SUFFIXES
        )
        if is_json_endpoint or "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                status_code=429,
                content={"success": False, "message": message, "data": None},
            )
        referer = request.headers.get("referer")
        redirect_url = (
            referer if referer and referer.startswith(str(request.base_url)) else "/"
        )
        return toast_redirect(redirect_url, message, "error", status_code=303)
    return await _rate_limit_exceeded_handler(request, exc)


# WebUI 认证异常处理：页面路由 401 时重定向到登录页
def _get_webui_error_user(request: Request) -> dict | None:
    token = request.cookies.get("webui_token")
    if not token:
        return None

    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    if not payload:
        return None

    return {
        "sub": payload.get("sub") or "",
        "role": payload.get("role", "user"),
        "user_id": payload.get("user_id"),
        "github_id": payload.get("github_id"),
        "avatar_url": payload.get("avatar_url"),
    }


@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and is_webui_request(request):
        return RedirectResponse(url="/auth/login", status_code=302)
    if exc.status_code == 428 and is_webui_request(request):
        return RedirectResponse(
            url="/settings/?_toast=MFA%20enrollment%20required&_toast_type=error",
            status_code=302,
        )
    if is_webui_request(request):
        return error_page(
            request,
            status_code=exc.status_code,
            title="请求无法完成",
            message=str(exc.detail),
            user=_get_webui_error_user(request),
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy", "service": "Sakura AI Reviewer"}


# Catch-all: 浏览器访问不存在的路径时自动跳转主页（API 请求仍返回 JSON 404）
@app.get("/{path:path}", include_in_schema=False)
async def webui_fallback(request: Request, path: str):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse(url="/", status_code=302)
    raise HTTPException(status_code=404, detail="Not Found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
