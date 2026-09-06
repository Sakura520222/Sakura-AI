"""Telegram Bot 主逻辑"""

import httpx
from loguru import logger
from telegram import Bot, BotCommand
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler

from backend.core.config import get_settings
from backend.telegram.handlers import cmd_bind, cmd_start
from backend.telegram.notifications import NotificationSender, set_notification_sender

settings = get_settings()

# 全局 Bot 实例
_telegram_bot: Bot = None
_telegram_app: Application = None


async def register_bot_commands(bot: Bot):
    """Expose only the optional notification binding handshake."""
    commands = [
        BotCommand("start", "🔗 开始通知绑定"),
        BotCommand("bind", "🔗 绑定通知 Telegram"),
    ]

    await bot.set_my_commands(commands)
    logger.info("✅ Bot 命令菜单已注册")


async def _telegram_error_handler(update: object, context) -> None:
    """处理 Telegram Bot 运行时错误，将瞬态网络错误降级为 WARNING"""
    error = context.error
    error_str = str(error)

    # 忽略消息内容未变更（用户重复点击同页按钮）
    if "Message is not modified" in error_str:
        return

    if isinstance(
        error,
        (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            NetworkError,
            TimedOut,
        ),
    ):
        logger.warning(f"⚡ Telegram 网络瞬态错误（将自动重试）: {error}")
    else:
        logger.error(f"❌ Telegram Bot 未预期的错误: {error}", exc_info=error)


async def start_telegram_bot():
    """启动 Telegram Bot"""
    global _telegram_bot, _telegram_app

    if not getattr(settings, "telegram_enabled", True) or not getattr(
        settings, "telegram_bot_token", None
    ):
        logger.info("ℹ️ Telegram Provider 未启用或缺少 token，跳过 Bot 启动")
        return

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

        # Only the optional notification binding handshake is mounted.  The
        # historical Telegram business/admin handlers remain in handlers.py
        # for compatibility imports but are intentionally unreachable here.
        _telegram_app.add_handler(CommandHandler("start", cmd_start))
        _telegram_app.add_handler(CommandHandler("bind", cmd_bind))

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

        # 获取 Bot 用户名并缓存到 Settings（用于构造深链接）
        try:
            bot_info = await _telegram_bot.get_me()
            settings.telegram_bot_username = bot_info.username
            logger.info(f"Telegram Bot 用户名: @{bot_info.username}")
        except Exception as e:
            logger.warning(f"获取 Telegram Bot 用户名失败: {e}")

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
        finally:
            _telegram_app = None


def get_telegram_bot() -> Bot:
    """获取 Telegram Bot 实例"""
    return _telegram_bot
