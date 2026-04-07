"""PR 变更摘要模块

调用辅助 AI 对 PR 的变更内容进行自动总结，
将总结内容追加到 PR body 描述后面（使用 HTML 注释标记界定区域），
后续新 commit 推送时通过标记定位并替换旧的 AI 摘要。
"""

import asyncio
import re
from typing import Any, Dict

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.pr_analyzer import PRAnalysis


class PRSummaryService:
    """PR 变更摘要服务"""

    # HTML 注释标记，用于定位 AI 摘要区域（GitHub 渲染时不可见）
    START_MARKER = "<!-- sakura-ai-summary-start -->"
    END_MARKER = "<!-- sakura-ai-summary-end -->"

    def __init__(self, api_client: AIApiClient, model: str):
        self.api_client = api_client
        self.model = model

    async def generate_summary(
        self, analysis: PRAnalysis, pr_info: Dict[str, Any]
    ) -> str:
        """生成 PR 变更摘要

        Args:
            analysis: PR 分析结果
            pr_info: PR 信息字典（包含 title、body 等）

        Returns:
            AI 生成的总结文本
        """
        system_prompt, user_message = self._build_prompts(analysis, pr_info)

        response = await self.api_client.call_with_retry(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
            timeout=60.0,
            max_tokens=1000,
        )

        summary_text = response.choices[0].message.content.strip()
        logger.info(f"PR 总结生成完成，长度: {len(summary_text)} 字符")
        return summary_text

    async def update_pr_body(
        self, pr: Any, summary: str, original_body: str
    ) -> None:
        """更新 PR body，追加或替换 AI 摘要部分

        Args:
            pr: PyGithub PullRequest 对象
            summary: AI 生成的总结文本
            original_body: PR 当前 body（可能包含之前的 AI 摘要）
        """
        original = self._extract_original_body(original_body)
        summary_block = self._build_summary_block(summary)
        new_body = f"{original}\n\n{summary_block}" if original.strip() else summary_block

        await asyncio.to_thread(pr.edit, body=new_body)
        logger.info("PR body 已更新（追加/替换 AI 摘要）")

    def _build_prompts(
        self, analysis: PRAnalysis, pr_info: Dict[str, Any]
    ) -> tuple[str, str]:
        """构建系统提示词和用户消息

        从 strategies.yaml 的 pr_summary 配置段加载模板。
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

        user_message = user_template.format(
            title=pr_info.get("title", ""),
            file_count=analysis.total_files,
            additions=analysis.total_additions,
            deletions=analysis.total_deletions,
            file_list=file_list,
            commits=commits,
        )

        return system_prompt, user_message

    def _build_file_list(self, analysis: PRAnalysis) -> str:
        """构建变更文件列表文本"""
        lines = []
        for f in analysis.code_files:
            status_icon = {"added": "+", "modified": "~", "deleted": "-"}.get(
                f.status, "?"
            )
            lines.append(
                f"  {status_icon} {f.path} (+{f.additions}/-{f.deletions})"
            )
        if not lines:
            return "（无代码文件变更）"
        return "\n".join(lines[:50])  # 最多显示 50 个文件

    def _build_commit_info(
        self, analysis: PRAnalysis, pr_info: Dict[str, Any]
    ) -> str:
        """构建 commit 信息文本"""
        commits = analysis.new_commits
        if not commits:
            return "（无可用的 commit 信息）"

        lines = []
        for c in commits[:20]:  # 最多显示 20 条
            lines.append(f"  - {c['sha'][:7]} {c['title']}")
        return "\n".join(lines)

    def _build_summary_block(self, summary: str) -> str:
        """构建带标记的摘要块"""
        return (
            f"{self.START_MARKER}\n\n"
            f"## 🌸 AI 变更总结\n\n{summary}\n\n"
            f"{self.END_MARKER}"
        )

    def _extract_original_body(self, body: str) -> str:
        """从 PR body 中提取不含 AI 摘要的原始内容"""
        if not body:
            return ""

        pattern = (
            re.escape(self.START_MARKER)
            + r".*?"
            + re.escape(self.END_MARKER)
        )
        # re.DOTALL 使 . 匹配换行符
        original = re.sub(pattern, "", body, flags=re.DOTALL).strip()
        return original
