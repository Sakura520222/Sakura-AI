"""Telegram Bot 主逻辑"""

import httpx
from telegram import Bot, BotCommand
from telegram.error import NetworkError, TimedOut
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
    cmd_sign,
    cmd_repo_subscribe,
    cmd_repo_unsubscribe,
    cmd_my_subscriptions,
)
from backend.telegram.notifications import NotificationSender, set_notification_sender

settings = get_settings()

# 全局 Bot 实例
_telegram_bot: Bot = None
_telegram_app: Application = None


async def register_bot_commands(bot: Bot):
    """注册 Telegram Bot 命令菜单"""
    commands = [
        # 基础命令（所有人可用）
        BotCommand("start", "🚀 启动 Bot"),
        BotCommand("help", "📖 使用帮助"),
        BotCommand("sign", "📝 注册账号"),
        BotCommand("status", "📊 系统状态"),
        BotCommand("recent", "🕐 最近记录"),
        BotCommand("myquota", "💎 我的配额"),
        BotCommand("docs_status", "📄 文档索引状态"),
        BotCommand("code_status", "💻 代码索引状态"),
        BotCommand("repo_subscribe", "📌 订阅仓库"),
        BotCommand("repo_unsubscribe", "❌ 取消订阅"),
        BotCommand("my_subscriptions", "📋 我的订阅"),
        # 管理员命令
        BotCommand("user_add", "➕ 添加用户"),
        BotCommand("user_remove", "➖ 移除用户"),
        BotCommand("users", "👥 用户列表"),
        BotCommand("repo_add", "➕ 添加仓库"),
        BotCommand("repo_remove", "➖ 移除仓库"),
        BotCommand("repos", "📁 仓库列表"),
        BotCommand("quota_set", "⚙️ 设置配额"),
        BotCommand("update_docs", "🔄 更新文档"),
        BotCommand("code_index", "🔍 索引代码"),
        # 超级管理员命令
        BotCommand("admin_add", "👑 添加管理员"),
        BotCommand("admin_remove", "🚫 移除管理员"),
        BotCommand("review", "🔧 手动审查"),
    ]

    await bot.set_my_commands(commands)
    logger.info("✅ Bot 命令菜单已注册")


async def _telegram_error_handler(update: object, context) -> None:
    """处理 Telegram Bot 运行时错误，将瞬态网络错误降级为 WARNING"""
    error = context.error
    if isinstance(error, (httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout)):
        logger.warning(f"⚡ Telegram 网络瞬态错误（将自动重试）: {error}")
    elif isinstance(error, (NetworkError, TimedOut)):
        logger.warning(f"⚡ Telegram 网络瞬态错误（将自动重试）: {error}")
    else:
        logger.error(f"❌ Telegram Bot 未预期的错误: {error}", exc_info=error)


async def start_telegram_bot():
    """启动 Telegram Bot"""
    global _telegram_bot, _telegram_app

    try:
        logger.info("🤖 启动 Telegram Bot...")

        # 创建 Bot 实例
        _telegram_bot = Bot(token=settings.telegram_bot_token)

        # 创建 Application（配置超时参数，适应不稳定网络环境）
        _telegram_app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .get_updates_read_timeout(30)
            .get_updates_connect_timeout(10)
            .read_timeout(30)
            .connect_timeout(10)
            .build()
        )

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
        _telegram_app.add_handler(CommandHandler("sign", cmd_sign))
        _telegram_app.add_handler(CommandHandler("repo_subscribe", cmd_repo_subscribe))
        _telegram_app.add_handler(
            CommandHandler("repo_unsubscribe", cmd_repo_unsubscribe)
        )
        _telegram_app.add_handler(
            CommandHandler("my_subscriptions", cmd_my_subscriptions)
        )

        # 设置通知发送器
        notification_sender = NotificationSender(_telegram_bot)
        set_notification_sender(notification_sender)

        # 注册错误处理器
        _telegram_app.add_error_handler(_telegram_error_handler)

        # 启动 Bot（非阻塞）
        await _telegram_app.initialize()
        await _telegram_app.start()
        await _telegram_app.updater.start_polling(drop_pending_updates=True)

        # 注册命令菜单
        await register_bot_commands(_telegram_bot)

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
