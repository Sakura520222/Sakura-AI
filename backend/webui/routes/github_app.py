"""User-facing GitHub App authorization center."""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, Request, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.services.github_user_authorization_service import (
    GitHubUserAuthorization,
    GitHubUserAuthorizationService,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    render_template,
    require_auth,
)

router = APIRouter(prefix="/github-app", tags=["WebUI GitHub App"])
_app_slug: str | None = None
_app_slug_client_id: str | None = None
_GITHUB_APP_SLUG_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _install_url_for_slug(slug: str) -> str | None:
    """Build an official install URL, rejecting unsafe configured slugs."""
    if not _GITHUB_APP_SLUG_RE.fullmatch(slug):
        logger.warning("Configured GitHub App slug is invalid")
        return None
    return f"https://github.com/apps/{slug}/installations/new"


async def _get_install_url() -> str | None:
    """Resolve the install URL for the configured user-authorization App."""
    global _app_slug, _app_slug_client_id

    settings = get_settings()
    client_id = settings.star_aid_github_app_client_id
    configured_slug = settings.star_aid_github_app_slug
    if configured_slug:
        return _install_url_for_slug(configured_slug)

    if _app_slug and _app_slug_client_id == client_id:
        return f"https://github.com/apps/{_app_slug}/installations/new"

    from backend.core.github_app import GitHubAppClient

    try:
        slug, main_app_client_id = await asyncio.to_thread(
            GitHubAppClient().get_app_identity
        )
    except Exception as exc:
        logger.warning("GitHub App install URL lookup failed: {}", type(exc).__name__)
        return None
    if (
        not slug
        or slug == "unknown-bot"
        or not main_app_client_id
        or main_app_client_id != client_id
    ):
        logger.warning(
            "A separate user-authorization GitHub App requires its slug to be configured"
        )
        return None
    slug = slug.removesuffix("[bot]")
    _app_slug = slug
    _app_slug_client_id = client_id
    return f"https://github.com/apps/{slug}/installations/new"


async def _authorization_state(
    db: AsyncSession, user: dict
) -> tuple[GitHubUserAuthorization, bool, str | None]:
    settings = get_settings()
    user_flow_configured = bool(
        settings.star_aid_github_app_client_id
        and settings.star_aid_github_app_client_secret
        and settings.star_aid_github_app_callback_url
    )
    authorization = await GitHubUserAuthorizationService().get_installations(
        db, int(user["user_id"]), expected_github_username=user.get("sub")
    )
    install_url = await _get_install_url()
    return authorization, user_flow_configured, install_url


def _fallback_state() -> tuple[GitHubUserAuthorization | None, bool, str | None]:
    settings = get_settings()
    return (
        None,
        bool(
            settings.star_aid_github_app_client_id
            and settings.star_aid_github_app_callback_url
        ),
        None,
    )


@router.get("/")
async def index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Show only installations accessible by the signed-in GitHub user."""
    try:
        authorization, user_flow_configured, install_url = await _authorization_state(
            db, user
        )
        error_code = None
    except Exception:
        logger.exception("GitHub App authorization center failed")
        authorization, user_flow_configured, install_url = _fallback_state()
        install_url = await _get_install_url()
        error_code = "request_failed"

    return render_template(
        "github_app.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="github_app",
        authorization=authorization,
        authorization_url=(
            "/star-aid/auth/start?intent=github_app&return_to=/github-app/"
            if user_flow_configured
            else None
        ),
        install_url=install_url,
        error_code=error_code,
    )


@router.get("/list-fragment")
async def list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
) -> Response:
    """Refresh user-scoped installations; no global installation cache is used."""
    try:
        authorization, user_flow_configured, install_url = await _authorization_state(
            db, user
        )
        error_code = None
    except Exception:
        logger.exception("GitHub App authorization fragment refresh failed")
        authorization, user_flow_configured, install_url = _fallback_state()
        install_url = await _get_install_url()
        error_code = "request_failed"

    return render_template(
        "components/github_app_list_fragment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        authorization=authorization,
        authorization_url=(
            "/star-aid/auth/start?intent=github_app&return_to=/github-app/"
            if user_flow_configured
            else None
        ),
        install_url=install_url,
        error_code=error_code,
    )
