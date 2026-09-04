"""Telegram 通知发送器"""

import asyncio

from loguru import logger
from sqlalchemy import select
from telegram import Bot
from telegram.helpers import escape_markdown

from backend.models.identity_models import NotificationEndpoint
from backend.models.telegram_models import TelegramUser


class NotificationSender:
    """通知发送器"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _enabled_telegram_targets(
        self,
        chat_ids: list[int],
        *,
        system_chat_ids: list[int] | None = None,
    ) -> list[int]:
        """Filter legacy mirror ids through the endpoint abstraction.

        Older review/scan/MFA code still passes Telegram ids because those
        values are part of historical business/FK data.  The endpoint table
        is authoritative for opt-in state, however, so a WebUI unbind must
        also silence these compatibility paths.  If the application database
        has not been initialized yet, retain the old behavior for startup and
        isolated unit callers; once a factory exists, failures fail closed so
        an unavailable database cannot accidentally bypass an unbind.
        """
        normalized: list[int] = []
        seen: set[int] = set()
        for value in chat_ids or []:
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                continue
            # A negative Telegram id denotes a group/channel, not a user.
            # Never infer that meaning from a legacy mirror or a SQLite
            # placeholder; only the explicit system configuration below may
            # bypass the per-user endpoint filter.
            if chat_id > 0 and chat_id not in seen:
                normalized.append(chat_id)
                seen.add(chat_id)

        from backend.core.config import get_settings

        configured_system_id: int | None = None
        raw_configured_id = getattr(get_settings(), "telegram_default_chat_id", "")
        try:
            if str(raw_configured_id).strip():
                configured_system_id = int(str(raw_configured_id).strip())
        except (TypeError, ValueError):
            configured_system_id = None
        if configured_system_id == 0:
            configured_system_id = None
        # System destinations are accepted only when the caller explicitly
        # marks them and the value still matches the configured default.  This
        # prevents an arbitrary negative value from becoming a broadcast
        # target while preserving configured group/channel notifications.
        system_targets: list[int] = []
        for value in system_chat_ids or []:
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                continue
            if (
                configured_system_id is not None
                and chat_id == configured_system_id
                and chat_id not in system_targets
            ):
                system_targets.append(chat_id)
                seen.add(chat_id)

        def combine(targets: list[int]) -> list[int]:
            combined: list[int] = []
            for chat_id in targets + system_targets:
                if chat_id not in combined:
                    combined.append(chat_id)
            return combined

        if not normalized:
            return system_targets

        from backend.models import database as db_module

        session_factory = db_module.async_session
        if session_factory is None:
            return combine(normalized)
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(NotificationEndpoint.address, TelegramUser.telegram_id)
                    .join(TelegramUser, TelegramUser.id == NotificationEndpoint.user_id)
                    .where(
                        NotificationEndpoint.provider == "telegram",
                        NotificationEndpoint.enabled.is_(True),
                        (
                            NotificationEndpoint.address.in_(
                                [str(item) for item in normalized]
                            )
                            | TelegramUser.telegram_id.in_(normalized)
                        ),
                    )
                    .order_by(NotificationEndpoint.id)
                )
                requested = {str(item) for item in normalized}
                resolved: list[int] = []
                seen_resolved: set[int] = set()
                for address, legacy_id in result.all():
                    try:
                        current_id = int(address)
                    except (TypeError, ValueError):
                        continue
                    if current_id <= 0:
                        continue
                    # A legacy caller may still pass the old mirror id after
                    # the user re-bound Telegram.  Resolve that id to the
                    # user's one active endpoint so the new chat receives
                    # existing review/scan/MFA notifications.
                    if (
                        str(current_id) in requested or legacy_id in normalized
                    ) and current_id not in seen_resolved:
                        resolved.append(current_id)
                        seen_resolved.add(current_id)
        except Exception as exc:
            logger.warning("查询 Telegram 通知端点失败，已安全跳过发送: {}", exc)
            return system_targets
        return combine(resolved)

    async def send_to_targets(
        self,
        text: str,
        chat_ids: list[int],
        parse_mode: str = "Markdown",
        *,
        system_chat_ids: list[int] | None = None,
        **kwargs,
    ):
        """向多个目标发送消息，单个失败不影响其他"""

        chat_ids = await self._enabled_telegram_targets(
            chat_ids, system_chat_ids=system_chat_ids
        )

        async def send_single(chat_id: int):
            try:
                await asyncio.wait_for(
                    self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        **kwargs,
                    ),
                    timeout=5,
                )
            except TimeoutError:
                logger.warning(f"发送通知到 {chat_id} 超时")
            except Exception as e:
                logger.warning(f"发送通知到 {chat_id} 失败: {e}")

        await asyncio.gather(*(send_single(cid) for cid in chat_ids))

    async def send_review_start(
        self,
        repo_name: str,
        pr_number: int,
        pr_title: str,
        author: str,
        chat_ids: list[int] | None = None,
    ):
        """发送审查开始通知"""
        try:
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

            if not chat_ids:
                logger.debug(f"无通知目标，跳过审查开始通知: {repo_name}#{pr_number}")
                return

            await self.send_to_targets(text, chat_ids)
            logger.info(
                f"✅ 发送审查开始通知: {repo_name}#{pr_number} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"❌ 发送审查开始通知失败: {e}")

    async def send_review_complete(
        self,
        repo_name: str,
        pr_number: int,
        score: int,
        critical_count: int,
        pr_url: str,
        chat_ids: list[int] | None = None,
    ):
        """发送审查完成通知"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)

            text = (
                f"🌸 *Sakura AI 审查完成*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"🔴 严重问题: {critical_count}\n"
                f"⭐ 评分: {score}/10\n\n"
                f"[查看完整报告]({pr_url})"
            )

            if not chat_ids:
                logger.debug(f"无通知目标，跳过审查完成通知: {repo_name}#{pr_number}")
                return

            await self.send_to_targets(text, chat_ids, disable_web_page_preview=True)
            logger.info(
                f"✅ 发送审查完成通知: {repo_name}#{pr_number} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"❌ 发送审查完成通知失败: {e}")

    async def send_quota_exceeded(
        self,
        repo_name: str,
        item_type: str = "PR",
        item_number: int = 0,
        reason: str = "",
        chat_id: int | None = None,
        pr_number: int | None = None,
    ):
        """发送配额不足通知（系统告警，仅发管理员）

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
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_reason = escape_markdown(reason, version=1)

            text = (
                f"⚠️ *审查被拒绝*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 {item_type}: #{item_number}\n\n"
                f"❌ 原因: {safe_reason}\n"
                f"💡 请联系管理员增加配额"
            )

            target_chat_id = chat_id
            if not target_chat_id:
                logger.warning("无通知目标 chat_id，跳过配额不足通知发送")
                return
            await self.send_to_targets(
                text,
                [target_chat_id],
                parse_mode="Markdown",
            )
            logger.info(f"✅ 发送配额不足通知: {repo_name}#{item_type}-{item_number}")

        except Exception as e:
            logger.error(f"❌ 发送配额不足通知失败: {e}")

    async def send_unauthorized_user(
        self,
        repo_name: str,
        pr_number: int,
        github_username: str,
        chat_id: int | None = None,
    ):
        """发送未注册用户通知（系统告警，仅发管理员）"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_github_username = escape_markdown(github_username, version=1)

            text = (
                f"👤 *未注册的用户*\n\n"
                f"📦 仓库: {safe_repo_name}\n"
                f"🔢 PR: #{pr_number}\n"
                f"👤 GitHub: {safe_github_username}\n\n"
                f"⚠️ 该用户未注册，审查已跳过"
            )

            target_chat_id = chat_id
            if not target_chat_id:
                logger.warning("无通知目标 chat_id，跳过未注册用户通知发送")
                return
            await self.send_to_targets(
                text,
                [target_chat_id],
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
        summary: str | None = None,
        chat_ids: list[int] | None = None,
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

            if not chat_ids:
                logger.debug(
                    f"无通知目标，跳过Issue分析完成通知: {repo_name}#{issue_number}"
                )
                return

            await self.send_to_targets(text, chat_ids)
            logger.info(
                f"Issue 分析完成通知已发送: {repo_name}#{issue_number} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"发送 Issue 分析完成通知失败: {e}")

    async def send_scan_complete(
        self,
        repo_name: str,
        health_score: int,
        critical_count: int,
        major_count: int,
        total_findings: int,
        issue_url: str = "",
        scan_id: int | None = None,
        chat_ids: list[int] | None = None,
    ):
        """扫描完成通知

        Args:
            repo_name: 仓库全名
            health_score: 健康评分 (0-100)
            critical_count: Critical 问题数
            major_count: Major 问题数
            total_findings: 总发现数
            issue_url: GitHub Issue 链接（可为空）
            scan_id: 扫描记录 ID，用于生成 WebUI 链接回退
            chat_ids: 通知目标 Telegram chat_id 列表
        """
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            health_emoji = (
                "🟢" if health_score >= 80 else "🟡" if health_score >= 60 else "🔴"
            )

            text = (
                f"*Sakura AI 仓库扫描完成*\n\n"
                f"仓库: {safe_repo_name}\n"
                f"{health_emoji} 健康评分: *{health_score}/100*\n"
                f"🔴 Critical: {critical_count}\n"
                f"🟡 Major: {major_count}\n"
                f"总计发现: {total_findings} 个问题\n"
            )

            # 链接：如有 Issue 链接则展示；始终提供 WebUI 链接回退（若 app_domain 已配置）
            if issue_url:
                text += f"\n[查看详细报告]({issue_url})"
            if scan_id is not None:
                # 延迟导入：避免 telegram 模块与 webui 模块之间产生循环依赖
                from backend.webui.deps import get_webui_url

                webui_url = get_webui_url(f"/scans/{scan_id}")
                if webui_url:
                    text += f"\n[WebUI 查看详情]({webui_url})"
                else:
                    logger.warning(
                        f"app_domain 未配置，跳过 WebUI 链接 (scan_id={scan_id})"
                    )

            if not chat_ids:
                logger.debug(f"无通知目标，跳过扫描完成通知: {repo_name}")
                return

            await self.send_to_targets(text, chat_ids, disable_web_page_preview=True)
            logger.info(f"发送扫描完成通知: {repo_name} → {len(chat_ids)} 人")

        except Exception as e:
            logger.error(f"发送扫描完成通知失败: {e}")

    async def send_critical_issue_alert(
        self,
        repo_name: str,
        issue_number: int,
        title: str,
        category: str,
        summary: str,
        feasibility: str,
        issue_url: str,
        suggested_labels: list | None = None,
        chat_ids: list[int] | None = None,
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

            text += f"\n*AI 摘要*\n{safe_summary}\n"

            text += f"\n*可行性评估*\n{safe_feasibility}\n"

            if suggested_labels:
                labels_str = ", ".join(
                    label.get("name", "")
                    for label in suggested_labels[:5]
                    if isinstance(label, dict)
                )
                if labels_str:
                    safe_labels = escape_markdown(labels_str, version=1)
                    text += f"\n🏷️ 建议标签: {safe_labels}\n"

            text += f"\n[查看详情]({issue_url})"

            if not chat_ids:
                logger.debug(
                    f"无通知目标，跳过Critical告警: {repo_name}#{issue_number}"
                )
                return

            await self.send_to_targets(text, chat_ids)
            logger.info(
                f"Critical Issue 告警已发送: {repo_name}#{issue_number} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"发送 Critical Issue 告警失败: {e}")

    # ========== MFA 安全通知 ==========

    _EVENT_EMOJIS = {
        "totp_enabled": "✅",
        "totp_disabled": "⚠️",
        "recovery_codes_regenerated": "🔄",
        "passkey_registered": "🔑",
        "passkey_deleted": "🗑️",
        "mfa_reset_by_admin": "🛡️",
        "totp_reset_by_admin": "🛡️",
        "passkey_deleted_by_admin": "🛡️",
        "mfa_lockout": "🔒",
        "mfa_required_by_admin": "📋",
        "mfa_unrequired_by_admin": "📋",
    }

    async def send_mfa_event(
        self,
        event_type: str,
        detail: str = "",
        chat_id: int | None = None,
    ):
        """发送 MFA 安全事件通知给用户。

        Args:
            event_type: 事件类型（totp_enabled / totp_disabled / passkey_registered 等）
            detail: 事件详情描述
            chat_id: 用户 Telegram chat_id
        """
        if not chat_id:
            return

        # Lazy import to avoid circular dependency at module load time
        from backend.webui.i18n import i18n as _i18n

        i18n_key = f"telegram_mfa.{event_type}"
        label = _i18n.t(i18n_key)
        # Fallback: if translation missing, use event_type as-is
        if label == i18n_key:
            label = event_type.replace("_", " ").title()

        emoji = self._EVENT_EMOJIS.get(event_type, "🔔")
        safe_label = escape_markdown(label, version=1)
        safe_detail = escape_markdown(detail[:300], version=1) if detail else ""

        text = f"{emoji} *{safe_label}*\n"
        if safe_detail:
            text += f"\n{safe_detail}\n"
        footer = _i18n.t("telegram_mfa.footer")
        text += f"\n_{escape_markdown(footer, version=1)}_"

        try:
            await self.send_to_targets(text, [chat_id])
            logger.info(f"MFA 通知已发送: event={event_type}, chat_id={chat_id}")
        except Exception as exc:
            logger.error(f"发送 MFA 通知失败: event={event_type}, error={exc}")


# 全局通知发送器实例
_notification_sender: NotificationSender | None = None


def get_notification_sender() -> NotificationSender | None:
    """获取通知发送器实例"""
    return _notification_sender


def set_notification_sender(sender: NotificationSender):
    """设置通知发送器实例"""
    global _notification_sender
    _notification_sender = sender
