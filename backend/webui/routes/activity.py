"""WebUI 实时活动监控路由

聚合 PR 审查、Issues 分析、仓库扫描三种任务的实时状态，
复用 Agent Team 的 Session/Message/ToolCheckPoint 模式实现对话流。
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.activity_conversation_models import (
    ActivityMessage,
    ActivitySession,
    ActivityToolCall,
)
from backend.models.database import (
    PRReview,
    PRStatus,
    IssueAnalysis,
    IssueAnalysisStatus,
)
from backend.models.scan_models import RepoScan, ScanStatus
from backend.services.activity_event_service import ActivityEventService
from backend.webui.deps import (
    require_auth,
    get_db,
    get_user_preferences,
    render_template,
    build_user_scope_filter,
)
from backend.webui.routes.activity_access import verify_task_access

router = APIRouter(prefix="/activity", tags=["WebUI Activity"])


# 活跃状态定义
_ACTIVE_PR_STATUSES = [PRStatus.PENDING.value, PRStatus.REVIEWING.value]
_ACTIVE_ISSUE_STATUSES = [
    IssueAnalysisStatus.PENDING.value,
    IssueAnalysisStatus.ANALYZING.value,
]
_ACTIVE_SCAN_STATUSES = [
    ScanStatus.PENDING.value,
    ScanStatus.INDEXING.value,
    ScanStatus.ANALYZING.value,
    ScanStatus.REPORTING.value,
]


@router.get("/")
async def activity_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """实时活动监控页面"""
    return render_template(
        "activity.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="activity",
    )


@router.get("/api/active-tasks")
async def get_active_tasks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取所有活跃任务（PR 审查 + Issue 分析 + 仓库扫描）"""
    tasks = []

    # --- 活跃 PR 审查 ---
    pr_query = (
        select(PRReview)
        .where(PRReview.status.in_(_ACTIVE_PR_STATUSES))
        .order_by(desc(PRReview.created_at))
        .limit(50)
    )
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        pr_query = pr_query.where(scope_filter)

    pr_result = await db.execute(pr_query)
    for pr in pr_result.scalars().all():
        tasks.append(
            {
                "type": "pr",
                "id": f"pr-{pr.id}",
                "task_id": pr.id,
                "repo_name": pr.repo_name,
                "title": pr.title or f"PR #{pr.pr_id}",
                "author": pr.author or "",
                "status": pr.status,
                "strategy": pr.strategy,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            }
        )

    # --- 活跃 Issue 分析 ---
    issue_query = (
        select(IssueAnalysis)
        .where(IssueAnalysis.status.in_(_ACTIVE_ISSUE_STATUSES))
        .order_by(desc(IssueAnalysis.created_at))
        .limit(50)
    )
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        issue_query = issue_query.where(scope_filter)

    issue_result = await db.execute(issue_query)
    for issue in issue_result.scalars().all():
        tasks.append(
            {
                "type": "issue",
                "id": f"issue-{issue.id}",
                "task_id": issue.id,
                "repo_name": issue.repo_name,
                "title": issue.title or f"Issue #{issue.issue_number}",
                "author": issue.author or "",
                "status": issue.status,
                "created_at": (
                    issue.created_at.isoformat() if issue.created_at else None
                ),
                "updated_at": (
                    issue.updated_at.isoformat() if issue.updated_at else None
                ),
            }
        )

    # --- 活跃仓库扫描 ---
    scan_query = (
        select(RepoScan)
        .where(RepoScan.status.in_(_ACTIVE_SCAN_STATUSES))
        .order_by(desc(RepoScan.created_at))
        .limit(50)
    )
    # 扫描记录没有 author 字段，仅按 repo_owner 过滤
    if user.get("role") not in ("admin", "super_admin"):
        scan_query = scan_query.where(RepoScan.repo_owner == user["sub"])

    scan_result = await db.execute(scan_query)
    for scan in scan_result.scalars().all():
        tasks.append(
            {
                "type": "scan",
                "id": f"scan-{scan.id}",
                "task_id": scan.id,
                "repo_name": scan.repo_name,
                "title": f"{scan.repo_name}",
                "author": scan.triggered_by or scan.trigger_type,
                "status": scan.status,
                "progress": scan.progress or 0,
                "current_phase": scan.current_phase or "",
                "created_at": (
                    scan.created_at.isoformat() if scan.created_at else None
                ),
                "updated_at": (
                    scan.updated_at.isoformat() if scan.updated_at else None
                ),
            }
        )

    # 按 updated_at 降序排序（最近更新的在前）
    tasks.sort(key=lambda t: t.get("updated_at") or "", reverse=True)

    return {"success": True, "tasks": tasks, "total": len(tasks)}


@router.get("/api/events/{task_type}/{task_id}")
async def get_task_events(
    task_type: str,
    task_id: int,
    after_id: int = 0,
    limit: int = 200,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取指定任务的活动事件列表（支持增量拉取）"""
    if task_type not in ("pr", "issue", "scan"):
        return {"success": False, "error": "Invalid task_type"}
    # Verify user has access to this task
    task_info = await verify_task_access(task_type, task_id, user, db)
    if not task_info:
        return {"success": False, "error": "Task not found or access denied"}
    events = await ActivityEventService.get_events(
        task_type, task_id, after_id=after_id, limit=limit
    )
    return {"success": True, "events": events}


@router.get("/api/recent-tasks")
async def get_recent_tasks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取最近完成的任务（用于活动流展示，包含已完成的）"""
    tasks = []
    limit = 20

    # --- 最近 PR 审查（含已完成） ---
    pr_query = (
        select(PRReview)
        .order_by(desc(PRReview.updated_at))
        .limit(limit)
    )
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        pr_query = pr_query.where(scope_filter)

    pr_result = await db.execute(pr_query)
    for pr in pr_result.scalars().all():
        tasks.append(
            {
                "type": "pr",
                "id": f"pr-{pr.id}",
                "task_id": pr.id,
                "repo_name": pr.repo_name,
                "title": pr.title or f"PR #{pr.pr_id}",
                "author": pr.author or "",
                "status": pr.status,
                "strategy": pr.strategy,
                "overall_score": pr.overall_score,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            }
        )

    # --- 最近 Issue 分析（含已完成） ---
    issue_query = (
        select(IssueAnalysis)
        .order_by(desc(IssueAnalysis.updated_at))
        .limit(limit)
    )
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        issue_query = issue_query.where(scope_filter)

    issue_result = await db.execute(issue_query)
    for issue in issue_result.scalars().all():
        tasks.append(
            {
                "type": "issue",
                "id": f"issue-{issue.id}",
                "task_id": issue.id,
                "repo_name": issue.repo_name,
                "title": issue.title or f"Issue #{issue.issue_number}",
                "author": issue.author or "",
                "status": issue.status,
                "created_at": (
                    issue.created_at.isoformat() if issue.created_at else None
                ),
                "updated_at": (
                    issue.updated_at.isoformat() if issue.updated_at else None
                ),
            }
        )

    # --- 最近仓库扫描（含已完成） ---
    scan_query = (
        select(RepoScan)
        .order_by(desc(RepoScan.updated_at))
        .limit(limit)
    )
    if user.get("role") not in ("admin", "super_admin"):
        scan_query = scan_query.where(RepoScan.repo_owner == user["sub"])

    scan_result = await db.execute(scan_query)
    for scan in scan_result.scalars().all():
        tasks.append(
            {
                "type": "scan",
                "id": f"scan-{scan.id}",
                "task_id": scan.id,
                "repo_name": scan.repo_name,
                "title": f"{scan.repo_name}",
                "author": scan.triggered_by or scan.trigger_type,
                "status": scan.status,
                "progress": scan.progress or 0,
                "current_phase": scan.current_phase or "",
                "created_at": (
                    scan.created_at.isoformat() if scan.created_at else None
                ),
                "updated_at": (
                    scan.updated_at.isoformat() if scan.updated_at else None
                ),
            }
        )

    # 按 updated_at 降序排序
    tasks.sort(key=lambda t: t.get("updated_at") or "", reverse=True)

    return {"success": True, "tasks": tasks[:50]}


@router.get("/api/tasks/{task_type}/{task_id}/stream-data")
async def activity_stream_data(
    task_type: str,
    task_id: int,
    after_id: int = 0,
    limit: int = 200,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """获取任务的对话流数据 — 返回格式与 Agent Team task_stream_data 完全一致。

    前端 agent_team_live_view_fragment.html 可直接渲染。
    """
    if task_type not in ("pr", "issue", "scan"):
        return JSONResponse({"success": False, "error": "Invalid task_type"}, status_code=400)

    task_info = await verify_task_access(task_type, task_id, user, db)
    if not task_info:
        return JSONResponse({"success": False, "error": "Task not found or access denied"}, status_code=404)

    # Query sessions for this task
    session_rows = (
        await db.execute(
            select(ActivitySession)
            .where(
                ActivitySession.source_type == task_type,
                ActivitySession.source_task_id == task_id,
            )
            .order_by(ActivitySession.id)
        )
    ).scalars().all()
    session_ids = [s.id for s in session_rows]

    if not session_ids:
        return JSONResponse({
            "success": True,
            "messages": [],
            "tool_calls": [],
            "sessions": [],
            "has_more": False,
        })

    # Messages with pagination
    msg_query = (
        select(ActivityMessage)
        .where(
            ActivityMessage.session_id.in_(session_ids),
            ActivityMessage.id > after_id,
        )
        .order_by(ActivityMessage.id)
        .limit(limit + 1)
    )
    msg_rows = (await db.execute(msg_query)).scalars().all()
    has_more = len(msg_rows) > limit
    msg_rows = msg_rows[:limit]

    # Tool calls (status can change without new messages)
    tc_rows = (
        await db.execute(
            select(ActivityToolCall)
            .where(ActivityToolCall.session_id.in_(session_ids))
            .order_by(ActivityToolCall.id)
        )
    ).scalars().all()

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
            for tc in tc_rows
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
        "has_more": has_more,
        "task_status": task_info.get("status", ""),
    })
