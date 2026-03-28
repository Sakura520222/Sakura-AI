"""WebUI 路由"""

from fastapi import APIRouter
from backend.webui.routes import auth, dashboard, pr, users, repos, logs, settings, config, queue

webui_router = APIRouter(prefix="/webui")

webui_router.include_router(auth.router)
webui_router.include_router(dashboard.router)
webui_router.include_router(pr.router)
webui_router.include_router(users.router)
webui_router.include_router(repos.router)
webui_router.include_router(logs.router)
webui_router.include_router(settings.router)
webui_router.include_router(config.router)
webui_router.include_router(queue.router)
