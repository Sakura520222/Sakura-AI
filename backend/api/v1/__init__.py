"""API v1 路由"""

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from backend.api.v1.deps import limiter
from backend.api.v1 import (
    setup,
    auth,
    dashboard,
    reviews,
    issues,
    users,
    repos,
    config,
    logs,
    queue,
    scans,
    settings,
    user_config,
    events,
    billing,
)

api_v1_router = APIRouter()

# 免认证模块
api_v1_router.include_router(setup.router)

# 需认证模块
api_v1_router.include_router(auth.router)
api_v1_router.include_router(dashboard.router)
api_v1_router.include_router(reviews.router)
api_v1_router.include_router(issues.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(repos.router)
api_v1_router.include_router(config.router)
api_v1_router.include_router(logs.router)
api_v1_router.include_router(queue.router)
api_v1_router.include_router(scans.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(user_config.router)
api_v1_router.include_router(events.router)
api_v1_router.include_router(billing.router)


@api_v1_router.get("/health", tags=["Health"])
@limiter.limit("10/second")
async def api_health(request: Request):
    """API v1 健康检查"""
    now = datetime.now(timezone.utc)
    started_at: datetime | None = getattr(request.app.state, "app_started_at", None)
    startup_duration: float | None = getattr(
        request.app.state, "startup_duration", None
    )

    result: dict = {"status": "ok", "version": "v1"}
    if started_at:
        result["started_at"] = started_at.isoformat()
        result["uptime_seconds"] = round((now - started_at).total_seconds(), 1)
    if startup_duration is not None:
        result["startup_duration_seconds"] = startup_duration
    return result
