"""Sakura AI Reviewer 主应用"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger
import sys
import asyncio

from backend.core.config import get_settings
from backend.models import init_db
from backend.api import webhook
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
    version="2.3.1",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Sakura AI Reviewer",
        "version": "1.0.0",
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
