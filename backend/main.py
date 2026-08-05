"""Sakura AI 主应用"""

import asyncio
import time
from contextlib import asynccontextmanager

from backend.core.logging_bridge import configure_logging

configure_logging()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend import __version__
from backend.api import webhook
from backend.api.v1 import api_v1_router
from backend.api.v1.deps import limiter
from backend.core.access_log import install_quiet_successful_access_filter
from backend.core.bootstrap import (
    BootstrapMiddleware,
    generate_setup_token,
    is_bootstrap_mode,
    read_connection_config,
)
from backend.core.config import Settings, get_settings
from backend.telegram import start_telegram_bot, stop_telegram_bot
from backend.webui.auth import decode_access_token
from backend.webui.deps import (
    error_page,
    is_webui_request,
    toast_redirect,
)
from backend.webui.routes import webui_router
from backend.webui.routes.setup import router as setup_router

install_quiet_successful_access_filter()

settings = get_settings()

# 启动耗时记录（由 lifespan 写入，/health 端点读取）
_startup_started_at: float = 0.0
_startup_finished_at: float = 0.0
_startup_duration: float = 0.0


def get_startup_info() -> dict:
    """返回启动时间与运行时长信息，供 /health 端点使用。"""
    now = time.time()
    uptime_seconds = now - _startup_finished_at if _startup_finished_at else 0.0
    return {
        "startup_time": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_startup_finished_at))
            if _startup_finished_at
            else None
        ),
        "startup_duration_seconds": round(_startup_duration, 2),
        "uptime_seconds": round(uptime_seconds),
    }


def get_system_info_dict() -> dict:
    """返回系统信息（含格式化字段），供 Dashboard API/WebUI 使用。"""
    info = get_startup_info()
    uptime_seconds = info["uptime_seconds"]
    info["uptime_formatted"] = _format_duration(uptime_seconds)
    info["startup_duration_formatted"] = _format_duration(
        info["startup_duration_seconds"]
    )
    info["version"] = __version__
    return info


def _format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的时长字符串。"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:.0f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m {secs:.0f}s"


def _get_allowed_origins(app_settings: Settings) -> list[str]:
    """构造 CORS origin 列表。开发模式下放行本地调试地址。"""
    origins = {f"https://{app_settings.sanitized_app_domain}"}
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


async def _shutdown_activity_outbox(app: FastAPI) -> None:
    """停止 Outbox dispatcher，并在必要时有界取消其后台任务。"""
    outbox_task = getattr(app.state, "activity_outbox_task", None)
    if not outbox_task:
        return

    dispatcher = getattr(app.state, "activity_outbox_dispatcher", None)
    if dispatcher:
        dispatcher.stop()

    try:
        await asyncio.wait_for(
            outbox_task,
            timeout=settings.activity_outbox_shutdown_timeout_seconds,
        )
    except TimeoutError:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
    except asyncio.CancelledError:
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _startup_started_at, _startup_finished_at, _startup_duration

    # 启动时
    _startup_started_at = time.time()
    logger.info("🚀 Sakura AI 启动中...")

    telegram_task = None
    redis_listener_task = None
    outbox_dispatcher = None
    scan_scheduler = None
    quota_reset_scheduler = None
    star_aid_scheduler = None

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
            except Exception:
                logger.exception("❌ 数据库初始化失败，停止启动")
                raise

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

            # 知识提取配置自检 / Knowledge extraction config self-check
            try:
                ke_enabled = settings.sakura_knowledge_extraction_enabled
                ke_interval = settings.sakura_extraction_min_reflections
                logger.info(
                    f"📚 知识提取配置: enabled={ke_enabled}, interval={ke_interval}"
                )
                if ke_enabled and not ke_interval:
                    logger.warning(
                        "⚠️ 知识提取已启用但 extraction_interval 为 0 或空，将使用默认值 10"
                    )
            except Exception as e:
                logger.warning(f"⚠️ 知识提取配置自检失败: {e}")

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

                # 自愈活动 cursor signing secret（已部署实例可能 Setup 时未生成）
                try:
                    from backend.core.setup_service import (
                        ensure_activity_cursor_signing_secret,
                    )

                    await ensure_activity_cursor_signing_secret()
                except Exception as e:
                    logger.warning(f"⚠️ 活动 cursor signing secret 自愈失败: {e}")

                # 启动活动观测 Outbox dispatcher（授权器由应用注入）
                try:
                    from backend.models import database as db_module
                    from backend.services.activity_observability.access_service import (
                        ActivityAccessService,
                        CursorConfig,
                    )
                    from backend.services.activity_observability.outbox_service import (
                        OutboxDispatcher,
                        OutboxDispatcherConfig,
                        OutboxRetryPolicy,
                    )

                    scope_authorizer = getattr(
                        app.state, "activity_scope_authorizer", None
                    )
                    if scope_authorizer is None:
                        from backend.services.activity_observability.legacy_scope_authorizer import (
                            LegacyRepositoryScopeAuthorizer,
                        )

                        scope_authorizer = LegacyRepositoryScopeAuthorizer()
                        app.state.activity_scope_authorizer = scope_authorizer
                    if (
                        settings.activity_cursor_signing_secret
                        and db_module.async_session
                        and scope_authorizer
                    ):
                        access_service = ActivityAccessService(
                            authorizer=scope_authorizer,
                            cursor_config=CursorConfig(
                                secret=settings.activity_cursor_signing_secret,
                                ttl_seconds=settings.activity_cursor_ttl_seconds,
                                page_size=settings.activity_cursor_page_size,
                            ),
                        )
                        outbox_dispatcher = OutboxDispatcher(
                            db_module.async_session,
                            authorizer=scope_authorizer,
                            config=OutboxDispatcherConfig(
                                batch_size=settings.activity_outbox_batch_size,
                                poll_interval_seconds=settings.activity_outbox_poll_interval_seconds,
                                claim_timeout_seconds=settings.activity_outbox_claim_timeout_seconds,
                                retry_policy=OutboxRetryPolicy(
                                    max_attempts=settings.activity_outbox_retry_max_attempts,
                                    initial_delay_seconds=settings.activity_outbox_retry_initial_delay_seconds,
                                    backoff_factor=settings.activity_outbox_retry_backoff_factor,
                                    max_delay_seconds=settings.activity_outbox_retry_max_delay_seconds,
                                ),
                            ),
                        )
                        app.state.activity_access_service = access_service
                        app.state.activity_outbox_dispatcher = outbox_dispatcher
                        app.state.activity_outbox_task = asyncio.create_task(
                            outbox_dispatcher.run()
                        )
                        logger.info("✅ 活动观测 Outbox dispatcher 已启动")
                    elif not settings.activity_cursor_signing_secret:
                        logger.warning(
                            "活动 cursor signing secret 缺失，跳过新版 dispatcher"
                        )
                    else:
                        logger.warning(
                            "活动 repository scope authorizer 未注入，跳过新版 dispatcher"
                        )
                except Exception as e:
                    logger.error(f"❌ 活动观测 Outbox dispatcher 启动失败: {e}")

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

                # 启动仓库互助调度器
                try:
                    from backend.services.star_aid_scheduler import StarAidScheduler

                    star_aid_scheduler = StarAidScheduler()
                    star_aid_scheduler.start()
                except Exception as e:
                    logger.error(f"仓库互助调度器启动失败: {e}")
            else:
                logger.warning("🧪 本地开发模式：已跳过后台任务启动")
    else:
        logger.warning("🔧 Bootstrap 模式：仅 Setup Wizard 可用")
        logger.info("请访问 /setup 完成初始配置")
        # 生成 Setup Token：用户需从日志中获取 Token 才能访问 Setup Wizard
        generate_setup_token()

    # 记录启动完成时间
    _startup_finished_at = time.time()
    _startup_duration = _startup_finished_at - _startup_started_at
    logger.info(
        "✅ Sakura AI 启动完成，耗时 {}",
        _format_duration(_startup_duration),
    )

    yield

    # 关闭时
    logger.info("👋 Sakura AI 关闭中...")

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

    # 停止活动观测 Outbox dispatcher
    await _shutdown_activity_outbox(app)

    # 停止配额重置调度器
    if quota_reset_scheduler:
        quota_reset_scheduler.stop()

    # 停止仓库互助调度器
    if star_aid_scheduler:
        star_aid_scheduler.stop()


# 创建FastAPI应用
app = FastAPI(
    title="Sakura AI",
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


# 健康检查
@app.get("/health")
async def health():
    """健康检查"""
    startup_info = get_startup_info()
    return {
        "status": "healthy",
        "service": "Sakura AI",
        "version": __version__,
        **startup_info,
    }


# 注册路由
app.include_router(setup_router)
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])
app.include_router(webui_router)
app.include_router(api_v1_router, prefix="/api/v1", tags=["API v1"])

# 限流：注册 slowapi 状态 + 异常处理
app.state.limiter = limiter
app.state.activity_scope_authorizer = None
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
    return _rate_limit_exceeded_handler(request, exc)


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


# Catch-all: 浏览器访问不存在的路径时自动跳转主页（API 请求仍返回 JSON 404）
@app.get("/{path:path}", include_in_schema=False)
async def webui_fallback(request: Request, path: str):
    # Bootstrap 模式下：/setup → catch-all 重定向到 / → 中间件重定向到 /setup → 循环
    if path == "setup" or path.startswith("setup/"):
        raise HTTPException(status_code=404, detail="Not Found")
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
        log_config=None,
        timeout_graceful_shutdown=15,
    )
