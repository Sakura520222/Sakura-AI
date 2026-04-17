"""API v1 扫描管理端点"""

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.scan_models import RepoScan, ScanFinding
from backend.webui.deps import get_db, paginate

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.api.v1.responses import success_response, error_response, paginated_response
from backend.api.v1.deps import limiter

router = APIRouter(prefix="/scans", tags=["Scans"])


@router.get("")
async def list_scans(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    search: str = Query("", description="搜索关键词"),
    repo_name: str = Query("", description="按仓库过滤"),
    status: str = Query("", description="按状态过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """扫描列表"""
    query = select(RepoScan)
    count_query = select(func.count(RepoScan.id))

    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            RepoScan.repo_name.ilike(f"%{escaped}%", escape="\\"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if repo_name:
        query = query.where(RepoScan.repo_name == repo_name)
        count_query = count_query.where(RepoScan.repo_name == repo_name)
    if status:
        query = query.where(RepoScan.status == status)
        count_query = count_query.where(RepoScan.status == status)

    query = query.order_by(desc(RepoScan.created_at))

    scans, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = [
        {
            "id": s.id,
            "repo_name": s.repo_name,
            "repo_owner": s.repo_owner,
            "trigger_type": s.trigger_type,
            "status": s.status,
            "progress": s.progress,
            "total_findings": s.total_findings,
            "overall_health_score": s.overall_health_score,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        }
        for s in scans
    ]

    return paginated_response(items, total, page, total_pages, per_page)


@router.get("/stats")
async def scan_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """扫描统计"""
    status_counts = (
        await db.execute(
            select(RepoScan.status, func.count(RepoScan.id))
            .group_by(RepoScan.status)
        )
    ).all()

    by_status = {row[0]: row[1] for row in status_counts}

    avg_score_result = await db.execute(
        select(func.avg(RepoScan.overall_health_score))
        .where(RepoScan.status == "completed", RepoScan.overall_health_score.isnot(None))
    )
    avg_score = avg_score_result.scalar()

    return success_response(data={
        "total": sum(by_status.values()),
        "by_status": by_status,
        "avg_health_score": round(avg_score, 1) if avg_score else None,
    })


@router.get("/{scan_id}")
async def get_scan(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """扫描详情（含 findings）"""
    result = await db.execute(select(RepoScan).where(RepoScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        return error_response("扫描记录不存在", status_code=404)

    # 关联 findings
    findings_result = await db.execute(
        select(ScanFinding)
        .where(ScanFinding.scan_id == scan_id)
        .order_by(ScanFinding.severity, ScanFinding.created_at)
    )
    findings = findings_result.scalars().all()

    data = {
        "id": scan.id,
        "repo_name": scan.repo_name,
        "repo_owner": scan.repo_owner,
        "trigger_type": scan.trigger_type,
        "triggered_by": scan.triggered_by,
        "commit_sha": scan.commit_sha,
        "status": scan.status,
        "progress": scan.progress,
        "current_phase": scan.current_phase,
        "error_message": scan.error_message,
        "file_count": scan.file_count,
        "code_file_count": scan.code_file_count,
        "total_findings": scan.total_findings,
        "critical_count": scan.critical_count,
        "major_count": scan.major_count,
        "minor_count": scan.minor_count,
        "suggestion_count": scan.suggestion_count,
        "overall_health_score": scan.overall_health_score,
        "report_issue_number": scan.report_issue_number,
        "report_issue_url": scan.report_issue_url,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
        "started_at": scan.started_at.isoformat() if scan.started_at else None,
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        "findings": [
            {
                "id": f.id,
                "file_path": f.file_path,
                "line_start": f.line_start,
                "line_end": f.line_end,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "description": f.description,
                "suggestion": f.suggestion,
                "confidence": f.confidence,
            }
            for f in findings
        ],
    }

    return success_response(data=data)


@router.post("/trigger")
@limiter.limit("3/minute")
async def trigger_scan(
    request: Request,
    user: dict = Depends(require_api_super_admin),
):
    """手动触发扫描（超级管理员）"""
    import asyncio

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
            return error_response(message, status_code=400)

        triggered = []
        for repo_name in candidates[:5]:
            try:
                scan_id = await worker.create_scan_record(
                    repo_name=repo_name,
                    trigger_type="manual_api",
                    triggered_by=f"api:{user['sub']}",
                )
                asyncio.create_task(worker.process_scan(scan_id))
                triggered.append({"repo": repo_name, "scan_id": scan_id})
            except Exception as e:
                logger.error(f"触发扫描失败 ({repo_name}): {e}")

        return success_response(
            data={"triggered": triggered, "count": len(triggered)},
            message=f"已触发 {len(triggered)} 个仓库扫描",
        )
    except Exception as e:
        return error_response(str(e), status_code=500)


@router.post("/{scan_id}/retry")
async def retry_scan(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """重试失败的扫描"""
    import asyncio
    from loguru import logger

    from backend.workers.scan_worker import ScanWorker

    result = await db.execute(select(RepoScan).where(RepoScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        return error_response("扫描记录不存在", status_code=404)
    if scan.status != "failed":
        return error_response("只能重试失败的扫描", status_code=400)

    # 重置状态并重新执行
    scan.status = "pending"
    scan.error_message = None
    await db.commit()

    try:
        worker = ScanWorker()
        asyncio.create_task(worker.process_scan(scan_id))
        return success_response(message="扫描已重新触发")
    except Exception as e:
        logger.error(f"重试扫描失败 ({scan_id}): {e}")
        return error_response(str(e), status_code=500)


@router.post("/{scan_id}/cancel")
async def cancel_scan(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """取消正在进行的扫描"""
    result = await db.execute(select(RepoScan).where(RepoScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        return error_response("扫描记录不存在", status_code=404)

    active_statuses = ("pending", "indexing", "analyzing", "reporting")
    if scan.status not in active_statuses:
        return error_response("只能取消进行中的扫描", status_code=400)

    scan.status = "cancelled"
    scan.error_message = "用户手动取消"
    await db.commit()

    return success_response(message="扫描已取消")
