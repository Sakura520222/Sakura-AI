"""API v1 Issue 分析端点"""

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import IssueAnalysis
from backend.webui.deps import (
    get_db,
    paginate,
    build_user_scope_filter,
)

from backend.api.v1.deps import require_api_auth
from backend.api.v1.schemas import IssueAnalysisResponse
from backend.api.v1.responses import success_response, error_response, paginated_response

router = APIRouter(prefix="/issues", tags=["Issues"])


@router.get("")
async def list_issues(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    search: str = Query("", description="搜索关键词"),
    repo_name: str = Query("", description="按仓库过滤"),
    category: str = Query("", description="按分类过滤"),
    priority: str = Query("", description="按优先级过滤"),
    status: str = Query("", description="按状态过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """Issue 分析列表（分页、搜索、过滤）"""
    query = select(IssueAnalysis)
    count_query = select(func.count(IssueAnalysis.id))

    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        query = query.where(scope_filter)
        count_query = count_query.where(scope_filter)

    if search:
        escaped = search.replace('%', r'\%').replace('_', r'\_')
        pattern = f"%{escaped}%"
        search_filter = or_(
            IssueAnalysis.title.like(pattern),
            IssueAnalysis.repo_name.like(pattern),
            IssueAnalysis.author.like(pattern),
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

    analyses, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = []
    for a in analyses:
        data = IssueAnalysisResponse.model_validate(a, from_attributes=True).model_dump()
        # 解析 JSON 字段
        try:
            data["suggested_labels"] = json.loads(a.suggested_labels) if a.suggested_labels else []
        except (json.JSONDecodeError, TypeError):
            data["suggested_labels"] = []
        try:
            data["suggested_assignees"] = json.loads(a.suggested_assignees) if a.suggested_assignees else []
        except (json.JSONDecodeError, TypeError):
            data["suggested_assignees"] = []
        try:
            data["related_prs"] = json.loads(a.related_prs) if a.related_prs else []
        except (json.JSONDecodeError, TypeError):
            data["related_prs"] = []
        items.append(data)

    return paginated_response(items, total, page, total_pages, per_page)


@router.get("/stats")
async def issue_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """Issue 统计数据"""
    from backend.services.issue_service import issue_service

    # IssueAnalysis 需支持 repo_owner/author 字段以正确过滤
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    stats = await issue_service.get_issue_stats(db, scope_filter=scope_filter)
    return success_response(data=stats)


@router.get("/{issue_id}")
async def get_issue(
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """Issue 分析详情"""
    query = select(IssueAnalysis).where(IssueAnalysis.id == issue_id)
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        query = query.where(scope_filter)

    analysis = (await db.execute(query)).scalar_one_or_none()
    if not analysis:
        return error_response("分析记录不存在或无权访问", status_code=404)

    data = IssueAnalysisResponse.model_validate(analysis, from_attributes=True).model_dump()
    # 解析 JSON 字段
    for field in ("suggested_labels", "suggested_assignees", "related_prs"):
        raw = getattr(analysis, field)
        try:
            data[field] = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            data[field] = []

    return success_response(data=data)


@router.post("/{issue_id}/reanalyze")
async def reanalyze_issue(
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """重新分析 Issue"""
    query = select(IssueAnalysis).where(IssueAnalysis.id == issue_id)
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        query = query.where(scope_filter)

    analysis = (await db.execute(query)).scalar_one_or_none()
    if not analysis:
        return error_response("记录不存在或无权访问", status_code=404)

    # 构造 issue_info
    issue_info = {
        "issue_number": analysis.issue_number,
        "repo_name": analysis.repo_name,
        "repo_owner": analysis.repo_owner,
        "author": analysis.author,
        "title": analysis.title,
        "body": analysis.body,
    }

    # 计算分析版本号
    max_version_result = await db.execute(
        select(func.max(IssueAnalysis.analysis_version)).where(
            IssueAnalysis.issue_number == analysis.issue_number,
            IssueAnalysis.repo_name == analysis.repo_name,
        )
    )
    max_version = max_version_result.scalar() or 0
    issue_info["analysis_version"] = max_version + 1

    try:
        from backend.workers.issue_worker import submit_issue_analysis_task

        task_id = await submit_issue_analysis_task(issue_info)
        return success_response(data={"task_id": task_id})
    except Exception as e:
        return error_response(str(e), status_code=500)
