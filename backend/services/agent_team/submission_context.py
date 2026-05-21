"""Agent Team task submission context helpers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_dynamic_config, get_settings
from backend.models.agent_team_models import AgentTeamSourceType
from backend.models.database import IssueAnalysis, IssueAnalysisStatus
from backend.services.agent_team.fullstack_expert import (
    FULLSTACK_SYSTEM_PROMPT,
    build_fullstack_user_message,
)


def json_loads(value: Any, fallback: Any) -> Any:
    """Safely parse a JSON value with a fallback."""
    if value in (None, ""):
        return fallback
    if isinstance(value, dict | list):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def json_list(value: Any) -> list:
    loaded = json_loads(value, [])
    return loaded if isinstance(loaded, list) else []


def json_dict(value: Any) -> dict:
    loaded = json_loads(value, {})
    return loaded if isinstance(loaded, dict) else {}


def format_issue_analysis_context(analysis: IssueAnalysis | None) -> dict | None:
    """Convert IssueAnalysis into a template and prompt friendly dictionary."""
    if analysis is None:
        return None

    detail = json_dict(analysis.analysis_detail)
    detail_json = ""
    if detail:
        detail_json = json.dumps(detail, ensure_ascii=False, indent=2)

    repo_name = analysis.repo_name or ""
    repo_full_name = repo_name
    if repo_name and "/" not in repo_name and analysis.repo_owner:
        repo_full_name = f"{analysis.repo_owner}/{repo_name}"
    return {
        "id": analysis.id,
        "issue_number": analysis.issue_number,
        "repo_full_name": repo_full_name,
        "author": analysis.author,
        "title": analysis.title,
        "category": analysis.category or detail.get("category"),
        "priority": analysis.priority or detail.get("priority"),
        "summary": analysis.summary or detail.get("summary"),
        "feasibility": analysis.feasibility or detail.get("feasibility"),
        "suggested_title": analysis.suggested_title or detail.get("suggested_title"),
        "suggested_labels": json_list(analysis.suggested_labels)
        or detail.get("suggested_labels", []),
        "suggested_assignees": json_list(analysis.suggested_assignees)
        or detail.get("suggested_assignees", []),
        "related_prs": json_list(analysis.related_prs)
        or detail.get("related_prs", []),
        "duplicate_of": analysis.duplicate_of or detail.get("duplicate_of"),
        "status": analysis.status,
        "error_message": analysis.error_message,
        "prompt_tokens": analysis.prompt_tokens or 0,
        "completion_tokens": analysis.completion_tokens or 0,
        "estimated_cost": analysis.estimated_cost or 0,
        "comment_posted": analysis.comment_posted,
        "comment_url": analysis.comment_url,
        "analysis_detail_json": detail_json,
        "created_at": analysis.created_at,
        "completed_at": analysis.completed_at,
    }


def format_issue_comments(comments: list, bot_username: str | None = None) -> list[dict]:
    """Convert GitHub issue comments into dictionaries for prompts and templates."""
    bot_login = (bot_username or "").lower()
    formatted = []
    for comment in comments:
        user = getattr(comment, "user", None)
        author = getattr(user, "login", "") or "unknown"
        author_lower = author.lower()
        user_type = (getattr(user, "type", "") or "").lower()
        is_bot = user_type == "bot" or author_lower.endswith("[bot]")
        if bot_login and author_lower == bot_login:
            is_bot = True
        body = getattr(comment, "body", "") or ""
        if not body.strip():
            continue
        formatted.append(
            {
                "id": getattr(comment, "id", None),
                "author": author,
                "body": body,
                "created_at": getattr(comment, "created_at", None),
                "updated_at": getattr(comment, "updated_at", None),
                "html_url": getattr(comment, "html_url", None),
                "author_association": getattr(comment, "author_association", None),
                "is_bot": is_bot,
            }
        )
    return formatted


async def load_issue_analysis_for_context(
    db: AsyncSession,
    *,
    source_type: str | None,
    source_id: int | None,
    repo_owner: str | None,
    repo_name: str | None,
    repo_full_name: str | None,
    issue_number: int | None,
) -> IssueAnalysis | None:
    """Load the IssueAnalysis record related to an Agent task or draft."""
    if source_type == AgentTeamSourceType.ISSUE_ANALYSIS.value and source_id:
        analysis = await db.get(IssueAnalysis, source_id)
        if analysis is not None:
            return analysis

    if not issue_number:
        return None

    repo_names = {name for name in (repo_name, repo_full_name) if name}
    filters = [
        IssueAnalysis.issue_number == issue_number,
        IssueAnalysis.status == IssueAnalysisStatus.COMPLETED.value,
    ]
    if repo_owner:
        filters.append(IssueAnalysis.repo_owner == repo_owner)
    if repo_names:
        filters.append(IssueAnalysis.repo_name.in_(repo_names))

    result = await db.execute(
        select(IssueAnalysis)
        .where(*filters)
        .order_by(desc(IssueAnalysis.analysis_version), desc(IssueAnalysis.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_issue_comments_for_context(
    *,
    repo_owner: str | None,
    repo_name: str | None,
    issue_number: int | None,
) -> list[dict]:
    """Load GitHub Issue comments for Agent task context."""
    if not (repo_owner and repo_name and issue_number):
        return []

    try:
        max_count = int(await get_dynamic_config("issue_max_comments_in_context") or 0)
    except (TypeError, ValueError):
        max_count = 0

    try:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        comments = await asyncio.to_thread(
            github_app.get_issue_comments,
            repo_owner,
            repo_name,
            int(issue_number),
        )
    except Exception as exc:
        logger.warning(
            "获取 Agent 任务关联 Issue 评论失败: {}/{}#{}: {}",
            repo_owner,
            repo_name,
            issue_number,
            exc,
        )
        return []

    if max_count > 0:
        comments = comments[-max_count:]
    return format_issue_comments(comments, get_settings().bot_username)


def _format_markdown_items(items: list) -> str:
    lines = []
    for item in items:
        if isinstance(item, dict):
            text = (
                item.get("name")
                or item.get("label")
                or item.get("username")
                or item.get("login")
                or item.get("title")
                or item.get("number")
                or json.dumps(item, ensure_ascii=False)
            )
            reason = item.get("reason") or item.get("description") or item.get("state")
            if reason:
                lines.append(f"- {text}: {reason}")
            else:
                lines.append(f"- {text}")
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def build_issue_context_markdown(
    *,
    repo_full_name: str | None,
    issue_number: int | None,
    issue_analysis_context: dict | None,
    issue_comments: list[dict],
) -> str:
    """Build the Issue-specific markdown appended to the Agent task context."""
    if not (repo_full_name or issue_number or issue_analysis_context or issue_comments):
        return ""

    parts = ["## GitHub Issue 上下文\n"]
    if repo_full_name:
        parts.append(f"仓库: {repo_full_name}\n")
    if issue_number:
        parts.append(f"Issue: #{issue_number}\n")

    if issue_analysis_context:
        analysis = issue_analysis_context
        parts.append("\n### Issue AI 分析\n")
        for label, key in (
            ("标题", "title"),
            ("作者", "author"),
            ("分类", "category"),
            ("优先级", "priority"),
            ("摘要", "summary"),
            ("可行性", "feasibility"),
            ("建议标题", "suggested_title"),
        ):
            value = analysis.get(key)
            if value:
                parts.append(f"{label}: {value}\n")
        if analysis.get("suggested_labels"):
            parts.append("\n建议标签:\n")
            parts.append(_format_markdown_items(analysis["suggested_labels"]))
            parts.append("\n")
        if analysis.get("suggested_assignees"):
            parts.append("\n建议指派人:\n")
            parts.append(_format_markdown_items(analysis["suggested_assignees"]))
            parts.append("\n")
        if analysis.get("related_prs"):
            parts.append("\n相关 PR:\n")
            parts.append(_format_markdown_items(analysis["related_prs"]))
            parts.append("\n")
        if analysis.get("duplicate_of"):
            parts.append(f"\n可能重复: #{analysis['duplicate_of']}\n")
        if analysis.get("analysis_detail_json"):
            parts.append("\n完整分析详情:\n")
            parts.append(f"```json\n{analysis['analysis_detail_json']}\n```\n")
    else:
        parts.append("\n### Issue AI 分析\n暂无已完成的 Issue AI 分析。\n")

    if issue_comments:
        parts.append("\n### Issue 评论讨论\n")
        for index, comment in enumerate(issue_comments, start=1):
            role = "AI/Bot" if comment.get("is_bot") else "User"
            author = comment.get("author") or "unknown"
            created_at = comment.get("created_at")
            timestamp = f" · {created_at}" if created_at else ""
            parts.append(f"\n#### 评论 {index}: @{author} ({role}{timestamp})\n")
            parts.append(f"{comment.get('body') or ''}\n")
    else:
        parts.append("\n### Issue 评论讨论\n暂无 Issue 评论。\n")

    return "".join(parts).strip()


def build_agent_task_summary(task_summary: str, issue_context_markdown: str = "") -> str:
    """Combine the editable task summary with Issue context sent to the Agent."""
    parts = []
    if task_summary.strip():
        parts.append(task_summary.strip())
    if issue_context_markdown.strip():
        parts.append(issue_context_markdown.strip())
    return "\n\n".join(parts)


def build_agent_submission_preview(
    *,
    task_title: str,
    task_summary: str,
    source_type: str = "",
    source_issue_number: int | None = None,
    sakura_memory: str = "",
    skills_summary: str = "",
    role_memory_context: str = "",
    handoff_context: str = "",
    feedback: str = "",
) -> str:
    """Build the first user message submitted to the fullstack Agent."""
    return build_fullstack_user_message(
        task_title=task_title,
        task_summary=task_summary,
        source_type=source_type,
        source_issue_number=source_issue_number,
        sakura_memory=sakura_memory,
        skills_summary=skills_summary,
        role_memory_context=role_memory_context,
        handoff_context=handoff_context,
        feedback=feedback,
    )


def build_agent_submission_context_preview(
    *,
    task_title: str,
    task_summary: str,
    source_type: str = "",
    source_issue_number: int | None = None,
    sakura_memory: str = "",
    skills_summary: str = "",
    role_memory_context: str = "",
    handoff_context: str = "",
    feedback: str = "",
    system_prompt: str = FULLSTACK_SYSTEM_PROMPT,
) -> str:
    """Build a role-separated preview of the messages sent to the fullstack Agent."""
    user_message = build_agent_submission_preview(
        task_title=task_title,
        task_summary=task_summary,
        source_type=source_type,
        source_issue_number=source_issue_number,
        sakura_memory=sakura_memory,
        skills_summary=skills_summary,
        role_memory_context=role_memory_context,
        handoff_context=handoff_context,
        feedback=feedback,
    )
    return f"## system\n{system_prompt.strip()}\n\n## user\n{user_message.strip()}"
