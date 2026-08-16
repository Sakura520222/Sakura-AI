"""仓库扫描报告生成服务"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from backend.core.branding import SAKURA_AI_REPO_URL
from backend.core.config import get_settings
from backend.core.time_service import get_time_service
from backend.services.ai_reviewer.constants import SEVERITY_EMOJI
from backend.webui.deps import get_webui_url

if TYPE_CHECKING:
    from backend.models.scan_models import RepoScan, ScanFinding

settings = get_settings()

# 严重性排序权重
_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}


class ScanReportService:
    """扫描报告生成与交付"""

    async def generate_and_deliver(
        self, scan_id: int, report_data: dict | None = None
    ) -> dict:
        """生成报告并交付到所有渠道

        Args:
            scan_id: 扫描记录 ID
            report_data: 直接传递的聚合数据（code_file_count, overall_health_score 等），
                         用于绕过 DB 读取时序问题

        Returns:
            {"issue_number": int|None, "issue_url": str|None}
        """
        from sqlalchemy import select

        from backend.models.database import async_session
        from backend.models.scan_models import RepoScan, ScanFinding

        # 加载扫描记录和 findings
        async with async_session() as session:
            scan = await session.get(RepoScan, scan_id)
            if not scan:
                logger.error(f"扫描记录不存在: {scan_id}")
                return {}

            result = await session.execute(
                select(ScanFinding)
                .where(ScanFinding.scan_id == scan_id)
                .order_by(ScanFinding.severity, ScanFinding.confidence.desc())
            )
            findings = result.scalars().all()

        # 用直接传递的聚合数据覆盖可能过期的 DB 值
        if report_data:
            for key, value in report_data.items():
                if hasattr(scan, key) and value is not None:
                    setattr(scan, key, value)

        report_info = {}

        # 创建 GitHub Issue（自动创建已固定开启，有发现才报告）
        if scan.total_findings > 0:
            issue_info = await self._create_github_issue(scan, findings)
            if issue_info:
                report_info.update(issue_info)

        # 发送 Telegram 通知（使用刚创建的 Issue URL）
        if settings.scan_send_telegram:
            logger.info(f"正在发送扫描 Telegram 通知: {scan.repo_name}")
            issue_url = report_info.get("issue_url") or scan.report_issue_url
            await self._send_telegram_notification(scan, issue_url=issue_url)
        else:
            logger.info("Telegram 扫描通知已禁用")

        return report_info

    def generate_issue_body(self, scan: RepoScan, findings: list[ScanFinding]) -> str:
        """生成 GitHub Issue 报告 Markdown 内容"""
        lines = []

        # 标题区
        lines.append("## Sakura AI 仓库扫描报告\n")

        # 扫描概览表
        scan_time = (
            get_time_service().format_display(scan.created_at)
            if scan.created_at
            else "未知"
        )
        commit_short = scan.commit_sha[:7] if scan.commit_sha else "未知"
        health = scan.overall_health_score or 0
        health_emoji = "🟢" if health >= 80 else "🟡" if health >= 60 else "🔴"

        lines.append("### 扫描概览\n")
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 仓库 | `{scan.repo_name}` |")
        lines.append(f"| 扫描时间 | {scan_time} |")
        lines.append(f"| Commit | `{commit_short}` |")
        lines.append(f"| 扫描文件数 | {scan.code_file_count or 0} |")
        lines.append(f"| {health_emoji} 健康评分 | **{health}/100** |")
        lines.append("")

        # 问题统计
        lines.append("### 问题统计\n")
        lines.append("| 严重性 | 数量 |")
        lines.append("|--------|------|")
        lines.append(f"| 🔴 Critical | {scan.critical_count or 0} |")
        lines.append(f"| 🟡 Major | {scan.major_count or 0} |")
        lines.append(f"| 🟠 Minor | {scan.minor_count or 0} |")
        lines.append(f"| 💡 Suggestion | {scan.suggestion_count or 0} |")
        lines.append("")

        # 按严重性分组展示 findings
        if findings:
            grouped = {}
            for f in findings:
                grouped.setdefault(f.severity, []).append(f)

            for sev in ["critical", "major", "minor", "suggestion"]:
                items = grouped.get(sev, [])
                if not items:
                    continue

                emoji = SEVERITY_EMOJI.get(sev, "💡")
                lines.append(f"### {emoji} {sev.upper()}\n")

                for idx, f in enumerate(items, 1):
                    lines.append(f"#### {idx}. {f.title}\n")

                    if f.file_path:
                        loc = f.file_path
                        if f.line_start:
                            loc += f":{f.line_start}"
                            if f.line_end and f.line_end != f.line_start:
                                loc += f"-{f.line_end}"
                        lines.append(f"- **文件**: `{loc}`")
                    lines.append(f"- **类别**: {f.category}")
                    if f.confidence is not None:
                        lines.append(f"- **置信度**: {f.confidence}%")
                    lines.append(f"- **描述**: {f.description}")
                    if f.suggestion:
                        lines.append(f"- **建议**: {f.suggestion}")
                    lines.append("")

        lines.append("---")
        lines.append(f"*此报告由 [Sakura AI]({SAKURA_AI_REPO_URL}) 自动生成*")

        return "\n".join(lines)

    def generate_telegram_message(
        self, scan: RepoScan, issue_url: str | None = None
    ) -> str:
        """生成 Telegram 通知消息"""
        health = scan.overall_health_score or 0
        health_emoji = "🟢" if health >= 80 else "🟡" if health >= 60 else "🔴"

        duration = ""
        if scan.started_at and scan.completed_at:
            delta = (scan.completed_at - scan.started_at).total_seconds()
            if delta < 60:
                duration = f"{int(delta)}s"
            elif delta < 3600:
                duration = f"{int(delta // 60)}m{int(delta % 60)}s"
            else:
                duration = f"{int(delta // 3600)}h{int((delta % 3600) // 60)}m"

        lines = [
            "*Sakura AI 仓库扫描完成*",
            "",
            f"仓库: `{scan.repo_name}`",
        ]

        if scan.commit_sha:
            lines.append(f"Commit: `{scan.commit_sha[:7]}`")
        if duration:
            lines.append(f"扫描耗时: {duration}")
        lines.append(f"扫描文件: {scan.code_file_count or 0}")
        lines.append("")

        # 健康评分
        lines.append(f"{health_emoji} 健康评分: *{health}/100*")
        lines.append("")

        # 问题统计
        total = scan.total_findings or 0
        if total > 0:
            lines.append("*问题统计*")
            if scan.critical_count or 0:
                lines.append(f" 🔴 Critical: {scan.critical_count}")
            if scan.major_count or 0:
                lines.append(f" 🟡 Major: {scan.major_count}")
            if scan.minor_count or 0:
                lines.append(f" 🟠 Minor: {scan.minor_count}")
            if scan.suggestion_count or 0:
                lines.append(f" 💡 Suggestion: {scan.suggestion_count}")
            lines.append("")

            # Token 消耗
            total_tokens = (scan.prompt_tokens or 0) + (scan.completion_tokens or 0)
            if total_tokens > 0:
                lines.append(f"Token 消耗: {total_tokens:,}")
                lines.append("")
        else:
            lines.append("✅ 未发现问题，代码质量良好")
            lines.append("")

        # 链接：如有 Issue 链接则展示；始终提供 WebUI 链接（若 app_domain 已配置）
        webui_url = get_webui_url(f"/scans/{scan.id}")
        logger.debug(f"WebUI URL for scan {scan.id}: {webui_url!r}")
        link_url = issue_url or scan.report_issue_url
        if link_url:
            lines.append(f"[查看详细报告]({link_url})")
        if webui_url:
            lines.append(f"[WebUI 查看详情]({webui_url})")
        else:
            logger.warning(f"app_domain 未配置，跳过 WebUI 链接 (scan_id={scan.id})")

        return "\n".join(lines)

    async def _create_github_issue(
        self, scan: RepoScan, findings: list[ScanFinding]
    ) -> dict | None:
        """在仓库中创建 GitHub Issue 报告"""
        try:
            from backend.core.github_app import GitHubAppClient

            github_app = GitHubAppClient()

            # 检查最低严重性过滤
            min_sev = _SEVERITY_ORDER.get(settings.scan_min_severity_for_issue, 1)
            has_qualifying = any(
                _SEVERITY_ORDER.get(f.severity, 3) <= min_sev for f in findings
            )
            if not has_qualifying:
                logger.info(
                    f"扫描 {scan.id} 无符合严重性阈值 ({settings.scan_min_severity_for_issue}) 的发现，跳过创建 Issue"
                )
                return None

            repo_owner, repo_name_only = scan.repo_name.split("/", 1)
            client = await asyncio.to_thread(
                github_app.get_repo_client, repo_owner, repo_name_only
            )
            if not client:
                logger.error(f"无法获取仓库客户端: {scan.repo_name}")
                return None
            repo = await asyncio.to_thread(client.get_repo, scan.repo_name)
            if not repo:
                logger.error(f"无法获取仓库: {scan.repo_name}")
                return None

            # 检查仓库是否启用了 Issues 功能
            try:
                repo_has_issues = await asyncio.to_thread(
                    lambda: repo.raw_data.get("has_issues", True)
                )
                if not repo_has_issues:
                    logger.info(f"仓库 {scan.repo_name} 已禁用 Issues，跳过创建 Issue")
                    return None
            except Exception:
                pass  # 检查失败不阻断流程

            # 生成 Issue 内容
            health = scan.overall_health_score or 0
            title = f"🛡️ Sakura AI 扫描报告 — {scan.repo_name} ({health}/100)"
            body = self.generate_issue_body(scan, findings)
            labels = ["sakura-scan", "automated"]

            # 创建 Issue
            issue = None
            try:
                issue = await asyncio.to_thread(
                    repo.create_issue,
                    title=title,
                    body=body,
                    labels=labels,
                )
            except Exception as create_err:
                err_str = str(create_err)
                if "410" in err_str or "has been disabled" in err_str:
                    logger.info(
                        f"仓库 {scan.repo_name} Issues 功能已禁用，跳过创建 Issue"
                    )
                    return None
                # labels 可能不存在，尝试不带 labels 重试
                logger.warning(
                    f"创建 Issue 失败（可能 label 不存在）: {create_err}，尝试不带 labels 重试"
                )
                try:
                    issue = await asyncio.to_thread(
                        repo.create_issue,
                        title=title,
                        body=body,
                    )
                except Exception as retry_err:
                    logger.error(
                        f"创建 GitHub Issue 重试也失败: {type(retry_err).__name__}: {retry_err}"
                    )
                    return None

            if issue:
                logger.info(f"✅ 已创建扫描报告 Issue: {scan.repo_name}#{issue.number}")

                # 索引到 Issue 向量库（bot 创建的 Issue 不触发 webhook，需主动索引）
                try:
                    from backend.services.issue_embedding_service import (
                        IssueEmbeddingService,
                    )

                    emb_service = IssueEmbeddingService()
                    await emb_service.upsert_issue(
                        repo_owner,
                        repo_name_only,
                        issue.number,
                        title=issue.title,
                        body=body,
                        state="open",
                    )
                    logger.info(
                        f"已索引扫描报告 Issue: {scan.repo_name}#{issue.number}"
                    )
                except Exception as emb_err:
                    logger.warning(f"索引扫描报告 Issue 失败: {emb_err}")

                return {"issue_number": issue.number, "issue_url": issue.html_url}

            return None

        except Exception as e:
            logger.error(
                f"创建 GitHub Issue 失败: {type(e).__name__}: {e}", exc_info=True
            )
            return None

    async def _send_telegram_notification(
        self, scan: RepoScan, issue_url: str | None = None
    ):
        """发送 Telegram 通知"""
        try:
            from sqlalchemy import select

            from backend.models.database import async_session
            from backend.models.telegram_models import (
                UserRepoSubscription,
            )
            from backend.telegram.notifications import get_notification_sender

            sender = get_notification_sender()
            if not sender or not sender.bot:
                logger.warning("Telegram Bot 未就绪，跳过扫描通知")
                return

            # 获取订阅该仓库的 Telegram 用户 telegram_id
            chat_ids: list[int] = []
            async with async_session() as session:
                # 1. 查询 UserRepoSubscription（用户主动订阅）
                result = await session.execute(
                    select(UserRepoSubscription.telegram_id)
                    .where(UserRepoSubscription.repo_name == scan.repo_name)
                    .distinct()
                )
                chat_ids = [r[0] for r in result.all() if r[0]]

            # 兜底：无订阅用户时查询所有管理员
            if not chat_ids:
                chat_ids = await self._get_all_admin_telegram_ids()

            # 添加默认管理员通知
            from backend.core.config import get_settings

            s = get_settings()
            if s.telegram_default_chat_id:
                try:
                    default_chat_id = int(s.telegram_default_chat_id)
                    if default_chat_id not in chat_ids:
                        chat_ids.append(default_chat_id)
                except ValueError:
                    pass

            if not chat_ids:
                logger.warning(f"无 Telegram 通知目标: {scan.repo_name}")
                return

            text = self.generate_telegram_message(scan, issue_url=issue_url)
            await sender.send_to_targets(text, chat_ids)

            logger.info(f"✅ 扫描通知已发送: {scan.repo_name} → {len(chat_ids)} 个目标")

        except Exception as e:
            logger.error(f"发送 Telegram 扫描通知失败: {e}")

    @classmethod
    async def _get_all_admin_telegram_ids(cls) -> list[int]:
        """查询所有管理员的 telegram_id"""
        from sqlalchemy import select

        from backend.models.database import async_session
        from backend.models.telegram_models import TelegramUser

        async with async_session() as session:
            result = await session.execute(
                select(TelegramUser.telegram_id).where(
                    TelegramUser.role.in_(("admin", "super_admin"))
                )
            )
            return [r[0] for r in result.all() if r[0]]
