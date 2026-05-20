"""WebUI Agent 专家团队路由（超级管理员专用）"""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.config import (
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SENSITIVE_KEYS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    get_all_dynamic_config_keys,
    get_dynamic_config_input_type,
    get_settings,
    invalidate_dynamic_config_cache,
    mask_sensitive_value,
    update_settings_field,
)
from backend.models.agent_team_models import (
    AgentTeamIteration,
    AgentTeamMessage,
    AgentTeamSession,
    AgentTeamTask,
    AgentTeamTaskStatus,
    AgentTeamToolCall,
    AgentTeamUserPrompt,
)
from backend.models.database import AppConfig
from backend.services.agent_team.ai_client import (
    load_agent_team_ai_config,
    resolve_agent_team_max_iterations,
)
from backend.services.agent_team.candidate_service import (
    AgentTeamCandidateService,
    candidates_to_dicts,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    paginate,
    render_template,
    require_csrf,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/agent-team", tags=["WebUI Agent Team"])

AGENT_TEAM_CONFIG_KEYS = [
    "agent_team_enabled",
    "agent_team_workspace_root",
    "agent_team_repo_allowlist",
    "agent_team_model_provider",
    "agent_team_api_base",
    "agent_team_api_key",
    "agent_team_model",
    "agent_team_review_model",
    "agent_team_summary_model",
    "agent_team_temperature",
    "agent_team_max_tokens",
    "agent_team_enable_context_compression",
    "agent_team_context_compression_threshold",
    "agent_team_context_compression_keep_rounds",
    "agent_team_context_summary_max_tokens",
    "agent_team_timeout_seconds",
    "agent_team_max_concurrent",
    "agent_team_min_priority",
    "agent_team_feasibility_keywords",
    "agent_team_max_iterations_per_task",
    "agent_team_max_tool_rounds",
    "agent_team_reviewer_max_tool_rounds",
    "agent_team_max_runtime_minutes",
    "agent_team_draft_pr",
    "agent_team_max_files_changed",
    "agent_team_max_lines_changed",
    "agent_team_run_tests",
    "agent_team_auto_install_deps",
    "agent_team_test_command_allowlist",
    "agent_team_test_command_blocklist",
    "agent_team_skills_enabled",
    "agent_team_skills_root",
]

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
    max_iterations: str | None = None,
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

    iterations = _parse_optional_int(max_iterations, "max_iterations")
    if iterations is not None:
        if iterations < 1:
            raise ValueError("max_iterations 必须大于 0")
        overrides["max_iterations"] = iterations

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


AGENT_TEAM_CONFIG_GROUPS = [
    {
        "key": "basic",
        "title_key": "agent_team.config_group_basic",
        "description_key": "agent_team.config_group_basic_desc",
        "keys": [
            "agent_team_enabled",
            "agent_team_workspace_root",
            "agent_team_repo_allowlist",
        ],
    },
    {
        "key": "ai",
        "title_key": "agent_team.config_group_ai",
        "description_key": "agent_team.config_group_ai_desc",
        "keys": [
            "agent_team_model_provider",
            "agent_team_api_base",
            "agent_team_api_key",
            "agent_team_model",
            "agent_team_review_model",
            "agent_team_summary_model",
            "agent_team_temperature",
            "agent_team_max_tokens",
            "agent_team_enable_context_compression",
            "agent_team_context_compression_threshold",
            "agent_team_context_compression_keep_rounds",
            "agent_team_context_summary_max_tokens",
            "agent_team_timeout_seconds",
        ],
    },
    {
        "key": "guardrails",
        "title_key": "agent_team.config_group_guardrails",
        "description_key": "agent_team.config_group_guardrails_desc",
        "keys": [
            "agent_team_max_concurrent",
            "agent_team_min_priority",
            "agent_team_feasibility_keywords",
            "agent_team_max_iterations_per_task",
            "agent_team_max_tool_rounds",
            "agent_team_reviewer_max_tool_rounds",
            "agent_team_max_runtime_minutes",
            "agent_team_draft_pr",
            "agent_team_max_files_changed",
            "agent_team_max_lines_changed",
            "agent_team_run_tests",
            "agent_team_auto_install_deps",
            "agent_team_test_command_allowlist",
            "agent_team_test_command_blocklist",
        ],
    },
    {
        "key": "skills",
        "title_key": "agent_team.config_group_skills",
        "description_key": "agent_team.config_group_skills_desc",
        "keys": [
            "agent_team_skills_enabled",
            "agent_team_skills_root",
        ],
    },
]


@router.get("/")
async def agent_team_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Agent 专家团队独立页面。"""
    lang = detect_language(user_prefs)
    config_items = await _load_config_items(db, lang=lang)
    stats = await _load_stats(db)
    config_groups = _group_config_items(config_items, lang=lang)
    return render_template(
        "agent_team.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="agent_team",
        config_items=config_items,
        config_groups=config_groups,
        stats=stats,
        workspace_summary=_load_workspace_summary(),
        status_options=[status.value for status in AgentTeamTaskStatus],
    )


@router.get("/list-fragment")
async def task_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
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
    user: dict = Depends(require_super_admin),
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
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )

    # selectinload 不支持在当前 SQLAlchemy 版本中稳定地继续链式排序，模板侧按序展示即可。
    task.iterations.sort(key=lambda item: item.iteration_number)
    task.feedback.sort(key=lambda item: item.created_at, reverse=True)
    for iteration in task.iterations:
        iteration.patch_files.sort(key=lambda item: item.file_path)

    return render_template(
        "components/agent_team_task_detail_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        task=task,
    )


@router.post("/config/save")
async def save_agent_team_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存 Agent 专家团队专用配置。"""
    form = await request.form()
    changed: dict[str, dict] = {}
    settings = get_settings()

    for key in AGENT_TEAM_CONFIG_KEYS:
        is_sensitive = key in DYNAMIC_CONFIG_SENSITIVE_KEYS
        if is_sensitive and form.get(f"{key}_changed") != "true":
            continue

        raw = form.get(key)
        if raw is None:
            field_type = type(getattr(settings, key, ""))
            if field_type is bool:
                raw = "false"
            else:
                continue
        val = str(raw).strip()

        if key in DYNAMIC_CONFIG_RANGES:
            min_v, max_v = DYNAMIC_CONFIG_RANGES[key]
            try:
                num_val = float(val)
            except ValueError:
                return toast_redirect(
                    "/agent-team/",
                    "toast.numeric_required",
                    "error",
                    lang=detect_language(),
                    field_key=key,
                )
            if not (min_v <= num_val <= max_v):
                return toast_redirect(
                    "/agent-team/",
                    "toast.value_range",
                    "error",
                    lang=detect_language(),
                    field_key=key,
                    min_v=min_v,
                    max_v=max_v,
                )

        if key in DYNAMIC_CONFIG_SELECT_OPTIONS:
            valid_values = [opt["value"] for opt in DYNAMIC_CONFIG_SELECT_OPTIONS[key]]
            if val not in valid_values:
                return toast_redirect(
                    "/agent-team/",
                    "toast.value_invalid",
                    "error",
                    lang=detect_language(),
                    field_key=key,
                )

        result = await db.execute(select(AppConfig).where(AppConfig.key_name == key))
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = AppConfig(key_name=key, key_value=val, description=key)
            db.add(cfg)
            changed[key] = {
                "old": "(无)",
                "new": mask_sensitive_value(val) if is_sensitive else val,
                "raw_new": val,
            }
        elif cfg.key_value != val:
            changed[key] = {
                "old": mask_sensitive_value(cfg.key_value)
                if is_sensitive
                else cfg.key_value,
                "new": mask_sensitive_value(val) if is_sensitive else val,
                "raw_new": val,
            }
            cfg.key_value = val

    if not changed:
        return toast_redirect(
            "/agent-team/", "toast.config_saved_live", lang=detect_language()
        )

    await db.commit()
    invalidate_dynamic_config_cache(AGENT_TEAM_CONFIG_KEYS)
    all_dynamic_keys = get_all_dynamic_config_keys()
    for key, change in changed.items():
        if key in all_dynamic_keys:
            update_settings_field(key, change.get("raw_new", change["new"]))

    log_changed = {
        key: {"old": value["old"], "new": value["new"]}
        for key, value in changed.items()
    }
    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_config_save",
        "agent_team",
        None,
        log_changed,
    )
    return toast_redirect(
        "/agent-team/", "toast.config_saved_live", lang=detect_language()
    )


@router.get("/api/repos/{owner}/{name}/branches")
async def list_repo_branches(
    owner: str,
    name: str,
    _=Depends(require_super_admin),
):
    """获取仓库分支列表，供任务创建弹窗选择基础分支。"""
    try:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        client = github_app.get_repo_client(owner, name)
        if not client:
            return JSONResponse(
                {"success": False, "message": f"无法获取 GitHub 客户端: {owner}/{name}"},
                status_code=200,
            )
        repo = client.get_repo(f"{owner}/{name}")
        default_branch = repo.default_branch or "main"
        branches = []
        for branch in repo.get_branches()[:100]:
            branches.append(branch.name)
        return JSONResponse({
            "success": True,
            "default_branch": default_branch,
            "branches": branches,
        })
    except Exception as exc:
        return JSONResponse(
            {"success": False, "message": f"获取分支列表失败: {exc}"},
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
    except Exception as exc:
        return JSONResponse(
            {"success": False, "message": f"AI 筛选候选失败: {exc}"}, status_code=200
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
    max_iterations: str = Form(""),
):
    """从候选来源创建 Agent 任务。"""
    try:
        config = await load_agent_team_ai_config()
        config.validate()
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"AI 配置加载失败: {e}"},
            status_code=200,
        )
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
            max_iterations=max_iterations,
        )
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)

    task = await service.create_task_from_candidate(
        db,
        candidate,
        started_by=user["sub"],
        ai_config_snapshot=config.safe_snapshot(),
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
            "max_iterations": task.max_iterations,
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
    m = re.match(
        r"(?:https?://)?github\.com/([^/]+/[^/]+)/issues/(\d+)", ref
    )
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
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    issue_ref: str = Form(...),
):
    """从指定 Issue 构建可编辑任务草稿。"""
    try:
        repo_full_name, issue_number = _parse_issue_ref(issue_ref)
        draft = await AgentTeamCandidateService().build_manual_issue_task_draft(
            db, repo_full_name, issue_number
        )
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )
    return JSONResponse({"success": True, "draft": draft})


@router.post("/tasks/create-from-issue")
async def create_task_from_issue(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
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
    max_iterations: str = Form(""),
):
    """从指定仓库的 Issue 直接创建 Agent 任务。"""
    try:
        repo_full_name, issue_number = _parse_issue_ref(issue_ref)
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )

    try:
        config = await load_agent_team_ai_config()
        config.validate()
    except ValueError as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=200,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"AI 配置加载失败: {e}"},
            status_code=200,
        )

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
            max_iterations=max_iterations,
        )
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)

    service = AgentTeamCandidateService()
    try:
        task = await service.create_task_from_manual_issue(
            db,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            started_by=user["sub"],
            ai_config_snapshot=config.safe_snapshot(),
            base_branch=base_branch.strip() or None,
            overrides=overrides,
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
            "max_iterations": task.max_iterations,
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
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """重试失败或卡住的任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
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

    try:
        config = await load_agent_team_ai_config()
        config.validate()
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"AI 配置加载失败: {e}"}, status_code=200
        )

    old_status = task.status
    task.status = AgentTeamTaskStatus.QUEUED.value
    task.current_phase = None
    task.max_iterations = await resolve_agent_team_max_iterations()
    task.started_at = None
    task.completed_at = None
    task.error_message = None
    task.ai_config_snapshot = json.dumps(config.safe_snapshot(), ensure_ascii=False)
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
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """从已持久化 messages 和工作区继续运行任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
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

    try:
        config = await load_agent_team_ai_config()
        config.validate()
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=200)
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"AI 配置加载失败: {e}"}, status_code=200
        )

    old_status = task.status
    task.status = AgentTeamTaskStatus.QUEUED.value
    task.current_phase = "resuming"
    task.resume_count = (task.resume_count or 0) + 1
    task.completed_at = None
    task.error_message = None
    task.ai_config_snapshot = json.dumps(config.safe_snapshot(), ensure_ascii=False)
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
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """取消任务。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
        )

    cancellable = {
        "queued",
        "planning",
        "cloning",
        "editing",
        "self_reviewing",
        "validating",
        "iterating",
    }
    if task.status not in cancellable:
        return JSONResponse(
            {"success": False, "message": f"当前状态 {task.status} 不可取消"},
            status_code=200,
        )

    old_status = task.status
    task.status = AgentTeamTaskStatus.CANCELLED.value
    task.completed_at = datetime.now(timezone.utc)
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
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """删除任务及其迭代/反馈/变更文件记录。"""
    result = await db.execute(select(AgentTeamTask).where(AgentTeamTask.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        return JSONResponse(
            {"success": False, "message": "任务不存在"}, status_code=404
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
            AgentTeamTask.repo_owner == repo_owner,
            AgentTeamTask.repo_name == repo_name,
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
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

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


async def _run_agent_task_background(task_id: int) -> None:
    """后台执行 Agent 任务，避免阻塞 WebUI 请求。"""
    try:
        from backend.workers.agent_team_worker import submit_agent_team_task

        await submit_agent_team_task(task_id)
    except Exception as exc:
        from loguru import logger

        logger.error(
            "Agent 后台任务提交失败: task_id={}, error={}", task_id, exc, exc_info=True
        )


async def _resume_agent_task_background(task_id: int) -> None:
    """后台续跑 Agent 任务，避免阻塞 WebUI 请求。"""
    try:
        from backend.workers.agent_team_worker import resume_agent_team_task

        await resume_agent_team_task(task_id)
    except Exception as exc:
        from loguru import logger

        logger.error(
            "Agent 后台任务续跑失败: task_id={}, error={}", task_id, exc, exc_info=True
        )


async def _load_config_items(db: AsyncSession, lang: str = "zh-CN") -> list[dict]:
    from backend.webui.i18n import i18n as _i18n

    result = await db.execute(
        select(AppConfig).where(AppConfig.key_name.in_(AGENT_TEAM_CONFIG_KEYS))
    )
    config_map = {cfg.key_name: cfg.key_value for cfg in result.scalars().all()}
    settings = get_settings()
    items = []
    for key in AGENT_TEAM_CONFIG_KEYS:
        value = config_map.get(key, str(getattr(settings, key, "")))
        default_val = str(getattr(settings, key, ""))
        is_sensitive = key in DYNAMIC_CONFIG_SENSITIVE_KEYS

        # 翻译标签 / Translate label
        label_key = f"config.label.{key}"
        translated_label = _i18n.t(label_key, lang=lang)
        label = translated_label if translated_label != label_key else key

        # 翻译描述 / Translate description
        desc_key = f"config.desc.{key}"
        translated_desc = _i18n.t(desc_key, lang=lang)
        description = translated_desc if translated_desc != desc_key else ""

        # 翻译 select options / Translate select options
        raw_options = DYNAMIC_CONFIG_SELECT_OPTIONS.get(key, [])
        translated_options = []
        for opt in raw_options:
            opt_key = f"config.option.{key}_{opt['value']}"
            opt_label = _i18n.t(opt_key, lang=lang)
            translated_options.append(
                {
                    "value": opt["value"],
                    "label": opt_label if opt_key != opt_label else opt["label"],
                }
            )

        items.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "input_type": get_dynamic_config_input_type(key),
                "value": mask_sensitive_value(value)
                if is_sensitive and value
                else value,
                "default": mask_sensitive_value(default_val)
                if is_sensitive and default_val
                else default_val,
                "sensitive": is_sensitive,
                "select_options": translated_options,
                "min_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[0],
                "max_val": DYNAMIC_CONFIG_RANGES.get(key, (None, None))[1],
            }
        )
    return items


def _group_config_items(config_items: list[dict], lang: str = "zh-CN") -> list[dict]:
    """按界面分组组织 Agent Team 配置项。"""
    from backend.webui.i18n import i18n as _i18n

    item_map = {item["key"]: item for item in config_items}
    groups = []
    used_keys = set()
    for group in AGENT_TEAM_CONFIG_GROUPS:
        group_items = [item_map[key] for key in group["keys"] if key in item_map]
        used_keys.update(item["key"] for item in group_items)
        groups.append(
            {
                "key": group["key"],
                "title": _i18n.t(group["title_key"], lang=lang),
                "description": _i18n.t(group["description_key"], lang=lang),
                "items": group_items,
            }
        )

    remaining = [item for item in config_items if item["key"] not in used_keys]
    if remaining:
        groups.append(
            {
                "key": "advanced",
                "title": _i18n.t("agent_team.config_group_advanced", lang=lang),
                "description": _i18n.t(
                    "agent_team.config_group_advanced_desc", lang=lang
                ),
                "items": remaining,
            }
        )
    return groups


async def _load_stats(db: AsyncSession) -> dict:
    total = await db.scalar(select(func.count(AgentTeamTask.id)))
    active = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES)
        )
    )
    completed = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(AgentTeamTask.status == "completed")
    )
    failed = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(AgentTeamTask.status == "failed")
    )
    queued = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(AgentTeamTask.status == "queued")
    )
    waiting_human = await db.scalar(
        select(func.count(AgentTeamTask.id)).where(
            AgentTeamTask.status == "waiting_human"
        )
    )
    status_rows = await db.execute(
        select(AgentTeamTask.status, func.count(AgentTeamTask.id)).group_by(
            AgentTeamTask.status
        )
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
        "modified_at": datetime.fromtimestamp(info.modified_at, tz=timezone.utc)
        if info.modified_at
        else None,
        "has_git": info.has_git,
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
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """获取活跃任务列表（供 Live View 下拉框使用，含最近完成的任务以便回看对话）。"""
    rows = (await db.execute(
        select(AgentTeamTask)
        .where(
            AgentTeamTask.status.in_(AGENT_TEAM_ACTIVE_STATUSES)
            | (
                AgentTeamTask.status.in_([
                    AgentTeamTaskStatus.COMPLETED.value,
                    AgentTeamTaskStatus.FAILED.value,
                    AgentTeamTaskStatus.CANCELLED.value,
                    AgentTeamTaskStatus.PR_OPENED.value,
                ])
                & AgentTeamTask.completed_at.isnot(None)
            )
        )
        .order_by(desc(AgentTeamTask.updated_at))
        .limit(30)
    )).scalars().all()

    return JSONResponse({
        "success": True,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "repo_full_name": t.repo_full_name,
                "current_phase": t.current_phase,
            }
            for t in rows
        ],
    })


@router.get("/api/tasks/{task_id}/stream-data")
async def task_stream_data(
    task_id: int,
    after_id: int = 0,
    limit: int = 50,
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """获取任务的消息流数据（messages + tool_calls + sessions + prompts）。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse({"success": False, "error": "Task not found"}, status_code=404)

    # Sessions for this task
    session_rows = (await db.execute(
        select(AgentTeamSession)
        .where(AgentTeamSession.task_id == task_id)
        .order_by(AgentTeamSession.id)
    )).scalars().all()
    session_ids = [s.id for s in session_rows]

    if not session_ids:
        return JSONResponse({
            "success": True,
            "messages": [],
            "tool_calls": [],
            "sessions": [],
            "prompts": [],
            "has_more": False,
        })

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
    tool_call_rows = (await db.execute(
        select(AgentTeamToolCall)
        .where(AgentTeamToolCall.session_id.in_(session_ids))
        .order_by(AgentTeamToolCall.id)
    )).scalars().all()

    # User prompts
    prompt_rows = (await db.execute(
        select(AgentTeamUserPrompt)
        .where(AgentTeamUserPrompt.task_id == task_id)
        .order_by(AgentTeamUserPrompt.created_at)
    )).scalars().all()

    return JSONResponse({
        "success": True,
        "messages": [
            {
                "id": m.id,
                "session_id": m.session_id,
                "seq": m.seq,
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "finish_reason": m.finish_reason,
                "created_at": m.created_at.isoformat() if m.created_at else None,
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
                "started_at": tc.started_at.isoformat() if tc.started_at else None,
                "completed_at": tc.completed_at.isoformat() if tc.completed_at else None,
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
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            }
            for s in session_rows
        ],
        "prompts": [
            {
                "id": p.id,
                "content": p.content,
                "status": p.status,
                "submitted_by": p.submitted_by,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "consumed_at": p.consumed_at.isoformat() if p.consumed_at else None,
            }
            for p in prompt_rows
        ],
        "has_more": has_more,
        "task_status": task.status,
    })


@router.post("/api/tasks/{task_id}/prompts")
async def submit_user_prompt(
    task_id: int,
    request: Request,
    content: str = Form(...),
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """提交管理员引导 Prompt（pending 状态，下次 AI 请求时注入）。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse({"success": False, "error": "Task not found"}, status_code=404)

    if task.status not in AGENT_TEAM_ACTIVE_STATUSES:
        return JSONResponse(
            {"success": False, "error": "Task is not active"},
            status_code=400,
        )

    content = content.strip()
    if not content:
        return JSONResponse({"success": False, "error": "Content is empty"}, status_code=400)

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
    await db.commit()
    await db.refresh(prompt)

    # SSE: 通知前端有新 prompt
    try:
        from backend.webui.sse import publish_event

        await publish_event("agent:prompt_received", {
            "task_id": task_id,
            "prompt_id": prompt.id,
        })
    except Exception as exc:
        from loguru import logger
        logger.debug("SSE 发布 prompt 通知失败: {}", exc)

    return JSONResponse({
        "success": True,
        "prompt_id": prompt.id,
    })


@router.get("/api/tasks/{task_id}/prompts")
async def list_user_prompts(
    task_id: int,
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """获取任务的管理员引导 Prompt 列表。"""
    task = await db.get(AgentTeamTask, task_id)
    if not task:
        return JSONResponse({"success": False, "error": "Task not found"}, status_code=404)

    rows = (await db.execute(
        select(AgentTeamUserPrompt)
        .where(AgentTeamUserPrompt.task_id == task_id)
        .order_by(AgentTeamUserPrompt.created_at)
    )).scalars().all()

    return JSONResponse({
        "success": True,
        "prompts": [
            {
                "id": p.id,
                "content": p.content,
                "status": p.status,
                "submitted_by": p.submitted_by,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "consumed_at": p.consumed_at.isoformat() if p.consumed_at else None,
            }
            for p in rows
        ],
    })
