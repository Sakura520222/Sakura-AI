"""Sakura AI 主应用"""

import argparse
import asyncio
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

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
from backend.core.build_info import get_build_info
from backend.core.config import Settings, get_sakura_memory_config, get_settings
from backend.core.time_service import (
    SystemClock,
    format_rfc3339,
    get_time_service,
    initialize_time_service,
)
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
_startup_started_instant_utc: datetime | None = None
_startup_finished_instant_utc: datetime | None = None
_startup_started_monotonic: float | None = None
_startup_finished_monotonic: float | None = None


def get_startup_info() -> dict:
    """返回启动时间与运行时长信息，供 /health 端点使用。"""
    service = get_time_service()
    now_mono = service.monotonic()
    started = _startup_finished_at > 0
    finished_instant = _startup_finished_instant_utc
    if finished_instant is None and started:
        finished_instant = datetime.fromtimestamp(_startup_finished_at, tz=UTC)
    uptime_seconds = (
        max(0.0, now_mono - _startup_finished_monotonic)
        if started and _startup_finished_monotonic is not None
        else 0.0
    )
    return {
        "startup_time": (
            format_rfc3339(finished_instant) if started and finished_instant else None
        ),
        "startup_duration_seconds": round(_startup_duration, 2),
        "uptime_seconds": round(uptime_seconds),
        "app_timezone": service.configured_timezone,
        "resolved_timezone": service.resolved_timezone,
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
    global _startup_started_instant_utc, _startup_finished_instant_utc
    global _startup_started_monotonic, _startup_finished_monotonic

    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeSupervisor,
        bind_runtime_supervisor,
        create_registered_background_task,
        quiesce_database_reset_runtime,
        reset_runtime_supervisor,
    )

    runtime_context_token = None
    try:
        # 启动时
        # Startup measurements must not resolve the application timezone before
        # its persisted configuration is available. Use one zone-independent
        # clock for both instants and elapsed time, then initialize TimeService
        # only after the database-backed app_timezone has loaded successfully.
        startup_clock = SystemClock()
        _startup_started_instant_utc = startup_clock.now_utc()
        _startup_started_monotonic = startup_clock.monotonic()
        _startup_started_at = _startup_started_instant_utc.timestamp()
        logger.info("🚀 Sakura AI 启动中...")

        # Install the admission gate before any background task can be created. A
        # reset request may race startup/shutdown, so the supervisor is always
        # present on app.state and is replaced after a previous lifespan quiesced.
        existing_supervisor = getattr(
            getattr(app, "state", None), "database_reset_runtime_supervisor", None
        )
        if existing_supervisor is None or getattr(
            existing_supervisor, "quiesced", False
        ):
            existing_supervisor = DatabaseResetRuntimeSupervisor()
            app.state.database_reset_runtime_supervisor = existing_supervisor
        runtime_context_token = bind_runtime_supervisor(existing_supervisor)
        from backend.webui.sse import sse_manager

        sse_manager.resume()
        # Keep the configured timeout available to the shared quiesce helper. This
        # also lets isolated lifespan tests override ``main.settings`` safely.
        app.state.activity_outbox_shutdown_timeout_seconds = (
            settings.activity_outbox_shutdown_timeout_seconds
        )

        telegram_task = None
        redis_listener_task = None
        outbox_dispatcher = None
        scan_scheduler = None
        quota_reset_scheduler = None
        star_aid_scheduler = None
        update_checker = None

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
                logger.warning(
                    "🔧 因缺少 DATABASE_URL 进入 bootstrap 模式，请访问 /setup"
                )
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

                    await load_dynamic_configs_to_settings(
                        required_keys={"app_timezone"}
                    )
                    # app_timezone is restart-required: this new process reads
                    # it once during bootstrap and freezes the resulting zone.
                    initialize_time_service(settings.app_timezone)
                    logger.info("✅ 配置已从数据库加载到 Settings")
                except Exception:
                    # A configured timezone is part of the process-wide time
                    # contract.  Continuing with the bootstrap zone would make
                    # only some components use the requested setting.
                    logger.exception("❌ 加载应用时区配置失败，停止启动")
                    raise

                # 3.5 加载统一配置节覆盖（strategy.*/label.*）并刷新 facade 单例
                try:
                    from backend.core.config import (
                        reload_label_config,
                        reload_strategy_config,
                    )
                    from backend.core.config_sections import (
                        load_section_configs,
                        migrate_yaml_files_to_db,
                    )
                    from backend.models.database import async_session

                    async with async_session() as session:
                        # 一次性迁移旧 YAML 差异节（DB 无节键时才执行，幂等）
                        await migrate_yaml_files_to_db(session)
                        await load_section_configs(session)
                    # 清除可能已构建的 lru_cache facade 单例，后续读取走新 store
                    reload_strategy_config()
                    reload_label_config()
                    logger.info("✅ 统一配置节存储已加载（strategy/label）")
                except Exception:
                    logger.exception("❌ 加载统一配置节存储失败，停止启动")
                    raise

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
                    ke_config = get_sakura_memory_config().get(
                        "knowledge_extraction", {}
                    )
                    ke_enabled = ke_config.get("enabled", True)
                    ke_interval = ke_config.get("min_reflections", 15)
                    logger.info(
                        f"📚 知识提取配置: enabled={ke_enabled}, interval={ke_interval}"
                    )
                    if ke_enabled and not ke_interval:
                        logger.warning(
                            "⚠️ 知识提取已启用但 min_reflections 为 0 或空，"
                            "请检查 strategy.context_enhancement.sakura_memory 配置"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ 知识提取配置自检失败: {e}")

                if _should_start_background_tasks(settings):
                    # 启动 Telegram Bot（后台任务）
                    try:
                        telegram_task = create_registered_background_task(
                            start_telegram_bot(), "telegram_listener"
                        )
                        logger.info("✅ Telegram Bot 已启动")
                    except Exception as e:
                        logger.error(f"❌ Telegram Bot 启动失败: {e}")

                    # 启动 Redis Pub/Sub 监听（SSE 多进程支持）
                    try:
                        from backend.webui.sse import start_redis_listener

                        redis_listener_task = create_registered_background_task(
                            start_redis_listener(), "sse_redis_listener"
                        )
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
                                    artifact_purge_interval_seconds=settings.activity_artifact_purge_interval_seconds,
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
                        app.state.scan_scheduler = scan_scheduler
                        existing_supervisor.register_scheduler(scan_scheduler)
                    except Exception as e:
                        logger.error(f"❌ 仓库扫描调度器启动失败: {e}")

                    # 启动配额重置调度器
                    try:
                        from backend.services.quota_scheduler import QuotaResetScheduler

                        quota_reset_scheduler = QuotaResetScheduler()
                        quota_reset_scheduler.start()
                        app.state.quota_reset_scheduler = quota_reset_scheduler
                        existing_supervisor.register_scheduler(quota_reset_scheduler)
                    except Exception as e:
                        logger.error(f"❌ 配额重置调度器启动失败: {e}")

                    # 启动仓库互助调度器
                    try:
                        from backend.services.star_aid_scheduler import StarAidScheduler

                        star_aid_scheduler = StarAidScheduler()
                        star_aid_scheduler.start()
                        app.state.star_aid_scheduler = star_aid_scheduler
                        existing_supervisor.register_scheduler(star_aid_scheduler)
                    except Exception as e:
                        logger.error(f"仓库互助调度器启动失败: {e}")

                    # 启动更新检查调度器（Slice 2）—— 唯一实例挂 app.state，供手动端点共用
                    try:
                        from backend.services.update_checker import UpdateChecker

                        update_checker = UpdateChecker()
                        update_checker.start()
                        app.state.update_checker = update_checker
                        existing_supervisor.register_scheduler(update_checker)
                        logger.info("✅ 更新检查调度器已启动（60min 周期）")
                    except Exception as e:
                        logger.error(f"❌ 更新检查调度器启动失败: {e}")
                else:
                    logger.warning("🧪 本地开发模式：已跳过后台任务启动")
        else:
            logger.warning("🔧 Bootstrap 模式：仅 Setup Wizard 可用")
            logger.info("请访问 /setup 完成初始配置")
            # 生成 Setup Token：用户需从日志中获取 Token 才能访问 Setup Wizard
            generate_setup_token()

        # 记录启动完成时间
        _startup_finished_instant_utc = startup_clock.now_utc()
        _startup_finished_monotonic = startup_clock.monotonic()
        _startup_finished_at = _startup_finished_instant_utc.timestamp()
        _startup_duration = max(
            0.0,
            _startup_finished_monotonic
            - (_startup_started_monotonic or _startup_finished_monotonic),
        )
        logger.info(
            "✅ Sakura AI 启动完成，耗时 {}",
            _format_duration(_startup_duration),
        )

        yield

        # 关闭时
        logger.info("👋 Sakura AI 关闭中...")

        # Close admission and await all DB-backed schedulers/workers before the
        # remaining clients are torn down. The reset path calls the same helper;
        # it is idempotent after a successful quiesce.
        try:
            await quiesce_database_reset_runtime(app)
        except Exception as exc:
            logger.error(
                "❌ 运行时静默未完成，应用关闭将继续但数据库重置必须停止: {}",
                exc,
            )

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

        # Scheduler/worker/SSE/Outbox handles were stopped by the shared runtime
        # supervisor above. Keep local variables for startup diagnostics and for
        # backwards-compatible monkeypatches in isolated lifespan tests.
    finally:
        if runtime_context_token is not None:
            reset_runtime_supervisor(runtime_context_token)


# 创建FastAPI应用
app = FastAPI(
    title="Sakura AI",
    description="GitHub AI代码审查机器人",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def bind_database_reset_runtime(request: Request, call_next):
    """将每个请求绑定到自己的 app supervisor。

    Worker submission functions不携带 ``Request``，但它们在请求创建的
    asyncio task 中运行，因此会继承此 contextvars binding。多 app 测试/嵌入
    场景不会再依赖 module-global active supervisor。
    """

    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeAdmissionClosed,
        DatabaseResetRuntimeSupervisor,
        bind_runtime_supervisor,
        reset_runtime_supervisor,
    )

    supervisor = getattr(request.app.state, "database_reset_runtime_supervisor", None)
    if supervisor is None:
        supervisor = DatabaseResetRuntimeSupervisor()
        request.app.state.database_reset_runtime_supervisor = supervisor
    try:
        supervisor.ensure_admission("http.request")
    except DatabaseResetRuntimeAdmissionClosed:
        return JSONResponse(
            status_code=503,
            content={"detail": "数据库重置正在进行，请稍后重试"},
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        )
    token = bind_runtime_supervisor(supervisor)
    try:
        return await call_next(request)
    finally:
        reset_runtime_supervisor(token)


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
        "build": get_build_info(),
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


def _parse_launch_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.main",
        description="Sakura AI 启动器：监督循环 + 应用子进程",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="以应用子进程模式运行（由监督循环拉起，一般不手动使用）",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument(
        "--port", type=int, default=None, help="监听端口，默认读取配置 app_port"
    )
    parser.add_argument("--log-level", default=None, help="日志级别，默认读取配置")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="关闭代码热重载（子进程内模块级 reload）",
    )
    return parser.parse_args(argv)


def _run_serve(args: argparse.Namespace) -> int:
    """应用子进程：单进程 uvicorn，以退出码向监督者表达重启意图。"""

    import uvicorn

    from backend.core import server_runtime

    if not args.no_reload:
        from backend.core.hot_reload import start_reload_watcher

        start_reload_watcher(Path(__file__).resolve().parent.parent)
        logger.info("代码热重载已开启（模块级 reload，进程不重启）")

    config = uvicorn.Config(
        "backend.main:app",
        host=args.host,
        port=args.port if args.port is not None else settings.app_port,
        log_level=(args.log_level or settings.log_level).lower(),
        log_config=None,
        timeout_graceful_shutdown=15,
    )
    server = uvicorn.Server(config)
    server_runtime.register_server(server)
    server.run()
    if server_runtime.restart_requested():
        return server_runtime.RESTART_EXIT_CODE
    return 0


def _spawn_child(args: argparse.Namespace) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "backend.main",
        "--serve",
        "--host",
        args.host,
    ]
    if args.port is not None:
        command += ["--port", str(args.port)]
    if args.log_level is not None:
        command += ["--log-level", args.log_level]
    if args.no_reload:
        command += ["--no-reload"]
    return subprocess.Popen(command)


def _run_supervisor(
    args: argparse.Namespace,
    *,
    spawn: Callable[[argparse.Namespace], subprocess.Popen] = _spawn_child,
    poll_interval: float = 0.2,
) -> int:
    """监督循环：拉起应用子进程，按约定退出码重启。

    代码热重载在应用子进程内完成（模块级 reload，见
    backend/core/hot_reload.py）；监督循环只负责应用主动请求的重启
    （Setup 完成、管理员重启按钮 → RESTART_EXIT_CODE）。uvicorn 自带的
    --reload reloader 只响应文件变化、不感知应用的重启请求，worker
    退出后既不会被拉起也不会退出，因此保留这层监督。
    """

    from backend.core import server_runtime

    while True:
        proc = spawn(args)
        logger.info("应用子进程已启动 (pid={})", proc.pid)
        exit_code: int | None = None
        try:
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    break
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止应用子进程...")
            _terminate_child(proc)
            return 130
        if exit_code == server_runtime.RESTART_EXIT_CODE:
            logger.info("应用请求重启，正在重新拉起...")
            continue
        return exit_code


def _terminate_child(proc: subprocess.Popen) -> None:
    """优雅请求子进程停机；超时后强杀兜底（与 uvicorn 停机超时对齐）。"""

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("应用子进程停机超时，强制结束")
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    _launch_args = _parse_launch_args(sys.argv[1:])
    if _launch_args.serve:
        sys.exit(_run_serve(_launch_args))
    sys.exit(_run_supervisor(_launch_args))