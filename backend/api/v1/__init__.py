"""API v1 路由"""

from fastapi import APIRouter, Request

from backend.api.v1 import (
    activity,
    agent,
    agent_management,
    announcements,
    auth,
    auth_webui,
    billing,
    billing_webui,
    community,
    config,
    config_center,
    console_data,
    dashboard,
    events,
    github_app,
    issues,
    logs,
    notifications,
    personal_security,
    public,
    queue,
    repos,
    reviews,
    scans,
    security_admin,
    settings,
    settings_webui,
    setup,
    user_config,
    users,
    version,
    webui,
)
from backend.api.v1.deps import limiter

api_v1_router = APIRouter()

# 免认证模块
api_v1_router.include_router(setup.router)
api_v1_router.include_router(public.router)

# 需认证模块
api_v1_router.include_router(auth.router)
api_v1_router.include_router(auth_webui.router)
api_v1_router.include_router(announcements.router)
api_v1_router.include_router(dashboard.router)
api_v1_router.include_router(reviews.router)
api_v1_router.include_router(issues.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(repos.router)
api_v1_router.include_router(config.router)
api_v1_router.include_router(config_center.router)
api_v1_router.include_router(console_data.router)
api_v1_router.include_router(community.router)
api_v1_router.include_router(logs.router)
api_v1_router.include_router(queue.router)
api_v1_router.include_router(scans.router)
api_v1_router.include_router(security_admin.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(settings_webui.router)
api_v1_router.include_router(user_config.router)
api_v1_router.include_router(events.router)
api_v1_router.include_router(billing.router)
api_v1_router.include_router(webui.router)
api_v1_router.include_router(version.router)
api_v1_router.include_router(github_app.router)
api_v1_router.include_router(activity.router)
api_v1_router.include_router(agent.router)
api_v1_router.include_router(agent_management.router)
api_v1_router.include_router(personal_security.router)
api_v1_router.include_router(notifications.router)
api_v1_router.include_router(billing_webui.router)


@api_v1_router.get("/health", tags=["Health"])
@limiter.limit("10/second")
async def api_health(request: Request):
    """API v1 健康检查"""
    return {"status": "ok", "version": "v1"}
