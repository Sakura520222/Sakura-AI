"""WebUI 仓库扫描路由"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.scan_models import RepoScan, ScanFinding, ScanStatus
from backend.services.database_reset_runtime_service import (
    create_registered_background_task,
)
from backend.webui.deps import (
    error_page,
    get_csrf_serializer,
    get_db,
    get_templates,
    get_user_preferences,
    paginate,
    render_template,
    require_admin,
    require_csrf_header,
    require_super_admin,
)

router = APIRouter(prefix="/scans", tags=["WebUI Scans"])
templates = get_templates()


def _apply_scan_filters(q, search: str, repo_name: str, status: str):
    """为扫描列表查询应用公共过滤条件"""
    if search:
        search_pattern = f"%{search}%"
        q = q.where(
            or_(
                RepoScan.repo_name.like(search_pattern),
                RepoScan.error_message.like(search_pattern),
            )
        )
    if repo_name:
        q = q.where(RepoScan.repo_name == repo_name)
    if status:
        q = q.where(RepoScan.status == status)
    return q


@router.get("/")
async def scan_list_page(
    request: Request,
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """扫描列表页面"""
    return render_template(
        "scans.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="scans",
        csrf_token=get_csrf_serializer().dumps({}),
    )


@router.get("/list-fragment")
async def scan_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词"),
    repo_name: str = Query("", description="按仓库过滤"),
    status: str = Query("", description="按状态过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """扫描列表 HTMX 片段"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]

    query = select(RepoScan)
    count_query = select(func.count(RepoScan.id))

    # 构建公共过滤条件
    query = _apply_scan_filters(query, search, repo_name, status)
    count_query = _apply_scan_filters(count_query, search, repo_name, status)

    query = query.order_by(desc(RepoScan.created_at))
    scans, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    return templates.TemplateResponse(
        request,
        "components/scan_list_fragment.html",
        {
            "request": request,
            "scans": scans,
            "search": search,
            "repo_name": repo_name,
            "status": status,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": per_page,
        },
    )


@router.get("/stats")
async def scan_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
):
    """扫描统计数据"""
    # 总扫描数
    total_result = await db.execute(select(func.count(RepoScan.id)))
    total = total_result.scalar() or 0

    # 按状态统计
    status_result = await db.execute(
        select(RepoScan.status, func.count(RepoScan.id)).group_by(RepoScan.status)
    )
    status_counts = dict(status_result.all())

    # 平均健康评分
    avg_health_result = await db.execute(
        select(func.avg(RepoScan.overall_health_score)).where(
            RepoScan.status == ScanStatus.COMPLETED.value
        )
    )
    avg_health = avg_health_result.scalar()
    avg_health = int(avg_health) if avg_health else 0

    # 总发现数
    findings_result = await db.execute(select(func.count(ScanFinding.id)))
    total_findings = findings_result.scalar() or 0

    # 最近扫描时间
    last_scan_result = await db.execute(
        select(RepoScan.created_at)
        .where(RepoScan.status == ScanStatus.COMPLETED.value)
        .order_by(desc(RepoScan.completed_at))
        .limit(1)
    )
    last_scan_time = last_scan_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "components/scan_stats_cards.html",
        {
            "request": request,
            "stats": {
                "total": total,
                "completed": status_counts.get(ScanStatus.COMPLETED.value, 0),
                "failed": status_counts.get(ScanStatus.FAILED.value, 0),
                "running": status_counts.get(ScanStatus.INDEXING.value, 0)
                + status_counts.get(ScanStatus.ANALYZING.value, 0)
                + status_counts.get(ScanStatus.REPORTING.value, 0),
                "avg_health": avg_health,
                "total_findings": total_findings,
                "last_scan_time": last_scan_time,
            },
        },
    )


@router.get("/{scan_id}")
async def scan_detail_page(
    request: Request,
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
) -> HTMLResponse:
    """扫描详情页面"""
    scan = await db.get(RepoScan, scan_id)
    if not scan:
        return error_page(
            request,
            status_code=404,
            title="未找到",
            message="扫描记录不存在",
            user=user,
        )

    # 获取 findings，按严重性排序
    severity_order = {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}
    result = await db.execute(
        select(ScanFinding)
        .where(ScanFinding.scan_id == scan_id)
        .order_by(ScanFinding.severity, ScanFinding.confidence.desc())
    )
    findings = result.scalars().all()

    # 按严重性分组
    grouped_findings = {}
    for f in findings:
        grouped_findings.setdefault(f.severity, []).append(f)

    return render_template(
        "scan_detail.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="scans",
        scan=scan,
        findings=findings,
        grouped_findings=grouped_findings,
        severity_order=severity_order,
    )


@router.post("/trigger")
async def trigger_scan(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """手动触发扫描"""
    from fastapi.responses import JSONResponse

    from backend.workers.scan_worker import ScanWorker

    try:
        worker = ScanWorker()
        result = await worker.get_scan_candidates()
        candidates = result["candidates"]

        if not candidates:
            total_active = result["total_active"]
            if total_active == 0:
                message = "当前无已安装的仓库，请确保 GitHub App 已安装到目标仓库"
            else:
                cooldown_hours = result["cooldown_hours"]
                message = (
                    f"所有 {total_active} 个仓库均在冷却期内"
                    f"（{cooldown_hours} 小时），请稍后重试"
                )
            return JSONResponse(
                {"success": False, "message": message},
                status_code=400,
            )

        triggered = []
        for repo_name in candidates[:5]:  # 最多手动触发 5 个
            try:
                scan_id = await worker.create_scan_record(
                    repo_name=repo_name,
                    trigger_type="manual",
                    triggered_by=f"webui:{user.get('user_id', '')}",
                )
                create_registered_background_task(
                    worker.process_scan(scan_id), "scan_manual_webui"
                )
                triggered.append({"repo": repo_name, "scan_id": scan_id})
            except Exception:
                logger.exception("触发仓库扫描失败: {}", repo_name)

        return JSONResponse(
            {
                "success": True,
                "message": f"已触发 {len(triggered)} 个仓库扫描",
                "scans": triggered,
            }
        )

    except Exception:
        logger.exception("获取扫描候选仓库失败")
        return JSONResponse(
            {"success": False, "message": "触发扫描失败，请稍后重试"},
            status_code=500,
        )
