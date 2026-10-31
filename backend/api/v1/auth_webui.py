"""Cookie-only browser login boundary reusing OAuth/MFA/Passkey authorities."""

import json
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1 import auth as token_auth
from backend.api.v1.deps import get_api_current_user, limiter
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.api.v1.schemas import MfaVerifyRequest
from backend.core.config import get_settings
from backend.core.time_service import get_time_service
from backend.models.telegram_models import TelegramUser
from backend.webui.auth import (
    WEBUI_TOKEN_COOKIE_NAME,
    decode_access_token,
    is_mfa_pending_payload,
    set_webui_token_cookie,
)
from backend.webui.deps import (
    generate_csrf_token,
    get_db,
    require_csrf_header,
    user_requires_mfa_enrollment,
)
from backend.webui.routes import auth as legacy

router = APIRouter(
    prefix="/webui/auth", tags=["Browser Authentication"], route_class=PrivateAPIRoute
)


class BrowserAuthState(BaseModel):
    csrf: str
    timezone: str
    github_available: bool
    passkeys_available: bool
    authenticated: bool = False
    enrollment_required: bool = False
    mfa_pending: bool = False
    totp_enabled: bool = False
    username: str | None = None


class BrowserMfaCode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=128, repr=False)


class BrowserPasskey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    challenge_id: str
    credential: dict[str, Any]


@router.get("/state", response_model=BrowserAuthState)
async def auth_state(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    state = BrowserAuthState(
        csrf=generate_csrf_token(),
        timezone=get_time_service().resolved_timezone,
        github_available=bool(
            settings.github_oauth_client_id and settings.github_oauth_redirect_uri
        ),
        passkeys_available=settings.passkeys_enabled,
    )
    if request.cookies.get(WEBUI_TOKEN_COOKIE_NAME):
        try:
            user = await get_api_current_user(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
        else:
            state.authenticated = True
            state.enrollment_required = await user_requires_mfa_enrollment(
                int(user["user_id"]), db
            )
            return state
    token = request.cookies.get(legacy.MFA_PENDING_COOKIE_NAME)
    pending = decode_access_token(token) if token else None
    if is_mfa_pending_payload(pending):
        user = (
            await db.execute(
                select(TelegramUser).where(
                    TelegramUser.id == pending.get("user_id"),
                    TelegramUser.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user and await legacy.user_has_any_mfa_method(db, user):
            state.mfa_pending = True
            state.totp_enabled = bool(user.totp_enabled)
            state.username = user.github_username
    return state


@router.get("/github")
@limiter.limit("10/minute")
async def github_login(request: Request):
    settings = get_settings()
    if not settings.github_oauth_client_id or not settings.github_oauth_redirect_uri:
        raise HTTPException(503, "github_login_unavailable")
    state = secrets.token_urlsafe(32)
    await legacy._save_oauth_state(state, "/app/")
    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    return RedirectResponse(f"{settings.github_oauth_auth_url}?{urlencode(params)}")


@router.post("/mfa/totp", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_verify_rate_limit)
async def verify_totp(request: Request, body: BrowserMfaCode):
    token = request.cookies.get(legacy.MFA_PENDING_COOKIE_NAME)
    if not token or not is_mfa_pending_payload(decode_access_token(token)):
        raise HTTPException(401, "mfa_session_expired")
    # Reuse the same transaction, replay protection and lockout as API MFA. The
    # browser boundary applies the configured WebUI rate above, and never exposes
    # the resulting bearer token to JavaScript.
    verify = getattr(
        token_auth.verify_two_factor, "__wrapped__", token_auth.verify_two_factor
    )
    result = await verify(request, MfaVerifyRequest(mfa_token=token, code=body.code))
    payload = json.loads(result.body)
    if result.status_code != 200 or not payload.get("success"):
        return result
    access_token = payload.get("data", {}).get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(502, "authentication_unavailable")
    response = JSONResponse({"success": True, "data": {"redirect": "/app/"}})
    set_webui_token_cookie(response, access_token)
    response.delete_cookie(legacy.MFA_PENDING_COOKIE_NAME, secure=True, httponly=True)
    return response


@router.post("/passkeys/options", dependencies=[Depends(require_csrf_header)])
async def passkey_options(request: Request):
    return await legacy.passkey_discover_options(request)


@router.post("/passkeys/verify", dependencies=[Depends(require_csrf_header)])
async def passkey_verify(request: Request, body: BrowserPasskey):
    return await legacy.passkey_verify_discover(request, body=body.model_dump())


@router.post("/mfa/passkeys/options")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def mfa_passkey_options(
    request: Request, csrf: str = Depends(require_csrf_header)
):
    return await legacy.two_factor_passkey_options(request, csrf_token=csrf)


@router.post("/mfa/passkeys/verify")
async def mfa_passkey_verify(
    request: Request, body: BrowserPasskey, csrf: str = Depends(require_csrf_header)
):
    return await legacy.two_factor_passkey_verify(
        request, csrf_token=csrf, body=body.model_dump()
    )
