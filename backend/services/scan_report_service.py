import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from telegram.helpers import escape_markdown

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
_CATEGORY_ORDER = [
    "security",
    "performance",
    "reliability",
    "maintainability",
    "architecture",
]

# 双语文案（服务端生成 GitHub Issue / Telegram 文本；协议枚举值保持英文）
_TEXT = {
    "zh-CN": {
        "title": "Sakura AI 仓库扫描报告",
        "summary_heading": "扫描总结",
        "overview_heading": "扫描概览",
        "repo": "仓库",
        "scan_time": "扫描时间",
        "commit": "Commit",
        "trigger": "触发方式",
        "files": "扫描文件数",
        "chunks": "索引代码块",
        "rounds": "AI 轮次",
        "tokens": "Token 消耗",
        "duration": "扫描耗时",
        "health": "健康评分",
        "stats_heading": "问题统计",
        "matrix_heading": "严重性 × 类别分布",
        "hotspots_heading": "问题热点文件",
        "hotspots_count": "问题数",
        "hotspots_top_severity": "最高严重性",
        "trend_heading": "趋势对比（上次扫描）",
        "trend_no_previous": "首次扫描，无历史对比",
        "trend_prev_time": "上次扫描时间",
        "trend_score_change": "健康评分变化",
        "trend_new": "新增问题",
        "trend_resolved": "已解决",
        "trend_persist": "持续存在",
        "findings_none": "未发现问题，代码质量良好",
        "file": "文件",
        "category": "类别",
        "confidence": "置信度",
        "suggestion": "建议",
        "repo_wide": "仓库级",
        "footer": f"*此报告由 [Sakura AI]({SAKURA_AI_REPO_URL}) 自动生成*",
        "superseded": ("此报告 Issue 已被最新一次扫描报告取代，自动关闭。"),
        "tg_title": "Sakura AI 仓库扫描完成",
        "tg_summary": "总结",
        "tg_files": "扫描文件",
        "tg_health": "健康评分",
        "tg_stats": "问题统计",
        "tg_tokens": "Token 消耗",
        "tg_clean": "未发现问题，代码质量良好",
        "tg_view": "查看详细报告",
        "tg_webui": "WebUI 查看详情",
    },
    "en": {
        "title": "Sakura AI Repository Scan Report",
        "summary_heading": "Scan Summary",
        "overview_heading": "Scan Overview",
        "repo": "Repository",
        "scan_time": "Scan time",
        "commit": "Commit",
        "trigger": "Trigger",
        "files": "Files scanned",
        "chunks": "Indexed chunks",
        "rounds": "AI rounds",
        "tokens": "Token usage",
        "duration": "Duration",
        "health": "Health score",
        "stats_heading": "Findings",
        "matrix_heading": "Severity × Category",
        "hotspots_heading": "Top hotspot files",
        "hotspots_count": "Findings",
        "hotspots_top_severity": "Highest severity",
        "trend_heading": "Trend vs previous scan",
        "trend_no_previous": "First scan, no history to compare",
        "trend_prev_time": "Previous scan",
        "trend_score_change": "Health score change",
        "trend_new": "New",
        "trend_resolved": "Resolved",
        "trend_persist": "Persisting",
        "findings_none": "No findings. Code quality looks good.",
        "file": "File",
        "category": "Category",
        "confidence": "Confidence",
        "suggestion": "Suggestion",
        "repo_wide": "repository-wide",
        "footer": f"*This report was generated automatically by [Sakura AI]({SAKURA_AI_REPO_URL})*",
        "superseded": "This report issue has been superseded by a newer scan report and is closed automatically.",
        "tg_title": "Sakura AI repository scan completed",
        "tg_summary": "Summary",
        "tg_files": "Files scanned",
        "tg_health": "Health score",
        "tg_stats": "Findings",
        "tg_tokens": "Token usage",
        "tg_clean": "No findings. Code quality looks good.",
        "tg_view": "View full report",
        "tg_webui": "Open in WebUI",
    },
}


def _text(language: str | None) -> dict:
    return _TEXT.get(language or "zh-CN", _TEXT["zh-CN"])


def _format_duration(scan) -> str:
    started = getattr(scan, "started_at", None)
    completed = getattr(scan, "completed_at", None)
    if not started or not completed:
        return "-"
    delta = (completed - started).total_seconds()
    if delta < 0:
        return "-"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m{int(delta % 60)}s"
    return f"{int(delta // 3600)}h{int((delta % 3600) // 60)}m"


def _escape_telegram_markdown(value: str, max_length: int) -> str:
    """Escape untrusted Markdown and avoid ending on a dangling escape."""
    escaped = escape_markdown(value, version=1)
    truncated = escaped[:max_length]
    trailing_slashes = len(truncated) - len(truncated.rstrip("\\"))
    if trailing_slashes % 2:
        truncated = truncated[:-1]
    return truncated


def _finding_key(f) -> tuple[str, str]:
    return (getattr(f, "file_path", None) or "", getattr(f, "title", "") or "")


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
        from sqlalchemy import desc, select

        from backend.models.database import async_session
        from backend.models.scan_models import RepoScan, ScanFinding, ScanStatus

        # 加载扫描记录、findings 与上一次完成扫描（趋势对比 + 关闭旧 Issue）
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

            previous_scan = None
            previous_findings: list = []
            prev_result = await session.execute(
                select(RepoScan)
                .where(
                    RepoScan.repo_name == scan.repo_name,
                    RepoScan.status == ScanStatus.COMPLETED.value,
                    RepoScan.id != scan.id,
                )
                .order_by(desc(RepoScan.completed_at))
                .limit(1)
            )
            previous_scan = prev_result.scalar_one_or_none()
            if previous_scan is not None:
                prev_findings_result = await session.execute(
                    select(ScanFinding).where(ScanFinding.scan_id == previous_scan.id)
                )
                previous_findings = list(prev_findings_result.scalars().all())

        # 用直接传递的聚合数据覆盖可能过期的 DB 值
        if report_data:
            for key, value in report_data.items():
                if hasattr(scan, key) and value is not None:
                    setattr(scan, key, value)

        language = (report_data or {}).get("output_language") or "zh-CN"
        report_info = {}

        # 创建 GitHub Issue（自动创建已固定开启，有发现才报告）
        if scan.total_findings > 0:
            issue_info = await self._create_github_issue(
                scan,
                findings,
                previous_scan=previous_scan,
                previous_findings=previous_findings,
                language=language,
            )
            if issue_info:
                report_info.update(issue_info)

        # 发送 Telegram 通知（使用刚创建的 Issue URL）
        if settings.scan_send_telegram:
            logger.info(f"正在发送扫描 Telegram 通知: {scan.repo_name}")
            issue_url = report_info.get("issue_url") or scan.report_issue_url
            await self._send_telegram_notification(
                scan, issue_url=issue_url, language=language
            )
        else:
            logger.info("Telegram 扫描通知已禁用")

        return report_info

    def generate_issue_body(
        self,
        scan: RepoScan,
        findings: list[ScanFinding],
        previous_scan: RepoScan | None = None,
        previous_findings: list[ScanFinding] | None = None,
        language: str = "zh-CN",
    ) -> str:
        """生成 GitHub Issue 报告 Markdown 内容（高密度布局 + 趋势对比）"""
        t = _text(language)
        lines: list[str] = []

        # 标题区 + AI 总结
        lines.append(f"## {t['title']}\n")

        summary = (getattr(scan, "summary", None) or "").strip()
        if summary:
            lines.append(f"### {t['summary_heading']}\n")
            lines.append(f"> {summary.replace(chr(10), chr(10) + '> ')}\n")

        # 扫描概览（单表高密度）
        scan_time = (
            get_time_service().format_display(scan.created_at)
            if scan.created_at
            else "-"
        )
        commit_short = scan.commit_sha[:7] if scan.commit_sha else "-"
        health = scan.overall_health_score or 0
        health_emoji = "🟢" if health >= 80 else "🟡" if health >= 60 else "🔴"
        total_tokens = (getattr(scan, "prompt_tokens", 0) or 0) + (
            getattr(scan, "completion_tokens", 0) or 0
        )
        scan_rounds = getattr(scan, "scan_rounds", None)
        indexed_chunks = getattr(scan, "indexed_chunks", None)

        lines.append(f"### {t['overview_heading']}\n")
        lines.append("| | |")
        lines.append("|---|---|")
        lines.append(f"| {t['repo']} | `{scan.repo_name}` |")
        lines.append(f"| {t['commit']} | `{commit_short}` |")
        lines.append(f"| {t['scan_time']} | {scan_time} |")
        lines.append(
            f"| {t['trigger']} | {getattr(scan, 'trigger_type', None) or '-'} |"
        )
        lines.append(f"| {t['files']} | {getattr(scan, 'code_file_count', 0) or 0} |")
        if indexed_chunks:
            lines.append(f"| {t['chunks']} | {indexed_chunks} |")
        if scan_rounds:
            lines.append(f"| {t['rounds']} | {scan_rounds} |")
        if total_tokens:
            lines.append(f"| {t['tokens']} | {total_tokens:,} |")
        duration = _format_duration(scan)
        if duration != "-":
            lines.append(f"| {t['duration']} | {duration} |")
        lines.append(f"| {health_emoji} {t['health']} | **{health}/100** |")
        lines.append("")

        # 问题统计
        lines.append(f"### {t['stats_heading']}\n")
        lines.append("| | |")
        lines.append("|---|---|")
        if scan.critical_count or 0:
            lines.append(f"| 🔴 Critical | {scan.critical_count} |")
        if scan.major_count or 0:
            lines.append(f"| 🟡 Major | {scan.major_count} |")
        if scan.minor_count or 0:
            lines.append(f"| 🟠 Minor | {scan.minor_count} |")
        if scan.suggestion_count or 0:
            lines.append(f"| 💡 Suggestion | {scan.suggestion_count} |")
        if not (
            scan.critical_count
            or scan.major_count
            or scan.minor_count
            or scan.suggestion_count
        ):
            lines.append(f"| - | {t['findings_none']} |")
        lines.append("")

        # 严重性 × 类别矩阵
        if findings:
            categories = [
                c for c in _CATEGORY_ORDER if any(f.category == c for f in findings)
            ]
            matrix: dict[tuple[str, str], int] = {}
            for f in findings:
                key = (f.severity, f.category)
                matrix[key] = matrix.get(key, 0) + 1
            if categories:
                lines.append(f"### {t['matrix_heading']}\n")
                lines.append("| " + " | ".join(["-", *categories]) + " |")
                lines.append("|" + "---|" * (len(categories) + 1))
                for sev in ["critical", "major", "minor", "suggestion"]:
                    row = [str(matrix.get((sev, cat), 0) or "-") for cat in categories]
                    lines.append(f"| {sev} | " + " | ".join(row) + " |")
                lines.append("")

            # 热点文件
            file_counts: dict[str, list] = {}
            for f in findings:
                if f.file_path:
                    file_counts.setdefault(f.file_path, []).append(f)
            if file_counts:
                hotspots = sorted(
                    file_counts.items(),
                    key=lambda item: (
                        -len(item[1]),
                        min(_SEVERITY_ORDER.get(x.severity, 3) for x in item[1]),
                        item[0],
                    ),
                )[:5]
                lines.append(f"### {t['hotspots_heading']}\n")
                lines.append(
                    f"| {t['file']} | {t['hotspots_count']} | {t['hotspots_top_severity']} |"
                )
                lines.append("|---|---|---|")
                for file_path, items in hotspots:
                    top = min(_SEVERITY_ORDER.get(x.severity, 3) for x in items)
                    top_name = next(
                        name
                        for name, weight in _SEVERITY_ORDER.items()
                        if weight == top
                    )
                    lines.append(f"| `{file_path}` | {len(items)} | {top_name} |")
                lines.append("")

        # 趋势对比
        lines.append(f"### {t['trend_heading']}\n")
        if previous_scan is not None:
            prev_time = (
                get_time_service().format_display(previous_scan.completed_at)
                if previous_scan.completed_at
                else "-"
            )
            prev_health = previous_scan.overall_health_score
            cur_health = scan.overall_health_score or 0
            delta = cur_health - prev_health if prev_health is not None else None
            current_keys = {_finding_key(f) for f in findings}
            previous_keys = {_finding_key(p) for p in (previous_findings or [])}
            added = len(current_keys - previous_keys)
            resolved = len(previous_keys - current_keys)
            persisting = len(current_keys & previous_keys)
            score_text = (
                f"{delta:+d} ({prev_health} → {cur_health})"
                if delta is not None
                else "-"
            )
            lines.append("| | |")
            lines.append("|---|---|")
            lines.append(f"| {t['trend_prev_time']} | {prev_time} |")
            lines.append(f"| {t['trend_score_change']} | {score_text} |")
            lines.append(f"| {t['trend_new']} | {added} |")
            lines.append(f"| {t['trend_resolved']} | {resolved} |")
            lines.append(f"| {t['trend_persist']} | {persisting} |")
        else:
            lines.append(t["trend_no_previous"])
        lines.append("")

        # Findings 明细：critical/major 展开，minor/suggestion 折叠
        if findings:
            grouped: dict[str, list] = {}
            for f in findings:
                grouped.setdefault(f.severity, []).append(f)

            for sev in ["critical", "major", "minor", "suggestion"]:
                items = grouped.get(sev, [])
                if not items:
                    continue

                emoji = SEVERITY_EMOJI.get(sev, "💡")
                open_attr = " open" if _SEVERITY_ORDER.get(sev, 3) <= 1 else ""
                lines.append(
                    f"<details{open_attr}>\n"
                    f"<summary>{emoji} {sev.upper()} ({len(items)})</summary>\n"
                )
                for idx, f in enumerate(items, 1):
                    lines.append(f"**{idx}. {f.title}**\n")
                    meta: list[str] = []
                    if f.file_path:
                        loc = f.file_path
                        if f.line_start:
                            loc += f":{f.line_start}"
                            if f.line_end and f.line_end != f.line_start:
                                loc += f"-{f.line_end}"
                        meta.append(f"`{loc}`")
                    meta.append(f.category)
                    if f.confidence is not None:
                        meta.append(f"{t['confidence']} {f.confidence}%")
                    if meta:
                        lines.append(" · ".join(meta) + "\n")
                    if f.description:
                        lines.append(f"{f.description}\n")
                    if f.suggestion:
                        lines.append(f"> **{t['suggestion']}**: {f.suggestion}\n")
                    lines.append("")

                lines.append("</details>\n")

        lines.append("---")
        lines.append(t["footer"])

        return "\n".join(lines)

    def generate_telegram_message(
        self, scan, issue_url: str | None = None, language: str = "zh-CN"
    ) -> str:
        """生成 Telegram 通知消息"""
        t = _text(language)
        health = scan.overall_health_score or 0
        health_emoji = "🟢" if health >= 80 else "🟡" if health >= 60 else "🔴"

        summary = (getattr(scan, "summary", None) or "").strip()
        lines = [
            f"*{t['tg_title']}*",
            "",
            f"仓库: `{scan.repo_name}`",
        ]

        if scan.commit_sha:
            lines.append(f"Commit: `{scan.commit_sha[:7]}`")
        duration = _format_duration(scan)
        if duration != "-":
            lines.append(f"{t['duration']}: {duration}")
        lines.append(f"{t['tg_files']}: {scan.code_file_count or 0}")
        lines.append("")

        if summary:
            safe_summary = _escape_telegram_markdown(summary, 300)
            lines.append(f"{t['tg_summary']}: {safe_summary}")
            lines.append("")

        lines.append(f"{health_emoji} {t['tg_health']}: *{health}/100*")
        lines.append("")

        total = scan.total_findings or 0
        if total > 0:
            lines.append(f"*{t['tg_stats']}*")
            if scan.critical_count or 0:
                lines.append(f" 🔴 Critical: {scan.critical_count}")
            if scan.major_count or 0:
                lines.append(f" 🟡 Major: {scan.major_count}")
            if scan.minor_count or 0:
                lines.append(f" 🟠 Minor: {scan.minor_count}")
            if scan.suggestion_count or 0:
                lines.append(f" 💡 Suggestion: {scan.suggestion_count}")
            lines.append("")

            total_tokens = (getattr(scan, "prompt_tokens", 0) or 0) + (
                getattr(scan, "completion_tokens", 0) or 0
            )
            if total_tokens > 0:
                lines.append(f"{t['tg_tokens']}: {total_tokens:,}")
                lines.append("")
        else:
            lines.append(f"✅ {t['tg_clean']}")
            lines.append("")

        # 链接：如有 Issue 链接则展示；始终提供 WebUI 链接（若 app_domain 已配置）
        webui_url = get_webui_url(f"/scans/{scan.id}")
        logger.debug(f"WebUI URL for scan {scan.id}: {webui_url!r}")
        link_url = issue_url or scan.report_issue_url
        if link_url:
            lines.append(f"[{t['tg_view']}]({link_url})")
        if webui_url:
            lines.append(f"[{t['tg_webui']}]({webui_url})")
        else:
            logger.warning(f"app_domain 未配置，跳过 WebUI 链接 (scan_id={scan.id})")

        return "\n".join(lines)

    async def _close_previous_issue(
        self, repo, scan, previous_scan, language: str
    ) -> None:
        """关闭上一次扫描的报告 Issue（每仓库保持最多一个 open 报告）"""
        issue_number = getattr(previous_scan, "report_issue_number", None)
        if not issue_number:
            return
        try:
            previous_issue = await asyncio.to_thread(repo.get_issue, int(issue_number))
            if previous_issue is None or previous_issue.state != "open":
                return
            await asyncio.to_thread(
                previous_issue.create_comment,
                _text(language)["superseded"],
            )
            await asyncio.to_thread(
                previous_issue.edit,
                state="closed",
            )
            logger.info(f"已关闭旧扫描报告 Issue: {scan.repo_name}#{issue_number}")
        except Exception as e:
            logger.warning(
                f"关闭旧扫描报告 Issue 失败（不阻断新报告）: "
                f"{getattr(previous_scan, 'report_issue_number', '?')}, {e}"
            )

    async def _create_github_issue(
        self,
        scan: RepoScan,
        findings: list[ScanFinding],
        previous_scan: RepoScan | None = None,
        previous_findings: list[ScanFinding] | None = None,
        language: str = "zh-CN",
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
            body = self.generate_issue_body(
                scan,
                findings,
                previous_scan=previous_scan,
                previous_findings=previous_findings,
                language=language,
            )
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

                # 只有新 Issue 创建成功后，才关闭上一次扫描的报告 Issue。
                # 关闭失败由 helper 记录并吞掉，不能阻断新报告交付。
                if previous_scan is not None:
                    await self._close_previous_issue(repo, scan, previous_scan, language)

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
        self, scan, issue_url: str | None = None, language: str = "zh-CN"
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

            text = self.generate_telegram_message(
                scan, issue_url=issue_url, language=language
            )
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
