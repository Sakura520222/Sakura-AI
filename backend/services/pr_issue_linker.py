"""PR-Issue 关联分析器"""

import re
from typing import Dict, Any, List, Optional
from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.core.config import get_settings, get_strategy_config


class PRIssueLinker:
    """PR-Issue 关联分析器"""

    def __init__(self):
        self.github_app = GitHubAppClient()
        settings = get_settings()
        config = get_strategy_config().get_issue_analysis_config()
        keywords = config.get("issue_reference_keywords", [])
        self._reference_pattern = re.compile(
            r'(?:' + '|'.join(re.escape(kw) for kw in keywords) + r')\s+#(\d+)',
            re.IGNORECASE
        )
        self._max_issues = config.get("max_linked_issues_in_prompt", 3)

    async def parse_issue_references(self, pr_body: str) -> List[int]:
        """从 PR 描述中解析 Issue 引用"""
        if not pr_body:
            return []
        return list(set(int(m.group(1)) for m in self._reference_pattern.finditer(pr_body)))

    async def fetch_issue_content(
        self, repo_owner: str, repo_name: str, issue_numbers: List[int]
    ) -> List[Dict[str, Any]]:
        """从 GitHub 获取 Issue 内容"""
        issues = []
        for num in issue_numbers:
            try:
                issue = self.github_app.get_issue(repo_owner, repo_name, num)
                if issue:
                    issues.append({
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body or "",
                        "state": issue.state,
                        "labels": [l.name for l in issue.labels],
                    })
            except Exception as e:
                logger.warning(f"获取 Issue #{num} 失败: {e}")
        return issues

    async def inject_issue_context(
        self, context: Dict[str, Any], issue_contents: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """将 Issue 内容注入到审查上下文中"""
        if not issue_contents:
            return context

        context["linked_issues"] = issue_contents[:self._max_issues]
        context["linked_issue_numbers"] = [i["number"] for i in issue_contents]
        return context

    def format_related_issues_section(
        self, explicit_issues: List[Dict[str, Any]]
    ) -> str:
        """格式化关联 Issue 信息（用于 Review 评论展示）"""
        if not explicit_issues:
            return ""

        lines = ["### 📎 关联 Issue\n"]
        for issue in explicit_issues[:self._max_issues]:
            state_icon = "🟢" if issue.get("state") == "open" else "🔴"
            labels = ", ".join(issue.get("labels", []))
            label_str = f" | 标签: {labels}" if labels else ""
            lines.append(f"- {state_icon} **#{issue['number']}: {issue['title']}** ({issue.get('state', 'unknown')}{label_str})")

            body = issue.get("body", "")
            if body:
                summary = body[:200] + "..." if len(body) > 200 else body
                lines.append(f"  > {summary}")

        return "\n".join(lines)
