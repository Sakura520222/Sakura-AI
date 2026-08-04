"""WebUI 路由"""

from fastapi import APIRouter, Depends

from backend.webui.deps import mark_webui_request
from backend.webui.routes import (
    action_logs,
    activity_observability,
    agent_skills,
    agent_team,
    assetlinks,
    auth,
    billing,
    config,
    dashboard,
    issues,
    legal,
    logs,
    pr,
    queue,
    repos,
    sakura_memory,
    scans,
    security,
    settings,
    sse,
    star_aid,
    system_config,
    users,
    vector_db,
)

# WebUI routes are mounted at root (no prefix) so the dashboard is served at /.
# The router prefix is kept as an explicit empty string to document this intent.
# Every request hitting a WebUI route carries ``request.state.is_webui = True``
# via the ``mark_webui_request`` dependency, so error handlers can distinguish
# WebUI pages from API/setup/docs routes without exclusion lists.
webui_router = APIRouter(prefix="", dependencies=[Depends(mark_webui_request)])

webui_router.include_router(auth.router)
webui_router.include_router(dashboard.router)
webui_router.include_router(pr.router)
webui_router.include_router(users.router)
webui_router.include_router(repos.router)
webui_router.include_router(logs.router)
webui_router.include_router(settings.router)
webui_router.include_router(config.router)
webui_router.include_router(queue.router)
webui_router.include_router(action_logs.router)
webui_router.include_router(issues.router)
webui_router.include_router(sse.router)
webui_router.include_router(scans.router)
webui_router.include_router(billing.router)
webui_router.include_router(security.router)
webui_router.include_router(agent_team.router)
webui_router.include_router(assetlinks.router)
webui_router.include_router(agent_skills.router)
webui_router.include_router(sakura_memory.router)
webui_router.include_router(star_aid.router)
webui_router.include_router(system_config.router)
webui_router.include_router(vector_db.router)
webui_router.include_router(legal.router)
webui_router.include_router(activity_observability.router)
