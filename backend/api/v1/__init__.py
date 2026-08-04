"""API v1 路由"""

from fastapi import APIRouter, Request

from backend.api.v1 import (
    auth,
    billing,
    config,
    dashboard,
    events,
    issues,
    logs,
    queue,
    repos,
    reviews,
    scans,
    settings,
    setup,
    user_config,
    users,
)
from backend.api.v1.deps import limiter

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
    return {"status": "ok", "version": "v1"}
