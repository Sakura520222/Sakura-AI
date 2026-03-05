"""Telegram 通知发送器"""

from typing import Optional
from telegram import Bot
from loguru import logger

from backend.core.config import get_settings
from backend.services.telegram_service import TelegramService

settings = get_settings()


class NotificationSender:
    """通知发送器"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_review_start(
        self,
        repo_name: str,
        pr_number: int,
        pr_title: str,
        author: str,
        chat_id: Optional[int] = None,
    ):
        """发送审查开始通知"""
        try:
            text = (
                f"🔔 *Sakura AI 开始审查*\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"👤 作者: {author}\n"
                f"📝 标题: {pr_title}\n\n"
                f"⏳ 审查中，请稍候..."
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"✅ 发送审查开始通知: {repo_name}#{pr_number}")

        except Exception as e:
            logger.error(f"❌ 发送审查开始通知失败: {e}")

    async def send_review_complete(
        self,
        repo_name: str,
        pr_number: int,
        score: int,
        critical_count: int,
        pr_url: str,
        chat_id: Optional[int] = None,
    ):
        """发送审查完成通知"""
        try:
            text = (
                f"🌸 *Sakura AI 审查完成*\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"🔴 严重问题: {critical_count}\n"
                f"⭐ 评分: {score}/10\n\n"
                f"[查看完整报告]({pr_url})"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            logger.info(f"✅ 发送审查完成通知: {repo_name}#{pr_number}")

        except Exception as e:
            logger.error(f"❌ 发送审查完成通知失败: {e}")

    async def send_quota_exceeded(
        self,
        repo_name: str,
        pr_number: int,
        reason: str,
        chat_id: Optional[int] = None,
    ):
        """发送配额不足通知"""
        try:
            text = (
                f"⚠️ *审查被拒绝*\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🔢 PR: #{pr_number}\n\n"
                f"❌ 原因: {reason}\n"
                f"💡 请联系管理员增加配额"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"✅ 发送配额不足通知: {repo_name}#{pr_number}")

        except Exception as e:
            logger.error(f"❌ 发送配额不足通知失败: {e}")

    async def send_unauthorized_repo(
        self,
        repo_name: str,
        pr_number: int,
        chat_id: Optional[int] = None,
    ):
        """发送未授权仓库通知（仅管理员可见）"""
        try:
            text = (
                f"🚫 *未授权的仓库*\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🔢 PR: #{pr_number}\n\n"
                f"⚠️ 该仓库未在白名单中，审查已跳过"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.warning(f"⚠️ 未授权仓库审查请求: {repo_name}#{pr_number}")

        except Exception as e:
            logger.error(f"❌ 发送未授权通知失败: {e}")

    async def send_unauthorized_user(
        self,
        repo_name: str,
        pr_number: int,
        github_username: str,
        chat_id: Optional[int] = None,
    ):
        """发送未注册用户通知（仅管理员可见）"""
        try:
            text = (
                f"👤 *未注册的用户*\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"👤 GitHub: {github_username}\n\n"
                f"⚠️ 该用户未注册，审查已跳过"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.warning(f"⚠️ 未注册用户审查请求: {github_username} in {repo_name}#{pr_number}")

        except Exception as e:
            logger.error(f"❌ 发送未注册用户通知失败: {e}")


# 全局通知发送器实例
_notification_sender: Optional[NotificationSender] = None


def get_notification_sender() -> Optional[NotificationSender]:
    """获取通知发送器实例"""
    return _notification_sender


def set_notification_sender(sender: NotificationSender):
    """设置通知发送器实例"""
    global _notification_sender
    _notification_sender = sender