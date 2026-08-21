"""Agent task submission context helpers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.time_service import format_rfc3339
from backend.models.agent_team_models import AgentTeamSourceType
from backend.models.database import (
    IssueAnalysis,
    IssueAnalysisStatus,
    PRReview,
    ReviewComment,
)
from backend.models.scan_models import RepoScan, ScanFinding
from backend.services.agent_team.prompt_config import (
    IMPLEMENTATION_SYSTEM_PROMPT,
    build_implementation_user_message,
)


def json_loads(value: Any, fallback: Any) -> Any:
    """Safely parse a JSON value with a fallback."""
    if value in (None, ""):
        return fallback
    if isinstance(value, dict | list):
        return value
    try:
        return json.loads(value)
    except TypeError, json.JSONDecodeError:
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
        "body": getattr(analysis, "body", "") or "",
        "category": analysis.category or detail.get("category"),
        "priority": analysis.priority or detail.get("priority"),
        "summary": analysis.summary or detail.get("summary"),
        "feasibility": analysis.feasibility or detail.get("feasibility"),
        "suggested_title": analysis.suggested_title or detail.get("suggested_title"),
        "suggested_labels": json_list(analysis.suggested_labels)
        or detail.get("suggested_labels", []),
        "suggested_assignees": json_list(analysis.suggested_assignees)
        or detail.get("suggested_assignees", []),
        "related_prs": json_list(analysis.related_prs) or detail.get("related_prs", []),
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


def format_issue_comments(
    comments: list, bot_username: str | None = None
) -> list[dict]:
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

    return format_issue_comments(comments, get_settings().bot_username)


async def load_sakura_memory(repo_owner: str, repo_name: str) -> dict:
    """加载仓库对应的 Sakura 记忆上下文及 GitHub repo 对象。"""
    repo_full_name = f"{repo_owner}/{repo_name}"
    result: dict = {"text": "", "github_repo": None, "sakura_ref": None}
    try:
        # 延迟导入: 避免 submission_context 与 github_app/sakura_memory_service 循环依赖
        from backend.core.github_app import GitHubAppClient
        from backend.services.github_write_service import GitHubWriteService
        from backend.services.sakura_memory_service import SakuraMemoryService

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(repo_owner, repo_name)
        if not client:
            logger.info(
                "Agent 未注入 Sakura 记忆: 无法获取 GitHub 客户端 ({})",
                repo_full_name,
            )
            return result

        repo = client.get_repo(repo_full_name)
        result["github_repo"] = repo

        write_service = GitHubWriteService()
        result["sakura_ref"] = await write_service.get_sakura_branch(repo)

        service = SakuraMemoryService()
        context = await service.get_sakura_context(
            repo=repo,
            repo_full_name=repo_full_name,
        )
        if not context:
            logger.info(
                "Agent 未注入 Sakura 记忆: 仓库无可用上下文 ({})", repo_full_name
            )
            return result

        parts = []
        if context.get("sakura_md"):
            parts.append(f"### SAKURA.md\n{context['sakura_md']}")
        if context.get("memory_md"):
            parts.append(f"### memory.md\n{context['memory_md']}")

        logger.info(
            "Agent 已注入 Sakura 记忆: repo={}, files={}",
            repo_full_name,
            ", ".join(context.keys()),
        )
        result["text"] = "\n\n".join(parts)
    except Exception as e:
        logger.warning(
            "Agent 加载 Sakura 记忆失败: repo={}, error={}", repo_full_name, e
        )
    return result


async def load_skills_context() -> tuple[str, dict, list[dict]]:
    """加载已启用的 Agent Skills 上下文。"""
    # 延迟导入: 避免 submission_context 与 ai_client 循环依赖
    from backend.services.agent_team.ai_client import resolve_agent_team_bool_config

    enabled = await resolve_agent_team_bool_config(
        "agent_team_skills_enabled",
        get_settings().agent_team_skills_enabled,
    )
    if not enabled:
        logger.info("Agent Skills 未启用")
        return "", {}, []

    try:
        # 延迟导入: 避免 submission_context 与 database/skill_service 循环依赖
        from backend.models import database as db_module
        from backend.services.agent_team.skill_service import AgentSkillService

        service = AgentSkillService()
        async with db_module.async_session() as session:
            await service.ensure_builtin_skills(session)
            summary = await service.build_enabled_skills_summary(session)
            snapshot = await service.snapshot_enabled_skills(session)
        if not snapshot:
            logger.info("Agent Skills 已启用但无可用 Skill")
            return "", {}, []

        root = await service.resolve_root()
        context = {
            "skills_root": str(root),
            "skills_index": {skill["slug"]: skill for skill in snapshot},
            "skills_cache": {},
        }
        logger.info("Agent 已加载 Skills: count={}", len(snapshot))
        return summary, context, snapshot
    except Exception as exc:
        logger.warning("Agent Skills 加载失败: {}", exc)
        return "", {}, []


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
    issue_body: str = "",
) -> str:
    """Build the Issue-specific markdown appended to the Agent task context."""
    if not (
        repo_full_name
        or issue_number
        or issue_analysis_context
        or issue_comments
        or issue_body.strip()
    ):
        return ""

    parts = ["## GitHub Issue 上下文\n"]
    if repo_full_name:
        parts.append(f"仓库: {repo_full_name}\n")
    if issue_number:
        parts.append(f"Issue: #{issue_number}\n")

    if issue_body.strip():
        parts.append("\n### Issue 原文（第三方引用）\n")
        parts.append(f"{issue_body.strip()}\n")

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
            if created_at and hasattr(created_at, "isoformat"):
                created_at = format_rfc3339(created_at)
            timestamp = f" · {created_at}" if created_at else ""
            parts.append(f"\n#### 评论 {index}: @{author} ({role}{timestamp})\n")
            parts.append(f"{comment.get('body') or ''}\n")
    else:
        parts.append("\n### Issue 评论讨论\n暂无 Issue 评论。\n")

    return "".join(parts).strip()


async def load_agent_task_reference_context(
    db: AsyncSession,
    *,
    source_type: str | None,
    source_id: int | None,
    repo_owner: str | None,
    repo_name: str | None,
    repo_full_name: str | None,
    issue_number: int | None,
) -> str:
    """Load third-party Issue/PR material for the explicit reference section.

    Agent tasks persist the editable goal in ``summary`` for compatibility, but
    do not need a schema migration to persist a second prompt field.  Rebuild
    the reference projection from the source records at each run instead.  A
    missing GitHub/DB reference is non-fatal: the task can still execute with
    its originator goal and source metadata.
    """
    if source_type == AgentTeamSourceType.PR_REVIEW.value:
        return await _load_pr_review_reference_context(
            db,
            source_id=source_id,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
        )
    if source_type in {
        AgentTeamSourceType.SCAN_FINDING.value,
        AgentTeamSourceType.SCAN_REPORT_ISSUE.value,
    }:
        return await _load_scan_finding_reference_context(
            db,
            source_id=source_id,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
        )

    analysis = await load_issue_analysis_for_context(
        db,
        source_type=source_type,
        source_id=source_id,
        repo_owner=repo_owner,
        repo_name=repo_name,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
    )
    issue_analysis_context = format_issue_analysis_context(analysis)
    issue_comments = await load_issue_comments_for_context(
        repo_owner=repo_owner,
        repo_name=repo_name,
        issue_number=issue_number,
    )

    issue_body = getattr(analysis, "body", "") or ""
    if not issue_body and repo_owner and repo_name and issue_number:
        try:
            from backend.core.github_app import GitHubAppClient

            issue = await asyncio.to_thread(
                GitHubAppClient().get_issue,
                repo_owner,
                repo_name,
                int(issue_number),
            )
            issue_body = getattr(issue, "body", "") or ""
        except Exception as exc:
            logger.debug(
                "加载 Agent 任务 Issue 原文失败，继续使用已有引用: {}",
                exc,
            )

    return build_issue_context_markdown(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        issue_analysis_context=issue_analysis_context,
        issue_comments=issue_comments,
        issue_body=issue_body,
    )


async def _load_pr_review_reference_context(
    db: AsyncSession,
    *,
    source_id: int | None,
    repo_full_name: str | None,
    issue_number: int | None,
) -> str:
    """Render persisted Sakura PR Review data as untrusted reference text."""
    review = await db.get(PRReview, source_id) if source_id else None
    if review is None and repo_full_name and issue_number:
        owner, _, name = repo_full_name.partition("/")
        result = await db.execute(
            select(PRReview)
            .where(
                PRReview.repo_owner == owner,
                PRReview.repo_name == name,
                PRReview.pr_number == issue_number,
            )
            .order_by(desc(PRReview.id))
            .limit(1)
        )
        review = result.scalar_one_or_none()
    if review is None:
        return ""

    comments = (
        (
            await db.execute(
                select(ReviewComment)
                .where(ReviewComment.review_id == review.id)
                .order_by(ReviewComment.id)
            )
        )
        .scalars()
        .all()
    )
    parts = [
        "## GitHub PR Review reference",
        f"Repository: {repo_full_name or ''}",
        f"PR: #{issue_number or getattr(review, 'pr_number', '')}",
    ]
    if review.title:
        parts.append(f"Title: {review.title}")
    if review.branch:
        parts.append(f"Branch: {review.branch}")
    if review.review_summary:
        parts.extend(["", "### Review summary", review.review_summary])
    if comments:
        parts.extend(["", "### Review comments"])
        for index, comment in enumerate(comments, start=1):
            location = comment.file_path or ""
            if comment.line_number:
                location += f":{comment.line_number}"
            label = f" ({location})" if location else ""
            parts.extend(
                [
                    f"\n#### Comment {index}{label}",
                    comment.content or "",
                ]
            )
    return "\n".join(parts).strip()


async def _load_scan_finding_reference_context(
    db: AsyncSession,
    *,
    source_id: int | None,
    repo_full_name: str | None,
    issue_number: int | None,
) -> str:
    """Render a scan finding/report as untrusted reference text."""
    finding = await db.get(ScanFinding, source_id) if source_id else None
    if finding is not None:
        parts = [
            "## Repository scan finding reference",
            f"Repository: {repo_full_name or ''}",
            f"Finding: {finding.title}",
            f"Severity: {finding.severity}",
            f"Category: {finding.category}",
        ]
        if finding.file_path:
            parts.append(f"Location: {finding.file_path}")
        if finding.description:
            parts.extend(["", "### Finding description", finding.description])
        if finding.suggestion:
            parts.extend(["", "### Suggested remediation", finding.suggestion])
        if finding.code_snippet:
            parts.extend(["", "### Code snippet", finding.code_snippet])
        return "\n".join(parts).strip()

    scan = await db.get(RepoScan, source_id) if source_id else None
    if scan is None:
        return ""
    parts = [
        "## Repository scan report reference",
        f"Repository: {repo_full_name or ''}",
    ]
    if issue_number:
        parts.append(f"Reported Issue: #{issue_number}")
    if scan.summary:
        parts.extend(["", "### Scan summary", scan.summary])
    return "\n".join(parts).strip()


def build_agent_task_summary(
    task_summary: str, issue_context_markdown: str = ""
) -> str:
    """Return only the task-originator goal.

    ``issue_context_markdown`` is retained as a source-compatible argument for
    callers that have not yet migrated to the explicit ``reference_context``
    builder parameter.  It is intentionally ignored so third-party Issue/PR
    text, AI analysis, and comments can never be promoted into
    ``<task_originator_goal>`` by accident.
    """
    del issue_context_markdown
    summary = (task_summary or "").strip()
    # Historical manual-Issue tasks stored the reference projection directly
    # after the editable summary.  Strip that legacy suffix on read so resume
    # migration cannot promote comments/AI analysis into the new goal section.
    legacy_reference_marker = "## GitHub Issue 上下文"
    marker_index = summary.find(legacy_reference_marker)
    if marker_index >= 0:
        summary = summary[:marker_index].rstrip()
    return summary


def build_agent_submission_context_preview(
    *,
    task_title: str,
    task_summary: str,
    source_type: str = "",
    source_issue_number: int | None = None,
    sakura_memory: str = "",
    skills_summary: str = "",
    reference_context: str = "",
    role_memory_context: str = "",
    handoff_context: str = "",
    feedback: str = "",
    execution_expectations: str = "",
    system_prompt: str = IMPLEMENTATION_SYSTEM_PROMPT,
) -> str:
    """Build the role-separated preview used by the Agent."""
    user_message = build_implementation_user_message(
        task_title=task_title,
        task_summary=task_summary,
        source_type=source_type,
        source_issue_number=source_issue_number,
        sakura_memory=sakura_memory,
        skills_summary=skills_summary,
        reference_context=reference_context,
        role_memory_context=role_memory_context,
        handoff_context=handoff_context,
        feedback=feedback,
        execution_expectations=execution_expectations,
    )
    return f"## system\n{system_prompt.strip()}\n\n## user\n{user_message.strip()}"
