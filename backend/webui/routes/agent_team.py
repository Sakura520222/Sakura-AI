"""WebUI Agent 专家团队路由"""

import asyncio
import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.config import get_dynamic_config
from backend.core.time_service import format_rfc3339, now_utc
from backend.models.agent_team_models import (
    AgentTeamConversationContext,
    AgentTeamIteration,
    AgentTeamMessage,
    AgentTeamSession,
    AgentTeamSourceType,
    AgentTeamTask,
    AgentTeamTaskStatus,
    AgentTeamToolCall,
    AgentTeamUserPrompt,
)
from backend.models.database import IssueAnalysis
from backend.models.database import utc_now as _utc_now
from backend.services.agent_team.candidate_service import (
    DEFAULT_AGENT_TASK_GOAL,
    AgentTeamCandidateService,
    CandidateServiceError,
    candidates_to_dicts,
)
from backend.services.agent_team.prompt_config import build_implementation_user_message
from backend.services.agent_team.submission_context import (
    build_agent_submission_context_preview,
    build_agent_task_summary,
    build_issue_context_markdown,
    format_issue_analysis_context,
    json_list,
    load_issue_analysis_for_context,
    load_issue_comments_for_context,
    load_sakura_memory,
    load_skills_context,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.services.database_reset_runtime_service import (
    create_registered_background_task,
    register_current_background_task,
)
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    paginate,
    render_template,
    require_auth,
    require_csrf,
    require_super_admin,
)
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/agent-team", tags=["WebUI Agent Team"])

AGENT_TEAM_ACTIVE_STATUSES = [
    AgentTeamTaskStatus.QUEUED.value,
    AgentTeamTaskStatus.PLANNING.value,
    AgentTeamTaskStatus.CLONING.value,
    AgentTeamTaskStatus.EDITING.value,
    AgentTeamTaskStatus.SELF_REVIEWING.value,
    AgentTeamTaskStatus.VALIDATING.value,
    AgentTeamTaskStatus.PUSHING.value,
    AgentTeamTaskStatus.PR_OPENED.value,
    AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
    AgentTeamTaskStatus.ITERATING.value,
    AgentTeamTaskStatus.WAITING_HUMAN.value,
]

_VALID_TASK_PRIORITIES = {"critical", "high", "medium", "low"}

# 终态但具备 workspace/branch/PR 信息、可续跑的任务状态
_RESUMABLE_TERMINAL_STATUSES = {
    AgentTeamTaskStatus.COMPLETED.value,
    AgentTeamTaskStatus.WAITING_HUMAN.value,
    AgentTeamTaskStatus.PR_OPENED.value,
}


def _can_send_agent_prompt(task: AgentTeamTask) -> tuple[bool, str]:
    """判断 Live View 是否允许管理员指导输入。

    Returns:
        (can_send, disabled_reason) — can_send 为 True 时 disabled_reason 为空。
    """
    if task.status in AGENT_TEAM_ACTIVE_STATUSES:
        return True, ""
    if task.status in _RESUMABLE_TERMINAL_STATUSES and (
        task.workspace_path and task.branch_name and task.pr_number
    ):
        return True, ""
    if task.status in {
        AgentTeamTaskStatus.FAILED.value,
        AgentTeamTaskStatus.CANCELLED.value,
        AgentTeamTaskStatus.ABANDONED.value,
    }:
        return False, "task_terminal"
    if task.status in _RESUMABLE_TERMINAL_STATUSES:
        return False, "missing_workspace_or_pr"
    return False, "task_inactive"


def _message_guidance_ids(message_json: str | None) -> list[int]:
    """Read stable prompt IDs from a checkpointed guidance message."""
    try:
        payload = json.loads(message_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_ids = payload.get("guidance_ids")
    metadata = payload.get("metadata")
    if raw_ids is None and isinstance(metadata, dict):
        raw_ids = metadata.get("guidance_ids")
    if not isinstance(raw_ids, (list, tuple)):
        return []

    guidance_ids: list[int] = []
    for raw_id in raw_ids:
        try:
            guidance_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if guidance_id > 0 and guidance_id not in guidance_ids:
            guidance_ids.append(guidance_id)
    return guidance_ids


def _is_admin(user: dict) -> bool:
    return user.get("role") in ("admin", "super_admin")


def _build_task_owner_filter(user: dict):
    """非管理员用户只能看到自己创建的任务"""
    if _is_admin(user):
        return None
    return AgentTeamTask.started_by == user["sub"]


async def _check_and_consume_agent_quota(
    db: AsyncSession, user: dict
) -> tuple[bool, str]:
    """非管理员用户消费 Agent 配额，管理员跳过"""
    if _is_admin(user):
        return True, ""
    # 延迟导入避免循环引用
    from backend.services.telegram_service import TelegramService

    service = TelegramService(db)
    return await service.check_and_consume_agent_quota(
        github_username=user["sub"],
    )


async def _check_repo_access(user: dict, repo_full_name: str) -> str | None:
    """非管理员用户校验仓库访问权限，返回错误信息或 None 表示通过。

    校验规则：
      1. 仓库 owner 必须是用户自己的 GitHub 用户名
      2. 仓库必须在 Agent 允许列表中（allowlist 为空时不限制）
    """
    repo_owner = repo_full_name.split("/")[0] if "/" in repo_full_name else ""
    github_username = user.get("sub", "")
    if repo_owner != github_username:
        return "只能操作自己仓库的 Issue"
    raw = str(await get_dynamic_config("agent_team_repo_allowlist") or "")
    allowlist = {
        item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()
    }
    if allowlist and repo_full_name not in allowlist:
        return "该仓库不在允许列表中，无法创建任务"
    return None


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_optional_int(value: str | None, field_name: str) -> int | None:
    cleaned = _clean_optional_text(value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc


def _parse_task_overrides(
    *,
    title: str | None = None,
    summary: str | None = None,
    priority: str | None = None,
    candidate_score: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    source_issue_number: str | None = None,
    repo_full_name: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    status: str | None = None,
    branch_name: str | None = None,
    base_branch: str | None = None,
) -> dict:
    overrides = {}
    for key, value in {
        "title": title,
        "summary": summary,
        "source_type": source_type,
        "repo_full_name": repo_full_name,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch_name": branch_name,
        "base_branch": base_branch,
    }.items():
        cleaned = _clean_optional_text(value)
        if cleaned is not None:
            overrides[key] = cleaned

    cleaned_priority = _clean_optional_text(priority)
    if cleaned_priority is not None:
        if cleaned_priority not in _VALID_TASK_PRIORITIES:
            raise ValueError("priority 必须是 critical/high/medium/low")
        overrides["priority"] = cleaned_priority

    cleaned_status = _clean_optional_text(status)
    if cleaned_status is not None:
        valid_statuses = {item.value for item in AgentTeamTaskStatus}
        if cleaned_status not in valid_statuses:
            raise ValueError("status 不是有效的任务状态")
        overrides["status"] = cleaned_status

    score = _parse_optional_int(candidate_score, "candidate_score")
    if score is not None:
        if score < 0 or score > 100:
            raise ValueError("candidate_score 必须在 0-100 之间")
        overrides["candidate_score"] = score

    for key, value in {
        "source_id": source_id,
        "source_issue_number": source_issue_number,
    }.items():
        parsed = _parse_optional_int(value, key)
        if parsed is not None:
            overrides[key] = parsed

    full_name = overrides.get("repo_full_name")
    owner = overrides.get("repo_owner")
    name = overrides.get("repo_name")
    if full_name:
        if "/" not in full_name:
            raise ValueError("repo_full_name 必须是 owner/repo 格式")
        full_owner, full_repo = full_name.split("/", 1)
        if (owner and owner != full_owner) or (name and name != full_repo):
            raise ValueError("repo_full_name 必须和 repo_owner/repo_name 一致")
        overrides.setdefault("repo_owner", full_owner)
        overrides.setdefault("repo_name", full_repo)
    elif owner and name:
        overrides["repo_full_name"] = f"{owner}/{name}"

    return overrides


def _should_schedule_agent_task(status: str) -> bool:
    return status == AgentTeamTaskStatus.QUEUED.value


def _workspace_repo_task_condition(repo_owner: str, repo_name: str):
    """匹配占用指定物理仓库工作区的普通或 direct-PR 任务。"""
    repo_full_name = f"{repo_owner}/{repo_name}"
    return or_(
        and_(
            AgentTeamTask.repo_owner == repo_owner,
            AgentTeamTask.repo_name == repo_name,
        ),
        and_(
            AgentTeamTask.source_type == AgentTeamSourceType.PR_REVIEW.value,
            AgentTeamTask.pr_head_repo_full_name == repo_full_name,
        ),
    )


def _compact_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError, ValueError:
        return str(value)


def _format_context_items(value) -> list[dict]:
    items = []
    for item in json_list(value):
        if isinstance(item, dict):
            text = (
                item.get("title")
                or item.get("summary")
                or item.get("description")
                or item.get("issue")
                or item.get("path")
                or item.get("file_path")
                or _compact_json(item)
            )
            meta = item.get("severity") or item.get("status") or item.get("risk_level")
        else:
            text = str(item)
            meta = None
        if text:
            items.append({"text": text, "meta": meta})
    return items


def _format_agent_conversation_contexts(
    contexts: list[AgentTeamConversationContext],
) -> list[dict]:
    ordered = sorted(
        contexts,
        key=lambda item: (
            item.iteration_number or 0,
            format_rfc3339(item.created_at) if item.created_at else "",
            item.id or 0,
        ),
    )
    return [
        {
            "id": context.id,
            "iteration_number": context.iteration_number,
            "source_role": context.source_role,
            "target_role": context.target_role,
            "summary": context.summary,
            "unresolved_items": _format_context_items(context.unresolved_items_json),
            "modified_files": _format_context_items(context.modified_files_json),
            "token_estimate": context.token_estimate,
            "created_at": context.created_at,
        }
        for context in ordered
    ]


async def _load_task_issue_analysis(
    db: AsyncSession, task: AgentTeamTask
) -> IssueAnalysis | None:
    return await load_issue_analysis_for_context(
        db,
        source_type=task.source_type,
        source_id=task.source_id,
        repo_owner=task.repo_owner,
        repo_name=task.repo_name,
        repo_full_name=task.repo_full_name,
        issue_number=task.source_issue_number,
    )


async def _load_task_issue_comments(task: AgentTeamTask) -> list[dict]:
    return await load_issue_comments_for_context(
        repo_owner=task.repo_owner,
        repo_name=task.repo_name,
        issue_number=task.source_issue_number,
    )


async def _build_manual_issue_submission_context(db: AsyncSession, draft: dict) -> dict:
    analysis = await load_issue_analysis_for_context(
        db,
        source_type=draft.get("source_type"),
        source_id=draft.get("source_id"),
        repo_owner=draft.get("repo_owner"),
        repo_name=draft.get("repo_name"),
        repo_full_name=draft.get("repo_full_name"),
        issue_number=draft.get("source_issue_number"),
    )
    issue_analysis_context = format_issue_analysis_context(analysis)
    issue_comments = await load_issue_comments_for_context(
        repo_owner=draft.get("repo_owner"),
        repo_name=draft.get("repo_name"),
        issue_number=draft.get("source_issue_number"),
    )
    issue_context_markdown = build_issue_context_markdown(
        repo_full_name=draft.get("repo_full_name"),
        issue_number=draft.get("source_issue_number"),
        issue_analysis_context=issue_analysis_context,
        issue_comments=issue_comments,
        issue_body=draft.get("issue_body") or "",
    )
    # ``summary`` in a source draft is often Issue text or an AI-generated
    # analysis.  Only an explicit task goal may enter ``task_originator_goal``;
    # all source material stays in the reference section.
    draft_goal = draft.get("task_goal")
    if draft_goal is None:
        # Compatibility for callers constructing an old draft shape: the
        # editable summary is treated as the administrator's goal.
        draft_goal = draft.get("summary") or ""
    agent_task_context = build_agent_task_summary(draft_goal)
    reference_context = "\n\n".join(
        item
        for item in (
            issue_context_markdown,
            str(draft.get("reference_context") or "").strip(),
        )
        if item
    )
    sakura_memory = ""
    skills_summary = ""
    try:
        repo_owner = draft.get("repo_owner")
        repo_name = draft.get("repo_name")
        if repo_owner and repo_name:
            sakura_info = await load_sakura_memory(repo_owner, repo_name)
            sakura_memory = sakura_info.get("text") or ""
        skills_summary, _, _ = await load_skills_context()
        skills_summary = skills_summary or ""
    except Exception as exc:
        logger.warning("加载 Agent 提交预览运行时上下文失败: {}", exc)
    fullstack_user_message = build_implementation_user_message(
        task_title=draft.get("title") or "",
        task_summary=agent_task_context,
        source_type=draft.get("source_type") or "",
        source_issue_number=draft.get("source_issue_number"),
        sakura_memory=sakura_memory,
        skills_summary=skills_summary,
        reference_context=reference_context,
    )
    return {
        "issue_analysis": issue_analysis_context,
        "issue_comments": issue_comments,
        "issue_context_markdown": issue_context_markdown,
        "agent_task_context": agent_task_context,
        "reference_context": reference_context,
        "fullstack_user_message": fullstack_user_message,
        "full_submission_preview": build_agent_submission_context_preview(
            task_title=draft.get("title") or "",
            task_summary=agent_task_context,
            source_type=draft.get("source_type") or "",
            source_issue_number=draft.get("source_issue_number"),
            sakura_memory=sakura_memory,
            skills_summary=skills_summary,
            reference_context=reference_context,
        ),
        "runtime_context": {
            "sakura_memory": sakura_memory,
            "skills_summary": skills_summary,
        },
    }


@router.get("/")
async def agent_team_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Agent 专家团队独立页面。"""
    is_admin = _is_admin(user)
    stats = await _load_stats(db, user=user)
    agent_quota = None
    if not is_admin:
        # 延迟导入避免循环引用
        from backend.services.telegram_service import TelegramService

        quota_info = await TelegramService(db).get_user_quota_info(user["sub"])
        if quota_info:
            agent_quota = {
                "daily_used": quota_info["agent_daily"]["used"],
                "daily_limit": quota_info["agent_daily"]["limit"],
                "weekly_used": quota_info["agent_weekly"]["used"],
                "weekly_limit": quota_info["agent_weekly"]["limit"],
                "monthly_used": quota_info["agent_monthly"]["used"],
                "monthly_limit": quota_info["agent_monthly"]["limit"],
            }
    return render_template(
        "agent_team.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="agent_team",
        stats=stats,
        workspace_summary=_load_workspace_summary(),
        status_options=[status.value for status in AgentTeamTaskStatus],
        is_admin=is_admin,
        agent_quota=agent_quota,
    )


@router.get("/list-fragment")
async def task_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    page: int = 1,
    per_page: int | None = None,
    status: str | None = None,
    source_type: str | None = None,
    q: str | None = None,
    sort: str = "newest",
):
    """任务列表片段。"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]
    filters = []
    owner_filter = _build_task_owner_filter(user)
    if owner_filter is not None:
        filters.append(owner_filter)
    if status and status != "all":
        if status == "active":
            filters.append(AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES))
        else:
            filters.append(AgentTeamTask.status == status)
    if source_type and source_type != "all":
        filters.append(AgentTeamTask.source_type == source_type)
    if q:
        keyword = f"%{q.strip()}%"
        filters.append(
            or_(
                AgentTeamTask.title.ilike(keyword),
                AgentTeamTask.repo_full_name.ilike(keyword),
                AgentTeamTask.summary.ilike(keyword),
                AgentTeamTask.branch_name.ilike(keyword),
            )
        )

    order_by = desc(AgentTeamTask.created_at)
    if sort == "score":
        order_by = desc(AgentTeamTask.candidate_score)
    elif sort == "updated":
        order_by = desc(AgentTeamTask.updated_at)

    query = select(AgentTeamTask).where(*filters).order_by(order_by)
    count_query = select(func.count(AgentTeamTask.id)).where(*filters)
    tasks, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )
    return render_template(
        "components/agent_team_task_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        tasks=tasks,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        status=status or "all",
        source_type=source_type or "all",
        q=q or "",
        sort=sort,
    )


@router.get("/workspaces-fragment")
async def workspace_list_fragment(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """工作区列表片段。"""
    service = AgentTeamWorkspaceService()
    workspaces = [_workspace_info_to_dict(info) for info in service.list_workspaces()]
    return render_template(
        "components/agent_team_workspace_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        workspace_root=str(service.base_dir),
        workspaces=workspaces,
        total_size=sum(item["total_size_bytes"] for item in workspaces),
        total_size_label=_format_bytes(
            sum(item["total_size_bytes"] for item in workspaces)
        ),
    )


@router.get("/tasks/{task_id}/detail-fragment")
async def task_detail_fragment(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """任务详情片段。"""
    result = await db.execute(
        select(AgentTeamTask)
        .where(AgentTeamTask.id == task_id)
        .options(
            selectinload(AgentTeamTask.iterations).selectinload(
                AgentTeamIteration.patch_files
            ),
            selectinload(AgentTeamTask.feedback),
            selectinload(AgentTeamTask.conversation_contexts),
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse(
            {"success": False, "message": "无权查看此任务"}, status_code=403
        )

    # selectinload 不支持在当前 SQLAlchemy 版本中稳定地继续链式排序，模板侧按序展示即可。
    task.iterations.sort(key=lambda item: item.iteration_number)
    task.feedback.sort(key=lambda item: item.created_at, reverse=True)
    for iteration in task.iterations:
        iteration.patch_files.sort(key=lambda item: item.file_path)

    issue_analysis = await _load_task_issue_analysis(db, task)
    issue_comments = await _load_task_issue_comments(task)
    agent_contexts = _format_agent_conversation_contexts(task.conversation_contexts)

    return render_template(
        "components/agent_team_task_detail_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        task=task,
        issue_analysis_context=format_issue_analysis_context(issue_analysis),
        issue_comments=issue_comments,
        agent_contexts=agent_contexts,
    )


@router.get("/api/repos/{owner}/{name}/branches")
async def list_repo_branches(
    owner: str,
    name: str,
    user: dict = Depends(require_auth),
):
    """获取仓库分支列表，供任务创建弹窗选择基础分支。"""
    if not _is_admin(user):
        err = await _check_repo_access(user, f"{owner}/{name}")
        if err:
            return JSONResponse({"success": False, "message": err}, status_code=403)
    try:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(owner, name)
        if not client:
            return JSONResponse(
                {
                    "success": False,
                    "message": f"无法获取 GitHub 客户端: {owner}/{name}",
                },
                status_code=200,
            )
        repo = client.get_repo(f"{owner}/{name}")
        default_branch = repo.default_branch or "main"
        branches = []
        for branch in repo.get_branches()[:100]:
            branches.append(branch.name)
        return JSONResponse(
            {
                "success": True,
                "default_branch": default_branch,
                "branches": branches,
            }
        )
    except Exception:
        logger.exception("获取仓库分支列表失败")
        return JSONResponse(
            {"success": False, "message": "获取分支列表失败，请稍后重试"},
            status_code=200,
        )


@router.post("/candidates")
async def preview_candidates(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    ai_filter_requirement: str = Form(""),
):
    """手动预览候选任务。"""
    service = AgentTeamCandidateService()
    try:
        candidates = await service.collect_candidates(
            db, ai_filter_requirement=ai_filter_requirement
        )
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=200)
    except Exception:
        logger.exception("AI 筛选候选失败")
        return JSONResponse(
            {"success": False, "message": "AI 筛选候选失败，请稍后重试"},
            status_code=200,
        )
    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_preview_candidates",
        "agent_team",
        None,
        {
            "count": len(candidates),
            "ai_filter": bool(ai_filter_requirement.strip()),
            "requirement": ai_filter_requirement.strip()[:300],
        },
    )
    return JSONResponse(
        {"success": True, "candidates": candidates_to_dicts(candidates)}
    )


@router.post("/tasks/create")
async def create_task_from_candidate(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    source_type: str = Form(...),
    source_id: int = Form(...),
    ai_filter_requirement: str = Form(""),
    title: str = Form(""),
    summary: str = Form(""),
    priority: str = Form(""),
    candidate_score: str = Form(""),
    edited_source_type: str = Form(""),
    edited_source_id: str = Form(""),
    source_issue_number: str = Form(""),
    repo_full_name: str = Form(""),
    repo_owner: str = Form(""),
    repo_name: str = Form(""),
    status: str = Form(""),
    branch_name: str = Form(""),
    base_branch: str = Form(""),
):
    """从候选来源创建 Agent 任务。"""
    service = AgentTeamCandidateService()
    candidates = await service.collect_candidates(
        db, limit=100, ai_filter_requirement=ai_filter_requirement
    )
    candidate = next(
        (
            item
            for item in candidates
            if item.source_type == source_type and item.source_id == source_id
        ),
        None,
    )
    if candidate is None:
        return JSONResponse(
            {"success": False, "message": "候选任务不存在或已被处理"}, status_code=404
        )

    try:
        overrides = _parse_task_overrides(
            title=title,
            summary=summary,
            priority=priority,
            candidate_score=candidate_score,
            source_type=edited_source_type,
            source_id=edited_source_id,
            source_issue_number=source_issue_number,
            repo_full_name=repo_full_name,
            repo_owner=repo_owner,
            repo_name=repo_name,
            status=status,
            branch_name=branch_name,
            base_branch=base_branch,
        )
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)

    task = await service.create_task_from_candidate(
        db,
        candidate,
        started_by=user["sub"],
        base_branch=base_branch.strip() or None,
        overrides=overrides,
    )
    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_create",
        "agent_team_task",
        str(task.id),
        {
            "source_type": task.source_type,
            "source_id": task.source_id,
            "repo_full_name": task.repo_full_name,
            "source_issue_number": task.source_issue_number,
            "status": task.status,
            "base_branch": task.base_branch,
            "branch_name": task.branch_name,
        },
    )

    if _should_schedule_agent_task(task.status):
        background_tasks.add_task(_run_agent_task_background, task.id)

    return JSONResponse({"success": True, "task_id": task.id})


def _parse_issue_ref(ref: str) -> tuple[str, int]:
    """Parse issue reference into (repo_full_name, issue_number).

    Supported formats:
      - https://github.com/owner/repo/issues/123
      - http://github.com/owner/repo/issues/123
      - github.com/owner/repo/issues/123
      - owner/repo#123
      - owner/repo 123
    """
    ref = ref.strip()

    # GitHub Issue URL
    m = re.match(r"(?:https?://)?github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if m:
        return m.group(1), int(m.group(2))

    # owner/repo#123
    m = re.match(r"^([^/]+/[^/#]+)#(\d+)$", ref)
    if m:
        return m.group(1).strip(), int(m.group(2))

    # owner/repo 123
    m = re.match(r"^([^/]+/[^/\s]+)\s+(\d+)$", ref)
    if m:
        return m.group(1).strip(), int(m.group(2))

    raise ValueError(
        "无法解析 Issue 引用，请使用以下格式之一：\n"
        "• https://github.com/owner/repo/issues/123\n"
        "• owner/repo#123\n"
        "• owner/repo 123"
    )


@router.post("/tasks/preview-from-issue")
async def preview_task_from_issue(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    issue_ref: str = Form(...),
    draft_json: str = Form(""),
):
    """从指定 Issue 构建可编辑任务草稿和生产提交预览。

    The client may send only the small set of editable fields from the draft.
    Repository/source identity and all reference material are always rebuilt
    from the server-side Issue draft, so the browser cannot approximate or
    replace the production submission context.
    """
    try:
        repo_full_name, issue_number = _parse_issue_ref(issue_ref)
    except ValueError as e:
        logger.warning("Invalid issue ref in preview-from-issue: {}", e)
        return JSONResponse(
            {"success": False, "message": "Invalid issue reference format"},
            status_code=200,
        )
    if not _is_admin(user):
        err = await _check_repo_access(user, repo_full_name)
        if err:
            return JSONResponse(
                {"success": False, "message": err},
                status_code=200,
            )
    try:
        draft = await AgentTeamCandidateService().build_manual_issue_task_draft(
            db, repo_full_name, issue_number
        )
        if isinstance(draft_json, str) and draft_json.strip():
            try:
                edited_draft = json.loads(draft_json)
            except TypeError, ValueError:
                return JSONResponse(
                    {"success": False, "message": "Invalid preview draft"},
                    status_code=200,
                )
            if not isinstance(edited_draft, dict):
                return JSONResponse(
                    {"success": False, "message": "Invalid preview draft"},
                    status_code=200,
                )
            for field in (
                "title",
                "summary",
                "priority",
                "base_branch",
                "branch_name",
                "status",
                "candidate_score",
            ):
                if field in edited_draft:
                    draft[field] = edited_draft[field]
        context_draft = dict(draft)
        context_draft["task_goal"] = (
            str(draft.get("summary") or "").strip() or DEFAULT_AGENT_TASK_GOAL
        )
        submission_context = await _build_manual_issue_submission_context(
            db, context_draft
        )
    except CandidateServiceError:
        return JSONResponse(
            {"success": False, "message": "GitHub API 调用失败，请稍后重试"},
            status_code=200,
        )
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )
    return JSONResponse(
        jsonable_encoder(
            {
                "success": True,
                "draft": draft,
                "submission_context": submission_context,
                "preview_source": "server_production_builder",
            }
        )
    )


@router.post("/tasks/create-from-issue")
async def create_task_from_issue(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    issue_ref: str = Form(...),
    title: str = Form(""),
    summary: str = Form(""),
    priority: str = Form(""),
    candidate_score: str = Form(""),
    source_type: str = Form(""),
    source_id: str = Form(""),
    source_issue_number: str = Form(""),
    repo_full_name: str = Form(""),
    repo_owner: str = Form(""),
    repo_name: str = Form(""),
    status: str = Form(""),
    branch_name: str = Form(""),
    base_branch: str = Form(""),
):
    """从指定仓库的 Issue 直接创建 Agent 任务。"""
    try:
        repo_full_name, issue_number = _parse_issue_ref(issue_ref)
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )
    if not _is_admin(user):
        err = await _check_repo_access(user, repo_full_name)
        if err:
            return JSONResponse(
                {"success": False, "message": err},
                status_code=200,
            )

    # Agent 配额消费（仓库权限校验通过后再扣费）
    ok, msg = await _check_and_consume_agent_quota(db, user)
    if not ok:
        return JSONResponse({"success": False, "message": msg}, status_code=200)

    try:
        overrides = _parse_task_overrides(
            title=title,
            summary=summary,
            priority=priority,
            candidate_score=candidate_score,
            source_type=source_type,
            source_id=source_id,
            source_issue_number=source_issue_number,
            repo_full_name=repo_full_name,
            repo_owner=repo_owner,
            repo_name=repo_name,
            status=status,
            branch_name=branch_name,
            base_branch=base_branch,
        )
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)

    draft_for_context = {
        "source_type": overrides.get("source_type")
        or AgentTeamSourceType.MANUAL_ISSUE.value,
        "source_id": overrides.get("source_id"),
        "source_issue_number": overrides.get("source_issue_number") or issue_number,
        "repo_full_name": overrides.get("repo_full_name") or repo_full_name,
        "repo_owner": overrides.get("repo_owner"),
        "repo_name": overrides.get("repo_name"),
        "title": overrides.get("title") or title or "",
        "summary": overrides.get("summary") or summary or "",
        "task_goal": overrides.get("summary") or summary or DEFAULT_AGENT_TASK_GOAL,
        "issue_body": "",
    }
    if not draft_for_context["repo_owner"] or not draft_for_context["repo_name"]:
        full_name = draft_for_context["repo_full_name"] or repo_full_name
        parts = full_name.split("/", 1) if "/" in full_name else []
        if len(parts) == 2:
            draft_for_context["repo_owner"] = (
                draft_for_context["repo_owner"] or parts[0]
            )
            draft_for_context["repo_name"] = draft_for_context["repo_name"] or parts[1]
    submission_context = await _build_manual_issue_submission_context(
        db, draft_for_context
    )
    if submission_context["agent_task_context"]:
        overrides["summary"] = submission_context["agent_task_context"]

    service = AgentTeamCandidateService()
    try:
        task = await service.create_task_from_manual_issue(
            db,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            started_by=user["sub"],
            base_branch=base_branch.strip() or None,
            overrides=overrides,
        )
    except CandidateServiceError:
        return JSONResponse(
            {"success": False, "message": "GitHub API 调用失败，请稍后重试"},
            status_code=200,
        )
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_create_from_issue",
        "agent_team_task",
        str(task.id),
        {
            "source_type": task.source_type,
            "source_id": task.source_id,
            "repo_full_name": task.repo_full_name,
            "source_issue_number": task.source_issue_number,
            "status": task.status,
            "base_branch": task.base_branch,
            "branch_name": task.branch_name,
        },
    )

    if _should_schedule_agent_task(task.status):
        background_tasks.add_task(_run_agent_task_background, task.id)

    return JSONResponse({"success": True, "task_id": task.id})


@router.post("/tasks/{task_id}/retry")
async def retry_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """重试失败或卡住的任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse(
            {"success": False, "message": "无权操作此任务"}, status_code=403
        )

    retryable_statuses = {"failed", "cancelled", "abandoned", "queued"}
    if task.status not in retryable_statuses:
        return JSONResponse(
            {
                "success": False,
                "message": f"当前状态 {task.status} 不可重试，仅支持 {'/'.join(sorted(retryable_statuses))}",
            },
            status_code=200,
        )

    # Agent 配额消费（确认任务可重试后再扣费）
    ok, msg = await _check_and_consume_agent_quota(db, user)
    if not ok:
        return JSONResponse({"success": False, "message": msg}, status_code=200)

    old_status = task.status
    task.status = AgentTeamTaskStatus.QUEUED.value
    task.current_phase = None
    task.started_at = None
    task.completed_at = None
    task.error_message = None
    await db.commit()

    # 提交给后台 worker 执行，避免阻塞 HTTP 请求导致前端/反代超时
    background_tasks.add_task(_run_agent_task_background, task_id)

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_retry",
        "agent_team_task",
        str(task_id),
        {"old_status": old_status},
    )
    return JSONResponse({"success": True, "task_id": task_id})


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """从已持久化 messages 和工作区继续运行任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse(
            {"success": False, "message": "无权操作此任务"}, status_code=403
        )

    if task.status not in {"failed", "cancelled"}:
        return JSONResponse(
            {"success": False, "message": f"当前状态 {task.status} 不可续跑"},
            status_code=200,
        )
    if not task.workspace_path or not task.branch_name:
        return JSONResponse(
            {"success": False, "message": "任务缺少续跑工作区或分支信息"},
            status_code=200,
        )

    from backend.services.agent_team.conversation_checkpoint import (
        ConversationCheckpointService,
    )

    checkpoint = ConversationCheckpointService(task_id)
    if not await checkpoint.has_resume_state():
        return JSONResponse(
            {"success": False, "message": "任务没有可续跑的 messages checkpoint"},
            status_code=200,
        )

    old_status = task.status
    task.status = AgentTeamTaskStatus.QUEUED.value
    task.current_phase = "resuming"
    task.resume_count = (task.resume_count or 0) + 1
    task.completed_at = None
    task.error_message = None
    await db.commit()

    background_tasks.add_task(_resume_agent_task_background, task_id)

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_resume",
        "agent_team_task",
        str(task_id),
        {"old_status": old_status, "resume_count": task.resume_count},
    )
    return JSONResponse({"success": True, "task_id": task_id})


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """取消任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse(
            {"success": False, "message": "无权操作此任务"}, status_code=403
        )

    cancellable = {
        "queued",
        "planning",
        "cloning",
        "editing",
        "self_reviewing",
        "validating",
        "iterating",
        "pr_opened",
        "external_reviewing",
        "waiting_human",
    }
    if task.status not in cancellable:
        return JSONResponse(
            {"success": False, "message": f"当前状态 {task.status} 不可取消"},
            status_code=200,
        )

    old_status = task.status
    task.status = AgentTeamTaskStatus.CANCELLED.value
    task.completed_at = now_utc()
    await db.commit()

    # 向正在运行的 worker 发送取消信号
    from backend.workers.agent_team_worker import request_task_cancel

    request_task_cancel(task_id)

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_cancel",
        "agent_team_task",
        str(task_id),
        {"old_status": old_status},
    )
    return JSONResponse({"success": True, "task_id": task_id})


@router.post("/tasks/{task_id}/delete")
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """删除任务及其迭代/反馈/变更文件记录。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse(
            {"success": False, "message": "无权操作此任务"}, status_code=403
        )

    protected_statuses = set(AGENT_TEAM_ACTIVE_STATUSES) - {
        AgentTeamTaskStatus.PR_OPENED.value,
        AgentTeamTaskStatus.WAITING_HUMAN.value,
    }
    if task.status in protected_statuses:
        return JSONResponse(
            {
                "success": False,
                "message": f"当前状态 {task.status} 仍在运行，请先取消或等待完成后再删除",
            },
            status_code=200,
        )

    log_payload = {
        "task_id": task.id,
        "title": task.title,
        "repo_full_name": task.repo_full_name,
        "source_type": task.source_type,
        "source_id": task.source_id,
        "status": task.status,
    }
    await db.delete(task)
    await db.commit()

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_delete",
        "agent_team_task",
        str(task_id),
        log_payload,
    )
    return JSONResponse({"success": True, "task_id": task_id})


@router.post("/workspaces/delete")
async def delete_workspace(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    repo_owner: str = Form(...),
    repo_name: str = Form(...),
):
    """删除 Agent 仓库工作区目录。"""
    active_count = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            _workspace_repo_task_condition(repo_owner, repo_name),
            AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES),
        )
    )
    if active_count:
        return JSONResponse(
            {
                "success": False,
                "message": "该仓库存在进行中的 Agent 任务，请先取消或等待完成后再删除工作区",
            },
            status_code=200,
        )

    service = AgentTeamWorkspaceService()
    try:
        workspace = service.delete_workspace(repo_owner, repo_name)
    except ValueError:
        logger.exception("删除 Agent 仓库工作区失败：路径校验未通过")
        return JSONResponse(
            {"success": False, "message": "工作区路径无效"}, status_code=400
        )

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_workspace_delete",
        "agent_team_workspace",
        f"{repo_owner}/{repo_name}",
        {"repo_owner": repo_owner, "repo_name": repo_name, "path": str(workspace)},
    )
    return JSONResponse(
        {"success": True, "repo_owner": repo_owner, "repo_name": repo_name}
    )


# ── Worktree 管理 ──────────────────────────────────────────


@router.get("/workspaces/{repo_owner}/{repo_name}/worktrees-fragment")
async def worktree_list_fragment(
    request: Request,
    repo_owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """返回指定仓库的 worktree 列表 HTML 片段。"""
    service = AgentTeamWorkspaceService()
    worktrees = await asyncio.to_thread(service.list_worktrees, repo_owner, repo_name)

    # 查询各 task 的状态，用于标识活跃/孤立 worktree
    task_ids = [w.task_id for w in worktrees if w.task_id is not None]
    task_status_map: dict[int, str] = {}
    if task_ids:
        rows = (
            await db.execute(
                select(AgentTeamTask.id, AgentTeamTask.status).where(
                    AgentTeamTask.id.in_(task_ids)
                )
            )
        ).all()
        task_status_map = {row.id: row.status for row in rows}

    active_statuses = set(AGENT_TEAM_ACTIVE_STATUSES)

    worktree_items = []
    for w in worktrees:
        task_status = task_status_map.get(w.task_id, "") if w.task_id else ""
        is_active = task_status in active_statuses
        worktree_items.append(
            {
                "dir_name": w.dir_name,
                "task_id": w.task_id,
                "branch_slug": w.branch_slug,
                "file_count": w.file_count,
                "total_size_bytes": w.total_size_bytes,
                "size_label": _format_bytes(w.total_size_bytes),
                "modified_at": datetime.fromtimestamp(w.modified_at, tz=UTC)
                if w.modified_at
                else None,
                "task_status": task_status,
                "is_active": is_active,
            }
        )

    orphans = [w for w in worktree_items if not w["is_active"]]
    has_orphans = len(orphans) > 0

    return render_template(
        "components/agent_team_worktree_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        repo_owner=repo_owner,
        repo_name=repo_name,
        worktrees=worktree_items,
        has_orphans=has_orphans,
        orphan_count=len(orphans),
    )


@router.post("/workspaces/worktrees/delete")
async def delete_worktree(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    repo_owner: str = Form(...),
    repo_name: str = Form(...),
    dir_name: str = Form(...),
):
    """删除单个 worktree 目录。"""
    service = AgentTeamWorkspaceService()

    # 目录名格式为 {task_id}-{branch_slug}，由 prepare_workspace 创建。
    # 从中提取 task_id 检查是否有活跃任务；若目录为手动创建则跳过此检查。
    wt_match = service._WT_DIR_RE.match(dir_name)
    if wt_match:
        task_id = int(wt_match.group(1))
        active_count = await db.scalar(
            select(func.count(AgentTeamTask.id)).where(
                AgentTeamTask.id == task_id,
                AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES),
            )
        )
        if active_count:
            return JSONResponse(
                {
                    "success": False,
                    "message": f"任务 #{task_id} 仍在运行中，无法删除其 worktree",
                },
                status_code=200,
            )

    try:
        deleted = await asyncio.to_thread(
            service.delete_worktree, repo_owner, repo_name, dir_name
        )
    except ValueError:
        logger.exception("删除 Agent worktree 失败：路径校验未通过")
        return JSONResponse(
            {"success": False, "message": "工作区路径无效"}, status_code=400
        )

    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_worktree_delete",
        "agent_team_worktree",
        f"{repo_owner}/{repo_name}/{dir_name}",
        {
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "dir_name": dir_name,
            "path": str(deleted),
        },
    )
    return JSONResponse({"success": True, "dir_name": dir_name})


@router.post("/workspaces/worktrees/clean-orphans")
async def clean_orphan_worktrees(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    repo_owner: str = Form(...),
    repo_name: str = Form(...),
):
    """清理指定仓库的孤立 worktree（对应任务已终结）。"""
    service = AgentTeamWorkspaceService()
    worktrees = await asyncio.to_thread(service.list_worktrees, repo_owner, repo_name)
    if not worktrees:
        return JSONResponse({"success": True, "cleaned": 0})

    task_ids = [w.task_id for w in worktrees if w.task_id is not None]
    task_status_map: dict[int, str] = {}
    if task_ids:
        rows = (
            await db.execute(
                select(AgentTeamTask.id, AgentTeamTask.status).where(
                    AgentTeamTask.id.in_(task_ids)
                )
            )
        ).all()
        task_status_map = {row.id: row.status for row in rows}

    active_statuses = set(AGENT_TEAM_ACTIVE_STATUSES)
    cleaned = 0
    for w in worktrees:
        task_status = task_status_map.get(w.task_id, "") if w.task_id else ""
        if task_status in active_statuses:
            continue
        try:
            await asyncio.to_thread(
                service.delete_worktree, repo_owner, repo_name, w.dir_name
            )
            cleaned += 1
        except Exception as exc:
            logger.warning("清理孤立 worktree 失败: {} - {}", w.dir_name, exc)

    if cleaned:
        await log_admin_action(
            db,
            user["user_id"],
            "agent_team_worktree_clean_orphans",
            "agent_team_worktree",
            f"{repo_owner}/{repo_name}",
            {"repo_owner": repo_owner, "repo_name": repo_name, "cleaned": cleaned},
        )

    return JSONResponse({"success": True, "cleaned": cleaned})


async def _run_agent_task_background(task_id: int) -> None:
    """后台执行 Agent 任务，避免阻塞 WebUI 请求。"""
    if register_current_background_task("agent_team_webui") is None:
        return
    try:
        from backend.workers.agent_team_worker import submit_agent_team_task

        await submit_agent_team_task(task_id)
    except Exception as exc:
        logger.error(
            "Agent 后台任务提交失败: task_id={}, error={}", task_id, exc, exc_info=True
        )


async def _resume_agent_task_background(task_id: int) -> None:
    """后台续跑 Agent 任务，避免阻塞 WebUI 请求。"""
    if register_current_background_task("agent_team_webui_resume") is None:
        return
    try:
        from backend.workers.agent_team_worker import resume_agent_team_task

        await resume_agent_team_task(task_id)
    except Exception as exc:
        logger.error(
            "Agent 后台任务续跑失败: task_id={}, error={}", task_id, exc, exc_info=True
        )


async def _load_stats(db: AsyncSession, user: dict | None = None) -> dict:
    owner_filter = _build_task_owner_filter(user) if user else None
    base = [owner_filter] if owner_filter else []
    total = await db.scalar(select(func.count(AgentTeamTask.id)).where(*base))
    active = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES), *base
        )
    )
    completed = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status == "completed", *base
        )
    )
    failed = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status == "failed", *base
        )
    )
    queued = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status == "queued", *base
        )
    )
    waiting_human = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status == "waiting_human", *base
        )
    )
    status_rows = await db.execute(
        select(AgentTeamTask.status, func.count(AgentTeamTask.id))
        .where(*base)
        .group_by(AgentTeamTask.status)
    )
    return {
        "total": total or 0,
        "active": active or 0,
        "completed": completed or 0,
        "failed": failed or 0,
        "queued": queued or 0,
        "waiting_human": waiting_human or 0,
        "status_counts": {status: count for status, count in status_rows.all()},
    }


def _load_workspace_summary() -> dict:
    """读取工作区摘要，用于页面顶部快捷展示。"""
    service = AgentTeamWorkspaceService()
    workspaces = service.list_workspaces()
    return {
        "root": str(service.base_dir),
        "count": len(workspaces),
        "total_size_bytes": sum(item.total_size_bytes for item in workspaces),
        "total_size_label": _format_bytes(
            sum(item.total_size_bytes for item in workspaces)
        ),
    }


def _workspace_info_to_dict(info) -> dict:
    """将工作区信息转换为模板友好结构。"""
    return {
        "repo_owner": info.repo_owner,
        "repo_name": info.repo_name,
        "repo_full_name": f"{info.repo_owner}/{info.repo_name}",
        "path": str(info.path),
        "exists": info.exists,
        "file_count": info.file_count,
        "total_size_bytes": info.total_size_bytes,
        "size_label": _format_bytes(info.total_size_bytes),
        "modified_at": datetime.fromtimestamp(info.modified_at, tz=UTC)
        if info.modified_at
        else None,
        "has_git": info.has_git,
        "worktree_count": info.worktree_count,
    }


def _format_bytes(value: int) -> str:
    """格式化字节数。"""
    size = float(value or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


# ── Live View API 端点 ──────────────────────────────────


@router.get("/api/active-tasks")
async def list_active_tasks(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取活跃任务列表（供 Live View 下拉框使用，含最近完成的任务以便回看对话）。"""
    owner_filter = _build_task_owner_filter(user)
    base_filters = [
        AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES)
        | (
            AgentTeamTask.status.in_(
                [
                    AgentTeamTaskStatus.COMPLETED.value,
                    AgentTeamTaskStatus.FAILED.value,
                    AgentTeamTaskStatus.CANCELLED.value,
                    AgentTeamTaskStatus.PR_OPENED.value,
                ]
            )
            & AgentTeamTask.completed_at.isnot(None)
        )
    ]
    if owner_filter is not None:
        base_filters.append(owner_filter)
    rows = (
        (
            await db.execute(
                select(AgentTeamTask)
                .where(*base_filters)
                .order_by(desc(AgentTeamTask.updated_at))
                .limit(30)
            )
        )
        .scalars()
        .all()
    )

    return JSONResponse(
        {
            "success": True,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "repo_full_name": t.repo_full_name,
                    "current_phase": t.current_phase,
                    "branch_name": t.branch_name,
                    "base_branch": t.base_branch,
                    "pr_number": t.pr_number,
                    "pr_url": t.pr_url,
                    "iteration_count": t.iteration_count,
                    "updated_at": format_rfc3339(t.updated_at)
                    if t.updated_at
                    else None,
                    "completed_at": format_rfc3339(t.completed_at)
                    if t.completed_at
                    else None,
                }
                for t in rows
            ],
        }
    )


@router.get("/api/tasks/{task_id}/stream-data")
async def task_stream_data(
    task_id: int,
    after_id: int = 0,
    limit: int = 50,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取任务的消息流数据（messages + tool_calls + sessions + prompts）。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse(
            {"success": False, "error": "Task not found"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)

    # Sessions for this task
    session_rows = (
        (
            await db.execute(
                select(AgentTeamSession)
                .where(AgentTeamSession.task_id == task_id)
                .order_by(AgentTeamSession.id)
            )
        )
        .scalars()
        .all()
    )
    session_ids = [s.id for s in session_rows]

    if not session_ids:
        can_send, disabled_reason = _can_send_agent_prompt(task)
        return JSONResponse(
            {
                "success": True,
                "messages": [],
                "tool_calls": [],
                "sessions": [],
                "prompts": [],
                "has_more": False,
                "task_status": task.status,
                "can_send_prompt": can_send,
                "prompt_disabled_reason": disabled_reason,
            }
        )

    # Messages with pagination (use global id, not per-session seq)
    msg_query = (
        select(AgentTeamMessage)
        .where(
            AgentTeamMessage.session_id.in_(session_ids),
            AgentTeamMessage.id > after_id,
        )
        .order_by(AgentTeamMessage.id)
        .limit(limit + 1)
    )
    msg_rows = (await db.execute(msg_query)).scalars().all()
    has_more = len(msg_rows) > limit
    msg_rows = msg_rows[:limit]

    # Tool calls can change status without producing a new message.
    tool_call_rows = (
        (
            await db.execute(
                select(AgentTeamToolCall)
                .where(AgentTeamToolCall.session_id.in_(session_ids))
                .order_by(AgentTeamToolCall.id)
            )
        )
        .scalars()
        .all()
    )

    # User prompts
    prompt_rows = (
        (
            await db.execute(
                select(AgentTeamUserPrompt)
                .where(AgentTeamUserPrompt.task_id == task_id)
                .order_by(AgentTeamUserPrompt.created_at)
            )
        )
        .scalars()
        .all()
    )

    can_send, disabled_reason = _can_send_agent_prompt(task)
    return JSONResponse(
        {
            "success": True,
            "messages": [
                {
                    "id": m.id,
                    "session_id": m.session_id,
                    "seq": m.seq,
                    "role": m.role,
                    "content": m.content,
                    "guidance_ids": _message_guidance_ids(m.message_json),
                    "tool_call_id": m.tool_call_id,
                    "finish_reason": m.finish_reason,
                    "created_at": format_rfc3339(m.created_at)
                    if m.created_at
                    else None,
                }
                for m in msg_rows
            ],
            "tool_calls": [
                {
                    "id": tc.id,
                    "session_id": tc.session_id,
                    "assistant_message_id": tc.assistant_message_id,
                    "tool_call_id": tc.tool_call_id,
                    "name": tc.name,
                    "status": tc.status,
                    "arguments_json": tc.arguments_json,
                    "started_at": format_rfc3339(tc.started_at)
                    if tc.started_at
                    else None,
                    "completed_at": format_rfc3339(tc.completed_at)
                    if tc.completed_at
                    else None,
                    "error_message": tc.error_message,
                }
                for tc in tool_call_rows
            ],
            "sessions": [
                {
                    "id": s.id,
                    "iteration_number": s.iteration_number,
                    "role_name": s.role_name,
                    "status": s.status,
                    "started_at": format_rfc3339(s.started_at)
                    if s.started_at
                    else None,
                    "completed_at": format_rfc3339(s.completed_at)
                    if s.completed_at
                    else None,
                }
                for s in session_rows
            ],
            "prompts": [
                {
                    "id": p.id,
                    "content": p.content,
                    "status": p.status,
                    "submitted_by": p.submitted_by,
                    "created_at": format_rfc3339(p.created_at)
                    if p.created_at
                    else None,
                    "consumed_at": format_rfc3339(p.consumed_at)
                    if p.consumed_at
                    else None,
                }
                for p in prompt_rows
            ],
            "has_more": has_more,
            "task_status": task.status,
            "can_send_prompt": can_send,
            "prompt_disabled_reason": disabled_reason,
        }
    )


@router.post("/api/tasks/{task_id}/prompts")
async def submit_user_prompt(
    task_id: int,
    request: Request,
    content: str = Form(...),
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """提交用户引导 Prompt（活跃任务注入 / 可续跑终态触发 follow-up）。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse(
            {"success": False, "error": "Task not found"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)

    can_send, disabled_reason = _can_send_agent_prompt(task)
    if not can_send:
        return JSONResponse(
            {"success": False, "error": f"Cannot send prompt: {disabled_reason}"},
            status_code=400,
        )

    content = content.strip()
    if not content:
        return JSONResponse(
            {"success": False, "error": "Content is empty"}, status_code=400
        )

    username = ""
    if request and hasattr(request, "state") and hasattr(request.state, "user"):
        username = getattr(request.state.user, "username", "")

    prompt = AgentTeamUserPrompt(
        task_id=task_id,
        content=content,
        status="pending",
        submitted_by=username or "super_admin",
    )
    db.add(prompt)

    # 可续跑终态：写入 prompt 后触发 follow-up iteration
    # 使用乐观锁防止并发请求重复触发 follow-up：
    # 将状态转换拆为独立的 UPDATE WHERE 语句，通过受影响行数判断是否竞态成功
    is_resumable_terminal = task.status in _RESUMABLE_TERMINAL_STATUSES and bool(
        task.workspace_path and task.branch_name and task.pr_number
    )
    if is_resumable_terminal:
        expected_updated_at = task.updated_at
        from sqlalchemy import update as sa_update

        optimistic_result = await db.execute(
            sa_update(AgentTeamTask)
            .where(
                AgentTeamTask.id == task_id,
                AgentTeamTask.status == task.status,
                AgentTeamTask.updated_at == expected_updated_at,
            )
            .values(
                status=AgentTeamTaskStatus.ITERATING.value,
                current_phase="human_followup",
                updated_at=_utc_now(),
            )
        )
        await db.commit()
        if optimistic_result.rowcount == 0:
            # 并发冲突：其他请求已抢先转换状态，跳过 follow-up
            is_resumable_terminal = False
            logger.info(
                "submit_user_prompt 乐观锁检测到并发冲突，跳过 follow-up: task_id={}",
                task_id,
            )
        else:
            await db.refresh(task)
    else:
        await db.commit()

    await db.refresh(prompt)

    # SSE: 通知前端有新 prompt
    try:
        from backend.webui.sse import publish_event

        sse_data = {
            "task_id": task_id,
            "prompt_id": prompt.id,
        }
        if is_resumable_terminal:
            sse_data["task_status"] = task.status
            sse_data["current_phase"] = task.current_phase
        await publish_event("agent:prompt_received", sse_data)
        # 续跑终态时额外发 task_updated 让前端刷新状态
        if is_resumable_terminal:
            await publish_event(
                "agent:task_updated",
                {
                    "task_id": task_id,
                    "status": task.status,
                    "current_phase": task.current_phase,
                },
            )
    except Exception as exc:
        logger.debug("SSE 发布 prompt 通知失败: {}", exc)

    # 可续跑终态：后台调度 follow-up iteration
    if is_resumable_terminal:
        try:
            from backend.workers.agent_team_worker import (
                submit_agent_team_human_followup,
            )

            create_registered_background_task(
                submit_agent_team_human_followup(task_id), "agent_team_human_followup"
            )
        except Exception as exc:
            logger.warning("调度 Agent follow-up iteration 失败: {}", exc)

    return JSONResponse(
        {
            "success": True,
            "prompt_id": prompt.id,
            "task_status": task.status,
        }
    )


@router.get("/api/tasks/{task_id}/prompts")
async def list_user_prompts(
    task_id: int,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取任务的用户引导 Prompt 列表。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse(
            {"success": False, "error": "Task not found"}, status_code=404
        )
    if not _is_admin(user) and task.started_by != user["sub"]:
        return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)

    rows = (
        (
            await db.execute(
                select(AgentTeamUserPrompt)
                .where(AgentTeamUserPrompt.task_id == task_id)
                .order_by(AgentTeamUserPrompt.created_at)
            )
        )
        .scalars()
        .all()
    )

    return JSONResponse(
        {
            "success": True,
            "prompts": [
                {
                    "id": p.id,
                    "content": p.content,
                    "status": p.status,
                    "submitted_by": p.submitted_by,
                    "created_at": format_rfc3339(p.created_at)
                    if p.created_at
                    else None,
                    "consumed_at": format_rfc3339(p.consumed_at)
                    if p.consumed_at
                    else None,
                }
                for p in rows
            ],
        }
    )
