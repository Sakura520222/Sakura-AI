"""PR-Issue 关联分析器"""

import asyncio
import re
from typing import Any

from loguru import logger

from backend.core.config import get_strategy_config
from backend.core.github_app import GitHubAppClient
from backend.services.pr_body import (
    replace_sakura_generated_section,
    strip_sakura_generated_sections,
)


class PRIssueLinker:
    """PR-Issue 关联分析器"""

    # PR body 中语义关联区域的 HTML 标记（幂等更新）
    ISSUE_LINKS_START = "<!-- sakura-ai-issue-links-start -->"
    ISSUE_LINKS_END = "<!-- sakura-ai-issue-links-end -->"

    def __init__(self):
        self.github_app = GitHubAppClient()
        config = get_strategy_config().get_issue_analysis_config()
        keywords = config.get("issue_reference_keywords", [])
        self._reference_pattern = re.compile(
            r"(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\s+#(\d+)",
            re.IGNORECASE,
        )
        self._max_issues = config.get("max_linked_issues_in_prompt", 3)

    async def parse_issue_references(self, pr_body: str) -> list[int]:
        """从 PR 描述中解析 Issue 引用"""
        if not pr_body:
            return []
        return list(
            {
                int(m.group(1))
                for m in self._reference_pattern.finditer(
                    strip_sakura_generated_sections(pr_body)
                )
            }
        )

    async def fetch_issue_content(
        self, repo_owner: str, repo_name: str, issue_numbers: list[int]
    ) -> list[dict[str, Any]]:
        """从 GitHub 获取 Issue 内容"""
        issues = []
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(
                    self.github_app.get_issue, repo_owner, repo_name, num
                )
                if issue:
                    issues.append(
                        {
                            "number": issue.number,
                            "title": issue.title,
                            "body": issue.body or "",
                            "state": issue.state,
                            "labels": [label.name for label in issue.labels],
                        }
                    )
            except Exception as e:
                logger.warning(f"获取 Issue #{num} 失败: {e}")
        return issues

    async def inject_issue_context(
        self, context: dict[str, Any], issue_contents: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """将 Issue 内容注入到审查上下文中"""
        if not issue_contents:
            return context

        context["linked_issues"] = issue_contents[: self._max_issues]
        context["linked_issue_numbers"] = [i["number"] for i in issue_contents]
        return context

    def format_related_issues_section(
        self, explicit_issues: list[dict[str, Any]]
    ) -> str:
        """格式化关联 Issue 信息（用于 Review 评论展示）"""
        if not explicit_issues:
            return ""

        lines = ["### 📎 关联 Issue\n"]
        for issue in explicit_issues[: self._max_issues]:
            state_icon = "🟢" if issue.get("state") == "open" else "🔴"
            labels = ", ".join(issue.get("labels", []))
            label_str = f" | 标签: {labels}" if labels else ""
            lines.append(
                f"- {state_icon} **#{issue['number']}: {issue['title']}** ({issue.get('state', 'unknown')}{label_str})"
            )

            body = issue.get("body", "")
            if body:
                summary = body[:200] + "..." if len(body) > 200 else body
                lines.append(f"  > {summary}")

        return "\n".join(lines)

    def build_updated_pr_body(
        self, original_body: str, related_issues: list[dict[str, Any]]
    ) -> str:
        """Replace the entire machine-owned set, including successful emptiness."""
        if not related_issues:
            return replace_sakura_generated_section(original_body, "issue-links", "")
        lines = [self.ISSUE_LINKS_START, ""]
        for issue in related_issues:
            prefix = "Closes" if issue.get("relation") == "closes" else "Related to"
            lines.append(f"{prefix} #{issue['number']}")
        lines.extend(["", self.ISSUE_LINKS_END])
        return replace_sakura_generated_section(
            original_body, "issue-links", "\n".join(lines)
        )
