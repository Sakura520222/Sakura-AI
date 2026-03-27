"""WebUI 路由"""

from fastapi import APIRouter
from backend.webui.routes import auth, dashboard, pr

webui_router = APIRouter(prefix="/webui")

webui_router.include_router(auth.router)
webui_router.include_router(dashboard.router)
webui_router.include_router(pr.router)
