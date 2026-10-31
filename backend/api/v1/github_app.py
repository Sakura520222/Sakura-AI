"""JSON authorization center backed by the existing user-scoped GitHub service."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.services.github_user_authorization_service import GitHubUserAuthorization
from backend.webui.deps import get_db
from backend.webui.routes.github_app import _authorization_state

router = APIRouter(prefix="/github-app", tags=["GitHub App"], route_class=PrivateAPIRoute)


class GitHubAuthorizationResponse(BaseModel):
    authorization: GitHubUserAuthorization
    authorization_url: str | None
    install_url: str | None
    repository_count: int


@router.get("", response_model=GitHubAuthorizationResponse)
async def authorization_center(
    user: dict = Depends(require_api_auth), db: AsyncSession = Depends(get_db)
):
    try:
        authorization, configured, install_url = await _authorization_state(db, user)
    except Exception:
        logger.exception("GitHub authorization API unavailable")
        raise HTTPException(503, "GitHub authorization is temporarily unavailable", headers={"Cache-Control": "no-store"}) from None
    return GitHubAuthorizationResponse(
        authorization=authorization,
        authorization_url=(
            "/star-aid/auth/start?intent=github_app&return_to=/app/github-app"
            if configured
            else None
        ),
        install_url=install_url,
        repository_count=authorization.repository_count,
    )
