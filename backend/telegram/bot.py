"""Telegram Bot 主逻辑"""

from telegram import Bot
from telegram.ext import Application, CommandHandler
from loguru import logger

from backend.core.config import get_settings
from backend.telegram.handlers import (
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_recent,
    cmd_myquota,
    cmd_admin_add,
    cmd_admin_remove,
    cmd_user_add,
    cmd_user_remove,
    cmd_repo_add,
    cmd_repo_remove,
    cmd_quota_set,
    cmd_users,
    cmd_repos,
    cmd_review,
    cmd_docs_status,
    cmd_update_docs,
    cmd_code_index,
    cmd_code_status,
)
from backend.telegram.notifications import NotificationSender, set_notification_sender

settings = get_settings()

# 全局 Bot 实例
_telegram_bot: Bot = None
_telegram_app: Application = None


async def start_telegram_bot():
    """启动 Telegram Bot"""
    global _telegram_bot, _telegram_app

    try:
        logger.info("🤖 启动 Telegram Bot...")

        # 创建 Bot 实例
        _telegram_bot = Bot(token=settings.telegram_bot_token)

        # 创建 Application
        _telegram_app = Application.builder().token(settings.telegram_bot_token).build()

        # 注册命令处理器
        _telegram_app.add_handler(CommandHandler("start", cmd_start))
        _telegram_app.add_handler(CommandHandler("help", cmd_help))
        _telegram_app.add_handler(CommandHandler("status", cmd_status))
        _telegram_app.add_handler(CommandHandler("recent", cmd_recent))
        _telegram_app.add_handler(CommandHandler("myquota", cmd_myquota))
        _telegram_app.add_handler(CommandHandler("docs_status", cmd_docs_status))
        _telegram_app.add_handler(CommandHandler("admin_add", cmd_admin_add))
        _telegram_app.add_handler(CommandHandler("admin_remove", cmd_admin_remove))
        _telegram_app.add_handler(CommandHandler("user_add", cmd_user_add))
        _telegram_app.add_handler(CommandHandler("user_remove", cmd_user_remove))
        _telegram_app.add_handler(CommandHandler("repo_add", cmd_repo_add))
        _telegram_app.add_handler(CommandHandler("repo_remove", cmd_repo_remove))
        _telegram_app.add_handler(CommandHandler("quota_set", cmd_quota_set))
        _telegram_app.add_handler(CommandHandler("users", cmd_users))
        _telegram_app.add_handler(CommandHandler("repos", cmd_repos))
        _telegram_app.add_handler(CommandHandler("update_docs", cmd_update_docs))
        _telegram_app.add_handler(CommandHandler("review", cmd_review))
        _telegram_app.add_handler(CommandHandler("code_index", cmd_code_index))
        _telegram_app.add_handler(CommandHandler("code_status", cmd_code_status))

        # 设置通知发送器
        notification_sender = NotificationSender(_telegram_bot)
        set_notification_sender(notification_sender)

        # 启动 Bot（非阻塞）
        await _telegram_app.initialize()
        await _telegram_app.start()
        await _telegram_app.updater.start_polling()

        logger.info("✅ Telegram Bot 启动成功")

    except Exception as e:
        logger.error(f"❌ Telegram Bot 启动失败: {e}")
        raise


async def stop_telegram_bot():
    """停止 Telegram Bot"""
    global _telegram_app

    if _telegram_app:
        try:
            await _telegram_app.updater.stop()
            await _telegram_app.stop()
            await _telegram_app.shutdown()
            logger.info("👋 Telegram Bot 已停止")
        except Exception as e:
            logger.error(f"❌ 停止 Telegram Bot 时出错: {e}")


def get_telegram_bot() -> Bot:
    """获取 Telegram Bot 实例"""
    return _telegram_bot
