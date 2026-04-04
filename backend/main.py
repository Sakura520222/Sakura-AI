"""Sakura AI Reviewer 主应用"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
import sys
import asyncio

from backend.core.config import get_settings
from backend.core.bootstrap import BootstrapMiddleware, is_bootstrap_mode
from backend.models import init_db
from backend.webui.routes.setup import router as setup_router
from backend.api import webhook
from backend.webui.routes import webui_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("🚀 Sakura AI Reviewer 启动中...")
    logger.info(f"📊 日志级别: {settings.log_level}")
    logger.info(f"🌐 应用域名: {settings.app_domain}")

    telegram_task = None
    redis_listener_task = None

    if not is_bootstrap_mode():
        # 正常模式：完整启动所有服务
        logger.info(f"🤖 OpenAI模型: {settings.openai_model}")

        # 校验必填配置
        missing = settings.validate_required_fields()
        if missing:
            logger.error(f"❌ 缺少必填配置项: {', '.join(missing)}")
            logger.error("请删除 .setup_complete 并通过 Setup Wizard 重新配置")
        else:
            # 检测默认 JWT 密钥
            if settings.webui_secret_key == "change-me-in-production":
                logger.warning(
                    "⚠️  WebUI JWT 密钥使用默认值！请设置 WEBUI_SECRET_KEY 环境变量，否则令牌可被伪造。"
                )

            # 初始化数据库
            try:
                await init_db()
                logger.info("✅ 数据库初始化成功")
            except Exception as e:
                logger.error(f"❌ 数据库初始化失败: {e}")

            # 从数据库加载动态配置到 Settings 单例
            try:
                from backend.core.config import load_dynamic_configs_to_settings

                await load_dynamic_configs_to_settings()
            except Exception as e:
                logger.warning(f"⚠️ 加载动态配置失败: {e}")

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


# 创建FastAPI应用
app = FastAPI(
    title="Sakura AI Reviewer",
    description="GitHub PR AI代码审查机器人",
    version="2.6.0",
    lifespan=lifespan,
)

# 配置CORS
_allowed_origins = [f"https://{settings.app_domain}"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
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


# WebUI 认证异常处理：页面路由 401 时重定向到登录页
@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/webui"):
        return RedirectResponse(url="/webui/auth/login", status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Sakura AI Reviewer",
        "version": "2.6.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy", "service": "Sakura AI Reviewer"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
