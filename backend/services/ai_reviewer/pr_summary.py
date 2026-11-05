"""PR 变更摘要模块

调用辅助 AI 对 PR 的变更内容进行自动总结，
将总结内容追加到 PR body 描述后面（使用 HTML 注释标记界定区域），
后续新 commit 推送时通过标记定位并替换旧的 AI 摘要。
"""

import asyncio
from typing import Any

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.pr_analyzer import PRAnalysis
from backend.services.pr_body import (
    replace_sakura_generated_section,
    strip_sakura_generated_sections,
)


class PRSummaryService:
    """PR 变更摘要服务"""

    # HTML 注释标记，用于定位 AI 摘要区域（GitHub 渲染时不可见）
    START_MARKER = "<!-- sakura-ai-summary-start -->"
    END_MARKER = "<!-- sakura-ai-summary-end -->"

    def __init__(self, api_client: AIApiClient, model: str = ""):
        self.api_client = api_client

    async def generate_summary(
        self, analysis: PRAnalysis, pr_info: dict[str, Any], pr: Any = None
    ) -> str:
        """生成 PR 变更摘要

        Args:
            analysis: PR 分析结果
            pr_info: PR 信息字典（包含 title、body 等）
            pr: PyGithub PullRequest 对象（兼容调用参数；数据使用 analysis 快照）

        Returns:
            AI 生成的总结文本
        """
        system_prompt, user_message = self._build_prompts(analysis, pr_info)

        response = await self.api_client.call_with_retry(
            model="",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            output_token_cap=16000,
            role="summary",
        )

        if (
            not response.choices
            or not response.choices[0].message
            or not response.choices[0].message.content
        ):
            raise ValueError("AI 返回的总结内容为空")
        content = response.choices[0].message.content
        summary_text = content.strip()
        logger.info(f"PR 总结生成完成，长度: {len(summary_text)} 字符")
        return summary_text

    async def update_pr_body(self, pr: Any, summary: str) -> None:
        """更新 PR body，追加或替换 AI 摘要部分

        Args:
            pr: PyGithub PullRequest 对象
            summary: AI 生成的总结文本
        """
        # 从 GitHub 读取最新 body，避免用过期的 pr_info 缓存
        current_body = await asyncio.to_thread(lambda: pr.body or "")
        summary_block = self._build_summary_block(summary)
        new_body = replace_sakura_generated_section(
            current_body, "summary", summary_block
        )

        await asyncio.to_thread(pr.edit, body=new_body)
        logger.info("PR body 已更新（追加/替换 AI 摘要）")

    def _build_prompts(
        self,
        analysis: PRAnalysis,
        pr_info: dict[str, Any],
    ) -> tuple[str, str]:
        """构建系统提示词和用户消息

        从统一配置节存储（strategy.pr_summary 节）加载模板。
        """
        config = get_strategy_config()
        summary_cfg = config.config.get("pr_summary", {})

        system_prompt = summary_cfg.get(
            "system_prompt",
            "你是专业的代码审查助手，擅长总结代码变更。请用中文生成简洁清晰的 PR 变更总结。",
        )

        user_template = summary_cfg.get(
            "user_template",
            "请总结以下 PR 的变更内容：\n\nPR 标题: {title}\n变更文件数: {file_count}\n代码变更: +{additions}/-{deletions}",
        )

        # 构建变更文件列表
        file_list = self._build_file_list(analysis)

        # 构建 commit 信息
        commits = self._build_commit_info(analysis, pr_info)

        # Use the complete net PR snapshot for every field, never live counters
        # alongside incremental files/commits or an earlier generated summary.
        if analysis.pr_files is not None:
            additions = sum(file.additions for file in analysis.pr_files)
            deletions = sum(file.deletions for file in analysis.pr_files)
            file_count = len(analysis.pr_files)
        else:
            if analysis.is_incremental:
                raise ValueError("PR summary requires the complete net PR snapshot")
            additions = analysis.total_additions
            deletions = analysis.total_deletions
            file_count = analysis.total_files

        user_message = user_template.format(
            title=pr_info.get("title", ""),
            file_count=file_count,
            additions=additions,
            deletions=deletions,
            file_list=file_list,
            commits=commits,
        )

        system_prompt += (
            "\n\nSummarize only the current net PR changes in the supplied file list. "
            "The counts and file list describe the same complete PR snapshot. "
            "Commit messages are context, not evidence that inherited base-branch "
            "changes belong to this PR. Do not carry forward earlier generated claims."
        )

        # 注入输出语言指令 / Inject output language directive
        output_lang = get_settings().output_language
        if output_lang:
            language_names = {
                "zh-CN": "中文 (Simplified Chinese)",
                "en": "English",
            }
            lang_display = language_names.get(output_lang, output_lang)
            system_prompt += (
                f"\n\n**Important**: You MUST write the summary in {lang_display}."
            )

        return system_prompt, user_message

    def _build_file_list(self, analysis: PRAnalysis) -> str:
        """构建变更文件列表文本"""
        lines = []
        files = (
            analysis.pr_files if analysis.pr_files is not None else analysis.code_files
        )
        for f in files:
            status_icon = {
                "added": "+",
                "modified": "~",
                "deleted": "-",
                "removed": "-",
            }.get(f.status, "?")
            lines.append(f"  {status_icon} {f.path} (+{f.additions}/-{f.deletions})")
        if not lines:
            return "（无文件变更）"
        displayed = lines[:50]
        if len(lines) > len(displayed):
            displayed.append(f"（另有 {len(lines) - len(displayed)} 个文件未列出）")
        return "\n".join(displayed)

    def _build_commit_info(self, analysis: PRAnalysis, pr_info: dict[str, Any]) -> str:
        """构建 commit 信息文本

        使用完整 PR 的提交信息，排除同步目标分支带入的提交。
        """
        commits = analysis.pr_commits
        if commits is None and not analysis.is_incremental:
            commits = analysis.new_commits
        if not commits:
            return f"PR 标题: {pr_info.get('title', '（未知）')}"

        lines = []
        # Keep recent semantic evidence after long PR histories.
        displayed = commits[-20:]
        if len(commits) > len(displayed):
            lines.append(f"({len(commits) - len(displayed)} earlier commits omitted)")
        for c in displayed:
            sha = c.get("sha", "unknown")
            title = c.get("title", "")
            lines.append(f"  - {sha[:7]} {title}")
        return "\n".join(lines)

    def _build_summary_block(self, summary: str) -> str:
        """构建带标记的摘要块"""
        return (
            f"{self.START_MARKER}\n\n"
            f"## 🌸 Sakura AI的总结\n\n{summary}\n\n"
            f"{self.END_MARKER}"
        )

    def _extract_original_body(self, body: str) -> str:
        """Extract human evidence; body publication removes only its own section."""
        return strip_sakura_generated_sections(body)
