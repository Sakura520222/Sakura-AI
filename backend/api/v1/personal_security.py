"""Private JSON operations for the authenticated user's security settings."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import get_api_current_user, limiter, require_api_auth
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.api.v1.responses import success_response
from backend.core.config import get_settings
from backend.core.time_service import format_rfc3339, now_utc
from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential
from backend.services import two_factor_service as totp
from backend.services.telegram_binding_service import create_telegram_binding_token
from backend.webui.auth import queue_webui_token_renewal
from backend.webui.deps import (
    generate_csrf_token,
    get_db,
    require_csrf_header,
    user_requires_mfa_enrollment,
)
from backend.webui.routes import settings as legacy

router = APIRouter(
    prefix="/webui/security", tags=["Personal Security"], route_class=PrivateAPIRoute
)


async def security_user(request: Request) -> dict:
    # Enrollment is the sole exception to the normal MFA enrollment gate. The
    # shared authenticator still rejects pending-MFA/expired/disabled sessions.
    user = await get_api_current_user(request)
    if getattr(request.state, "api_cookie_auth", False):
        queue_webui_token_renewal(request, user)
    return user


async def account(user, db):
    db_user = (
        await db.execute(
            select(TelegramUser).where(
                TelegramUser.id == int(user["user_id"]),
                TelegramUser.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if db_user is None:
        raise HTTPException(401, "session_expired")
    return db_user


class VerifyCode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=128, repr=False)


class RegisterPasskey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    challenge_id: str
    credential: dict[str, Any]
    device_name: str = Field(default="Passkey", max_length=100)


@router.get("")
async def status(
    user: dict = Depends(security_user), db: AsyncSession = Depends(get_db)
):
    db_user = await account(user, db)
    keys = await legacy._get_user_passkeys(db, db_user.id)
    settings = get_settings()
    return success_response(
        data={
            "totp_enabled": bool(db_user.totp_enabled),
            "recovery_code_count": await totp.count_unused_recovery_codes(
                db, db_user.id
            ),
            "mfa_required": await user_requires_mfa_enrollment(db_user.id, db),
            "two_factor_available": settings.two_factor_enabled,
            "passkeys_available": settings.passkeys_enabled,
            "passkeys": [
                {
                    "id": key.id,
                    "device_name": key.device_name,
                    "backed_up": bool(key.backed_up),
                    "created_at": format_rfc3339(key.created_at),
                    "last_used_at": format_rfc3339(key.last_used_at)
                    if key.last_used_at
                    else None,
                }
                for key in keys
            ],
            "csrf": generate_csrf_token(),
        }
    )


@router.post("/totp/setup", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_setup_rate_limit)
async def setup_totp(
    request: Request,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
):
    if not get_settings().two_factor_enabled:
        raise HTTPException(403, "totp_unavailable")
    db_user = await account(user, db)
    setup = totp.create_totp_setup(db_user)
    await legacy._save_totp_setup_secret(db_user.id, setup.secret)
    return success_response(data=asdict(setup))


@router.post("/totp/enable", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_setup_rate_limit)
async def enable_totp(
    request: Request,
    body: VerifyCode,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
):
    if not get_settings().two_factor_enabled:
        raise HTTPException(403, "totp_unavailable")
    db_user = await account(user, db)
    secret = await legacy._pop_totp_setup_secret(db_user.id)
    if not secret:
        raise HTTPException(400, "setup_expired")
    step = totp.verify_totp_secret(secret, body.code)
    if step is None:
        raise HTTPException(400, "invalid_code")
    db_user.totp_enabled = True
    db_user.totp_secret_encrypted = totp.encrypt_totp_secret(secret)
    db_user.totp_enabled_at = now_utc()
    db_user.totp_last_used_step = step
    db_user.mfa_required = False
    codes = await totp.replace_recovery_codes(db, db_user.id)
    await db.commit()
    return success_response(data={"recovery_codes": codes})


async def verified_totp(user, db, code, *, allow_recovery=False):
    db_user = await account(user, db)
    if not db_user.totp_enabled:
        raise HTTPException(400, "totp_not_enabled")
    try:
        step = totp.verify_user_totp(db_user, code)
    except totp.TwoFactorError:
        step = None
    if step is not None:
        db_user.totp_last_used_step = step
        return db_user
    if allow_recovery and await totp.consume_recovery_code(db, db_user.id, code):
        return db_user
    await db.rollback()
    raise HTTPException(400, "invalid_code")


@router.post("/totp/disable", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_verify_rate_limit)
async def disable_totp(
    request: Request,
    body: VerifyCode,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
):
    db_user = await verified_totp(user, db, body.code, allow_recovery=True)
    await totp.disable_totp(db, db_user)
    await db.commit()
    return success_response()


@router.post("/recovery-codes", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_verify_rate_limit)
async def recovery_codes(
    request: Request,
    body: VerifyCode,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
):
    db_user = await verified_totp(user, db, body.code)
    codes = await totp.replace_recovery_codes(db, db_user.id)
    await db.commit()
    return success_response(data={"recovery_codes": codes})


@router.post("/passkeys/options")
async def passkey_options(
    request: Request,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await legacy.passkey_register_options(
        request, db=db, user=user, csrf_token=csrf
    )


@router.post("/passkeys/verify")
async def passkey_verify(
    request: Request,
    body: RegisterPasskey,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await legacy.passkey_register_verify(
        request, db=db, user=user, csrf_token=csrf, body=body.model_dump()
    )


@router.delete("/passkeys/{credential_id}", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_verify_rate_limit)
async def delete_passkey(
    request: Request,
    credential_id: int,
    user: dict = Depends(security_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(UserWebAuthnCredential).where(
            UserWebAuthnCredential.id == credential_id,
            UserWebAuthnCredential.user_id == int(user["user_id"]),
        )
    )
    await db.commit()
    return success_response()


@router.post("/telegram/bind", dependencies=[Depends(require_csrf_header)])
@limiter.limit(lambda: get_settings().two_factor_setup_rate_limit)
async def telegram_binding(request: Request, user: dict = Depends(require_api_auth)):
    binding = await create_telegram_binding_token(int(user["user_id"]))
    return success_response(data=jsonable_encoder(asdict(binding)))
