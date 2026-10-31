"""Typed, session-aware bootstrap for the React application."""

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import __version__
from backend.api.v1.deps import require_api_auth
from backend.core.config import get_settings
from backend.core.time_service import get_time_service
from backend.models.database import WebUIConfig
from backend.services.config_metadata import PERSONAL_DEFAULTS, PERSONAL_OPTIONS
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME
from backend.webui.deps import generate_csrf_token, get_db, require_csrf_header
from backend.webui.routes.auth import MFA_PENDING_COOKIE_NAME

router = APIRouter(prefix="/webui", tags=["WebUI"])


class WebUIUser(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    avatar_url: str | None


class WebUIPreferences(BaseModel):
    language: Literal["zh-CN", "en"]
    theme: Literal["light", "dark", "system"]
    items_per_page: int


class WebUIPermissions(BaseModel):
    admin: bool
    super_admin: bool
    manage_settings: bool


class WebUIFeatures(BaseModel):
    payment_enabled: bool
    agent_enabled: bool
    github_app_configured: bool


class WebUILinks(BaseModel):
    login: str = "/app/login"
    logout: str = "/api/v1/webui/logout"
    mfa: str = "/app/security"


class WebUIBootstrap(BaseModel):
    user: WebUIUser
    preferences: WebUIPreferences
    permissions: WebUIPermissions
    features: WebUIFeatures
    csrf: str
    version: str
    timezone: str
    links: WebUILinks


@router.get("/bootstrap", response_model=WebUIBootstrap)
async def bootstrap(
    response: Response,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
) -> WebUIBootstrap:
    """Return only the current user's shell state, never configuration secrets."""
    config = (
        await db.execute(
            select(WebUIConfig).where(WebUIConfig.user_id == int(user["user_id"]))
        )
    ).scalar_one_or_none()
    preferences = {
        key: (
            value
            if (value := getattr(config, key, None)) in PERSONAL_OPTIONS[key]
            else default
        )
        for key, default in PERSONAL_DEFAULTS.items()
    }
    settings = get_settings()
    role = user["role"]
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie, Authorization"
    return WebUIBootstrap(
        user=WebUIUser(
            id=int(user["user_id"]),
            username=user["sub"],
            display_name=user["sub"],
            role=role,
            avatar_url=user.get("avatar_url"),
        ),
        preferences=WebUIPreferences(**preferences),
        permissions=WebUIPermissions(
            admin=role in {"admin", "super_admin"},
            super_admin=role == "super_admin",
            manage_settings=role == "super_admin",
        ),
        features=WebUIFeatures(
            payment_enabled=settings.payment_enabled,
            agent_enabled=settings.agent_team_enabled,
            github_app_configured=bool(settings.github_app_id),
        ),
        csrf=generate_csrf_token(),
        version=__version__,
        timezone=get_time_service().resolved_timezone,
        links=WebUILinks(),
    )


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf_header)])
async def logout() -> Response:
    """Clear both cookies even when the access session has already expired."""
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(WEBUI_TOKEN_COOKIE_NAME, secure=True, httponly=True)
    response.delete_cookie(MFA_PENDING_COOKIE_NAME, secure=True, httponly=True)
    return response
