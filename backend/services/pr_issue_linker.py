"""PR-Issue 关联分析器"""

import re
from typing import Dict, Any, List
from loguru import logger

from backend.core.github_app import GitHubAppClient
from backend.core.config import get_strategy_config


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

    async def parse_issue_references(self, pr_body: str) -> List[int]:
        """从 PR 描述中解析 Issue 引用"""
        if not pr_body:
            return []
        return list(
            set(int(m.group(1)) for m in self._reference_pattern.finditer(pr_body))
        )

    async def fetch_issue_content(
        self, repo_owner: str, repo_name: str, issue_numbers: List[int]
    ) -> List[Dict[str, Any]]:
        """从 GitHub 获取 Issue 内容"""
        issues = []
        for num in issue_numbers:
            try:
                issue = self.github_app.get_issue(repo_owner, repo_name, num)
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
        self, context: Dict[str, Any], issue_contents: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """将 Issue 内容注入到审查上下文中"""
        if not issue_contents:
            return context

        context["linked_issues"] = issue_contents[: self._max_issues]
        context["linked_issue_numbers"] = [i["number"] for i in issue_contents]
        return context

    def format_related_issues_section(
        self, explicit_issues: List[Dict[str, Any]]
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
        self, original_body: str, related_issues: List[Dict[str, Any]]
    ) -> str:
        """构建包含语义关联 issues 的 PR body

        使用 HTML 注释标记界定区域，支持幂等更新。
        参考 PRSummaryService 和 PRDependencyGraphService 的模式。

        Args:
            original_body: PR 原始 body
            related_issues: 语义关联的 issue 列表，含 number 字段

        Returns:
            更新后的 PR body
        """
        pattern = self._issue_links_pattern()

        if not related_issues:
            # 移除已有标记区域
            if self.ISSUE_LINKS_START in original_body:
                return re.sub(pattern, "", original_body, flags=re.DOTALL).rstrip()
            return original_body

        # 构建 "Related to #xxx" 引用列表
        lines = [self.ISSUE_LINKS_START, ""]
        for issue in related_issues:
            lines.append(f"Related to #{issue['number']}")
        lines.extend(["", self.ISSUE_LINKS_END])

        new_block = "\n".join(lines)

        # 替换已有标记区域或追加
        if self.ISSUE_LINKS_START in original_body:
            return re.sub(pattern, new_block, original_body, flags=re.DOTALL)
        else:
            return f"{original_body.rstrip()}\n\n{new_block}"

    def _issue_links_pattern(self) -> str:
        """语义关联区域的正则匹配模式"""
        return (
            f"{re.escape(self.ISSUE_LINKS_START)}.*?"
            f"{re.escape(self.ISSUE_LINKS_END)}"
        )
