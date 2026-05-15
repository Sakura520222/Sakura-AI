"""WebUI Agent 专家团队路由（超级管理员专用）"""

import json
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
    AgentTeamTask,
    AgentTeamTaskStatus,
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
    "agent_team_timeout_seconds",
    "agent_team_max_concurrent",
    "agent_team_min_priority",
    "agent_team_feasibility_keywords",
    "agent_team_max_iterations_per_task",
    "agent_team_max_tool_rounds",
    "agent_team_max_runtime_minutes",
    "agent_team_draft_pr",
    "agent_team_max_files_changed",
    "agent_team_max_lines_changed",
    "agent_team_run_tests",
    "agent_team_test_command_allowlist",
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
            "agent_team_max_runtime_minutes",
            "agent_team_draft_pr",
            "agent_team_max_files_changed",
            "agent_team_max_lines_changed",
            "agent_team_run_tests",
            "agent_team_test_command_allowlist",
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

    task = await service.create_task_from_candidate(
        db,
        candidate,
        started_by=user["sub"],
        ai_config_snapshot=config.safe_snapshot(),
    )
    await log_admin_action(
        db,
        user["user_id"],
        "agent_team_task_create",
        "agent_team_task",
        str(task.id),
        {"source_type": source_type, "source_id": source_id},
    )

    # 提交给后台 worker 执行，避免阻塞 HTTP 请求导致前端/反代超时
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
