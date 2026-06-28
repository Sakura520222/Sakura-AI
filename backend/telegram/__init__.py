"""Telegram Bot 模块"""

from typing import Any


async def start_telegram_bot(*args: Any, **kwargs: Any):
    """惰性启动 Telegram Bot。

    避免循环导入路径：payment_service → refund_notification_service →
    telegram.notifications → telegram.__init__ → telegram.bot → payment_service。
    """
    from backend.telegram.bot import start_telegram_bot as _start_telegram_bot

    return await _start_telegram_bot(*args, **kwargs)


async def stop_telegram_bot(*args: Any, **kwargs: Any):
    """惰性停止 Telegram Bot。

    避免循环导入路径：payment_service → refund_notification_service →
    telegram.notifications → telegram.__init__ → telegram.bot → payment_service。
    """
    from backend.telegram.bot import stop_telegram_bot as _stop_telegram_bot

    return await _stop_telegram_bot(*args, **kwargs)


__all__ = ["start_telegram_bot", "stop_telegram_bot"]
