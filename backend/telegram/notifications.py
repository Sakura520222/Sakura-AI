"""Telegram 通知发送器"""

import asyncio
import json
from typing import Optional, List, Dict
from telegram import Bot
from telegram.helpers import escape_markdown
from loguru import logger


class NotificationSender:
    """通知发送器"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_to_targets(
        self, text: str, chat_ids: List[int], parse_mode: str = "Markdown", **kwargs
    ):
        """向多个目标发送消息，单个失败不影响其他"""

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
            except asyncio.TimeoutError:
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
        chat_ids: Optional[List[int]] = None,
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
        chat_ids: Optional[List[int]] = None,
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
        chat_id: Optional[int] = None,
        pr_number: Optional[int] = None,
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
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=text,
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
        chat_id: Optional[int] = None,
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
        chat_ids: Optional[List[int]] = None,
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
        chat_ids: Optional[List[int]] = None,
    ):
        """扫描完成通知

        注意: 此方法为预留功能，尚未集成到扫描流程中。
        EVENT_TYPES 中已包含 scan_complete 事件类型，待扫描流程集成后生效。

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
        suggested_labels: list = None,
        chat_ids: Optional[List[int]] = None,
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
        chat_id: Optional[int] = None,
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

    # ========== Agent 任务通知 ==========

    # 通知事件类型（用户可配置偏好）
    EVENT_TYPES: Dict[str, str] = {
        "review_start": "PR 审查开始",
        "review_complete": "PR 审查完成",
        "issue_analysis": "Issue 分析完成",
        "scan_complete": "仓库扫描完成",
        "agent_task_started": "Agent 任务开始",
        "agent_task_completed": "Agent 任务完成",
        "agent_task_failed": "Agent 任务失败",
    }

    async def send_agent_task_started(
        self,
        task_id: int,
        repo_name: str,
        title: str,
        source_type: str = "",
        chat_ids: Optional[List[int]] = None,
    ):
        """发送 Agent 任务开始通知"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_title = escape_markdown(title[:100], version=1)
            source_label = {
                "issue_analysis": "Issue 分析",
                "scan_finding": "扫描发现",
                "scan_report_issue": "扫描报告",
                "manual_issue": "手动触发",
            }.get(source_type, source_type or "未知")

            # 延迟导入：避免 telegram 模块与 webui 模块之间产生循环依赖
            from backend.webui.i18n import i18n as _i18n

            i18n_key = "telegram_agent.task_started"
            text = _i18n.t(
                i18n_key,
                repo_name=safe_repo_name,
                title=safe_title,
                source_type=source_label,
                task_id=task_id,
            )
            # Fallback: if translation missing, use hardcoded default
            if text == i18n_key:
                text = (
                    f"🤖 *Agent 任务已启动*\n\n"
                    f"📦 仓库: {safe_repo_name}\n"
                    f"📝 标题: {safe_title}\n"
                    f"📋 来源: {source_label}\n"
                    f"🆔 任务ID: {task_id}\n\n"
                    f"⏳ 正在执行中..."
                )

            footer = _i18n.t("telegram_agent.footer")
            if footer != "telegram_agent.footer":
                text += f"\n\n_{escape_markdown(footer, version=1)}_"

            if not chat_ids:
                logger.debug(f"无通知目标，跳过Agent任务开始通知: task_id={task_id}")
                return

            await self.send_to_targets(text, chat_ids)
            logger.info(
                f"✅ 发送Agent任务开始通知: task_id={task_id} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"❌ 发送Agent任务开始通知失败: {e}")

    async def send_agent_task_completed(
        self,
        task_id: int,
        repo_name: str,
        title: str,
        pr_url: str = "",
        iteration_count: int = 0,
        chat_ids: Optional[List[int]] = None,
    ):
        """发送 Agent 任务完成通知"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_title = escape_markdown(title[:100], version=1)

            # 延迟导入：避免 telegram 模块与 webui 模块之间产生循环依赖
            from backend.webui.i18n import i18n as _i18n

            i18n_key = "telegram_agent.task_completed"
            text = _i18n.t(
                i18n_key,
                repo_name=safe_repo_name,
                title=safe_title,
                iterations=iteration_count,
                task_id=task_id,
            )
            # Fallback: if translation missing, use hardcoded default
            if text == i18n_key:
                text = (
                    f"✅ *Agent 任务已完成*\n\n"
                    f"📦 仓库: {safe_repo_name}\n"
                    f"📝 标题: {safe_title}\n"
                    f"🔄 迭代轮数: {iteration_count}\n"
                    f"🆔 任务ID: {task_id}\n"
                )

            if pr_url:
                text += f"\n[查看 Pull Request]({pr_url})"

            footer = _i18n.t("telegram_agent.footer")
            if footer != "telegram_agent.footer":
                text += f"\n_{escape_markdown(footer, version=1)}_"

            if not chat_ids:
                logger.debug(f"无通知目标，跳过Agent任务完成通知: task_id={task_id}")
                return

            await self.send_to_targets(text, chat_ids, disable_web_page_preview=True)
            logger.info(
                f"✅ 发送Agent任务完成通知: task_id={task_id} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"❌ 发送Agent任务完成通知失败: {e}")

    async def send_agent_task_failed(
        self,
        task_id: int,
        repo_name: str,
        title: str,
        error_message: str = "",
        failed_phase: str = "",
        chat_ids: Optional[List[int]] = None,
    ):
        """发送 Agent 任务失败通知"""
        try:
            safe_repo_name = escape_markdown(repo_name, version=1)
            safe_title = escape_markdown(title[:100], version=1)
            safe_error = escape_markdown(error_message[:300], version=1)
            phase_label = {
                "iteration_failed": "迭代审查未通过",
                "validation_failed": "验证失败",
                "error": "执行异常",
            }.get(failed_phase, failed_phase or "未知")

            # 延迟导入：避免 telegram 模块与 webui 模块之间产生循环依赖
            from backend.webui.i18n import i18n as _i18n

            i18n_key = "telegram_agent.task_failed"
            text = _i18n.t(
                i18n_key,
                repo_name=safe_repo_name,
                title=safe_title,
                phase=phase_label,
                task_id=task_id,
                error=safe_error,
            )
            # Fallback: if translation missing, use hardcoded default
            if text == i18n_key:
                text = (
                    f"❌ *Agent 任务失败*\n\n"
                    f"📦 仓库: {safe_repo_name}\n"
                    f"📝 标题: {safe_title}\n"
                    f"🔍 失败阶段: {phase_label}\n"
                    f"🆔 任务ID: {task_id}\n"
                )
                if safe_error:
                    text += f"\n💬 原因: {safe_error}"

            footer = _i18n.t("telegram_agent.footer")
            if footer != "telegram_agent.footer":
                text += f"\n_{escape_markdown(footer, version=1)}_"

            if not chat_ids:
                logger.debug(f"无通知目标，跳过Agent任务失败通知: task_id={task_id}")
                return

            await self.send_to_targets(text, chat_ids)
            logger.info(
                f"✅ 发送Agent任务失败通知: task_id={task_id} → {len(chat_ids)} 人"
            )

        except Exception as e:
            logger.error(f"❌ 发送Agent任务失败通知失败: {e}")

    @staticmethod
    def is_event_enabled(preferences_json: Optional[str], event_type: str) -> bool:
        """检查指定事件类型在用户偏好中是否启用。

        Args:
            preferences_json: 用户通知偏好 JSON 字符串（None 表示全部启用）
            event_type: 事件类型

        Returns:
            True 表示启用，False 表示禁用
        """
        if not preferences_json:
            return True
        try:
            prefs = json.loads(preferences_json)
            if not isinstance(prefs, dict):
                return True
            # 未设置的事件类型默认启用
            return prefs.get(event_type, True)
        except (json.JSONDecodeError, TypeError):
            return True


# 全局通知发送器实例
_notification_sender: Optional[NotificationSender] = None


def get_notification_sender() -> Optional[NotificationSender]:
    """获取通知发送器实例"""
    return _notification_sender


def set_notification_sender(sender: NotificationSender):
    """设置通知发送器实例"""
    global _notification_sender
    _notification_sender = sender
