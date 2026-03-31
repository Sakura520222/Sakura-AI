"""Sakura AI Reviewer 主应用"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
import sys
import asyncio

from backend.core.config import get_settings
from backend.models import init_db
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
    logger.info(f"🤖 OpenAI模型: {settings.openai_model}")

    # 检测默认 JWT 密钥
    if settings.webui_secret_key == "change-me-in-production":
        logger.warning("⚠️  WebUI JWT 密钥使用默认值！请设置 WEBUI_SECRET_KEY 环境变量，否则令牌可被伪造。")

    # 初始化数据库
    try:
        await init_db()
        logger.info("✅ 数据库初始化成功")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")

    # 启动 Telegram Bot（后台任务）
    telegram_task = None
    try:
        telegram_task = asyncio.create_task(start_telegram_bot())
        logger.info("✅ Telegram Bot 已启动")
    except Exception as e:
        logger.error(f"❌ Telegram Bot 启动失败: {e}")

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


# 创建FastAPI应用
app = FastAPI(
    title="Sakura AI Reviewer",
    description="GitHub PR AI代码审查机器人",
    version="2.5.1",
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

# 注册路由
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
        "version": "2.5.1",
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
