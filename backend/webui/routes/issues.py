"""WebUI Issue 分析管理路由"""

import json
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import IssueAnalysis, IssueAnalysisStatus
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer, get_user_preferences, paginate, error_page

router = APIRouter(prefix="/issues", tags=["WebUI Issues"])
templates = get_templates()


@router.get("/")
async def issue_list_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染 Issue 分析列表页面"""
    return templates.TemplateResponse("issues.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "issues",
        "user_prefs": user_prefs,
    })


@router.get("/list-fragment")
async def issue_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词"),
    repo_name: str = Query("", description="按仓库过滤"),
    category: str = Query("", description="按分类过滤"),
    priority: str = Query("", description="按优先级过滤"),
    status: str = Query("", description="按状态过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """Issue 列表 HTMX 片段"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]

    query = select(IssueAnalysis)
    count_query = select(func.count(IssueAnalysis.id))

    if search:
        search_pattern = f"%{search}%"
        from sqlalchemy import or_
        search_filter = or_(
            IssueAnalysis.title.like(search_pattern),
            IssueAnalysis.repo_name.like(search_pattern),
            IssueAnalysis.author.like(search_pattern),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if repo_name:
        query = query.where(IssueAnalysis.repo_name == repo_name)
        count_query = count_query.where(IssueAnalysis.repo_name == repo_name)
    if category:
        query = query.where(IssueAnalysis.category == category)
        count_query = count_query.where(IssueAnalysis.category == category)
    if priority:
        query = query.where(IssueAnalysis.priority == priority)
        count_query = count_query.where(IssueAnalysis.priority == priority)
    if status:
        query = query.where(IssueAnalysis.status == status)
        count_query = count_query.where(IssueAnalysis.status == status)

    query = query.order_by(desc(IssueAnalysis.created_at))
    analyses, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

    return templates.TemplateResponse("components/issue_list_fragment.html", {
        "request": request,
        "analyses": analyses,
        "search": search,
        "repo_name": repo_name,
        "category": category,
        "priority": priority,
        "status": status,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


@router.get("/stats")
async def issue_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """Issue 统计数据"""
    from backend.services.issue_service import issue_service
    stats = await issue_service.get_issue_stats(db)
    return templates.TemplateResponse("components/issue_stats_cards.html", {
        "request": request,
        "stats": stats,
    })


@router.get("/{issue_id}")
async def issue_detail_page(
    request: Request,
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
) -> HTMLResponse:
    """Issue 分析详情页面"""
    result = await db.execute(
        select(IssueAnalysis).where(IssueAnalysis.id == issue_id)
    )
    analysis = result.scalar_one_or_none()
    if not analysis:
        return error_page(request, message="分析记录不存在", user=user)

    # 解析 JSON 字段
    suggested_labels = []
    try:
        suggested_labels = json.loads(analysis.suggested_labels) if analysis.suggested_labels else []
    except (json.JSONDecodeError, TypeError):
        pass

    suggested_assignees = []
    try:
        suggested_assignees = json.loads(analysis.suggested_assignees) if analysis.suggested_assignees else []
    except (json.JSONDecodeError, TypeError):
        pass

    related_prs = []
    try:
        related_prs = json.loads(analysis.related_prs) if analysis.related_prs else []
    except (json.JSONDecodeError, TypeError):
        pass

    return templates.TemplateResponse("issue_detail.html", {
        "request": request,
        "current_user": user,
        "analysis": analysis,
        "suggested_labels": suggested_labels,
        "suggested_assignees": suggested_assignees,
        "related_prs": related_prs,
        "active_page": "issues",
        "user_prefs": user_prefs,
    })


@router.get("/{issue_id}/detail-fragment")
async def issue_detail_fragment(
    request: Request,
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """Issue 详情 HTMX 片段"""
    result = await db.execute(
        select(IssueAnalysis).where(IssueAnalysis.id == issue_id)
    )
    analysis = result.scalar_one_or_none()
    if not analysis:
        return HTMLResponse(content="<p>记录不存在</p>")

    return templates.TemplateResponse("components/issue_detail_fragment.html", {
        "request": request,
        "analysis": analysis,
    })


@router.post("/{issue_id}/reanalyze")
async def reanalyze_issue(
    request: Request,
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """重新分析 Issue"""
    result = await db.execute(
        select(IssueAnalysis).where(IssueAnalysis.id == issue_id)
    )
    analysis = result.scalar_one_or_none()
    if not analysis:
        return JSONResponse(content={"success": False, "message": "记录不存在"}, status_code=404)

    # 构造 issue_info
    issue_info = {
        "issue_number": analysis.issue_number,
        "repo_name": analysis.repo_name,
        "repo_owner": analysis.repo_owner,
        "author": analysis.author,
        "title": analysis.title,
        "body": analysis.body,
    }

    try:
        from backend.workers.issue_worker import submit_issue_analysis_task
        task_id = await submit_issue_analysis_task(issue_info)
        return JSONResponse(content={"success": True, "task_id": task_id})
    except Exception as e:
        return JSONResponse(content={"success": False, "message": str(e)}, status_code=500)
