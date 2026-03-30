"""Telegram 通知发送器"""

from typing import Optional
from telegram import Bot
from telegram.helpers import escape_markdown
from loguru import logger

from backend.core.config import get_settings

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
            # 转义用户输入的特殊字符，避免 Markdown 解析错误
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_pr_title = escape_markdown(pr_title, version=1)
            safe_author = escape_markdown(author, version=1)

            text = (
                f"🔔 *Sakura AI 开始审查*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"👤 作者: {safe_author}\n"
                f"📝 标题: {safe_pr_title}\n\n"
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
            # 转义仓库名称
            safe_repo_name = escape_markdown(repo_name, version=1)

            text = (
                f"🌸 *Sakura AI 审查完成*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
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
        item_type: str = "PR",
        item_number: int = 0,
        reason: str = "",
        chat_id: Optional[int] = None,
        pr_number: Optional[int] = None,
    ):
        """发送配额不足通知

        Args:
            repo_name: 仓库全名
            item_type: 项目类型 ("PR" 或 "Issue")
            item_number: 项目编号
            reason: 配额不足原因
            chat_id: 目标聊天 ID
            pr_number: 向后兼容，传入时使用 "PR" 类型
        """
        # 向后兼容旧调用方式
        if pr_number is not None and item_number == 0:
            item_number = pr_number

        try:
            # 转义用户输入的特殊字符
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_reason = escape_markdown(reason, version=1)

            text = (
                f"⚠️ *审查被拒绝*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 {item_type}: #{item_number}\n\n"
                f"❌ 原因: {safe_reason}\n"
                f"💡 请联系管理员增加配额"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"✅ 发送配额不足通知: {repo_name}#{item_type}-{item_number}")

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
            # 转义仓库名称
            safe_repo_name = escape_markdown(repo_name, version=1)

            text = (
                f"🚫 *未授权的仓库*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
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
            # 转义用户输入的特殊字符
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_github_username = escape_markdown(github_username, version=1)

            text = (
                f"👤 *未注册的用户*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"👤 GitHub: {safe_github_username}\n\n"
                f"⚠️ 该用户未注册，审查已跳过"
            )

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.warning(
                f"⚠️ 未注册用户审查请求: {github_username} in {repo_name}#{pr_number}"
            )

        except Exception as e:
            logger.error(f"发送未注册用户通知失败: {e}")

    async def send_issue_analysis_complete(
        self,
        repo_name: str,
        issue_number: int,
        category: str,
        priority: str,
        issue_url: str,
        summary: str = None,
        chat_id: Optional[int] = None,
    ):
        """Issue 分析完成通知"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_category = escape_markdown(category, version=1)
            safe_priority = escape_markdown(priority, version=1)

            text = (
                f"📋 *Issue 分析完成*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 Issue: #{issue_number}\n"
                f"🏷️ 分类: {safe_category}\n"
                f"📊 优先级: {safe_priority}\n"
            )

            if summary:
                safe_summary = escape_markdown(summary[:200], version=1)
                text += f"\n📝 {safe_summary}\n"

            text += f"\n[查看详情]({issue_url})"

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"Issue 分析完成通知已发送: {repo_name}#{issue_number}")

        except Exception as e:
            logger.error(f"发送 Issue 分析完成通知失败: {e}")

    async def send_critical_issue_alert(
        self,
        repo_name: str,
        issue_number: int,
        title: str,
        category: str,
        summary: str,
        feasibility: str,
        issue_url: str,
        suggested_labels: list = None,
        chat_id: Optional[int] = None,
    ):
        """Critical Issue 即时告警（附带 AI 摘要 + 可行性结论）"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_title = escape_markdown(title, version=1)
            safe_category = escape_markdown(category, version=1)
            safe_summary = escape_markdown(summary[:300], version=1)
            safe_feasibility = escape_markdown(feasibility[:300], version=1)

            text = (
                f"🚨 *Critical Issue 告警*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 Issue: #{issue_number}\n"
                f"🏷️ 分类: {safe_category}\n"
                f"📊 优先级: critical\n"
                f"📝 标题: {safe_title}\n"
            )

            text += (
                f"\n📋 *AI 摘要*\n"
                f"{safe_summary}\n"
            )

            text += (
                f"\n🔍 *可行性评估*\n"
                f"{safe_feasibility}\n"
            )

            if suggested_labels:
                labels_str = ", ".join(
                    l.get("name", "") for l in suggested_labels[:5] if isinstance(l, dict)
                )
                if labels_str:
                    safe_labels = escape_markdown(labels_str, version=1)
                    text += f"\n🏷️ 建议标签: {safe_labels}\n"

            text += f"\n[查看详情]({issue_url})"

            target_chat_id = chat_id or int(settings.telegram_default_chat_id)
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"Critical Issue 告警已发送: {repo_name}#{issue_number}")

        except Exception as e:
            logger.error(f"发送 Critical Issue 告警失败: {e}")

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
