"""Security administration JSON boundary using the existing audited services."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.core.time_service import format_rfc3339
from backend.models.telegram_models import TelegramUser
from backend.services import security_admin_service as security
from backend.services.security_audit_service import record_security_event
from backend.webui.deps import get_db, require_csrf_header

router = APIRouter(
    prefix="/admin/security",
    tags=["Security Administration"],
    route_class=PrivateAPIRoute,
)


def summary_view(summary):
    user = summary.user
    return {
        "id": user.id,
        "username": user.github_username,
        "role": user.role,
        "active": bool(user.is_active),
        "totp_enabled": bool(user.totp_enabled),
        "mfa_required": bool(user.mfa_required),
        "recovery_codes": summary.recovery_code_count,
        "passkeys": summary.passkey_count,
        "last_event_at": format_rfc3339(summary.last_security_event_at)
        if summary.last_security_event_at
        else None,
    }


def event_view(event):
    return {
        "id": event.id,
        "type": event.event_type,
        "result": event.event_result,
        "actor_id": event.actor_user_id,
        "user_id": event.target_user_id,
        "ip_address": event.ip_address,
        "created_at": format_rfc3339(event.created_at),
    }


@router.get("")
async def overview(
    user=Depends(require_api_super_admin), db: AsyncSession = Depends(get_db)
):
    return {
        "users": [
            summary_view(item)
            for item in await security.get_user_security_summaries(db)
        ],
        "events": [
            event_view(item)
            for item in await security.get_recent_security_events(db, limit=30)
        ],
        "global_mfa_required": await security.is_global_mfa_required(db),
    }


@router.get("/users/{user_id}")
async def detail(
    user_id: int,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(TelegramUser, user_id)
    if target is None:
        raise HTTPException(404, "user_not_found")
    return {
        "user": summary_view(await security.get_user_security_summary(db, target)),
        "passkeys": [
            {
                "id": key.id,
                "name": key.device_name,
                "backed_up": bool(key.backed_up),
                "created_at": format_rfc3339(key.created_at),
                "last_used_at": format_rfc3339(key.last_used_at)
                if key.last_used_at
                else None,
            }
            for key in await security.get_user_passkeys(db, user_id)
        ],
        "events": [
            event_view(item)
            for item in await security.get_recent_security_events(db, user_id, limit=50)
        ],
    }


class MfaRequirement(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    required: bool


@router.put("/global-mfa", dependencies=[Depends(require_csrf_header)])
async def global_mfa(
    body: MfaRequirement,
    request: Request,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    await security.set_global_mfa_required(db, body.required)
    await record_security_event(
        db,
        "super_admin_enable_global_mfa"
        if body.required
        else "super_admin_disable_global_mfa",
        "success",
        actor_user_id=user["user_id"],
        request=request,
    )
    await db.commit()
    return {"required": body.required}


@router.post("/users/{user_id}/{action}", dependencies=[Depends(require_csrf_header)])
async def user_action(
    user_id: int,
    action: Literal[
        "reset-totp", "reset-mfa", "require-mfa", "unrequire-mfa", "delete-passkeys"
    ],
    request: Request,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(TelegramUser, user_id)
    if target is None:
        raise HTTPException(404, "user_not_found")
    if action == "reset-totp":
        await security.reset_user_totp(db, target)
    elif action == "reset-mfa":
        await security.reset_user_mfa(db, target)
    elif action == "delete-passkeys":
        await security.delete_user_passkeys(db, user_id)
    else:
        await security.set_user_mfa_required(db, target, action == "require-mfa")
    await record_security_event(
        db,
        "super_admin_" + action.replace("-", "_"),
        "success",
        actor_user_id=user["user_id"],
        target_user_id=user_id,
        request=request,
    )
    await db.commit()
    return {"success": True}


@router.delete(
    "/users/{user_id}/passkeys/{credential_id}",
    dependencies=[Depends(require_csrf_header)],
)
async def delete_key(
    user_id: int,
    credential_id: int,
    request: Request,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    count = await security.delete_user_passkey(db, user_id, credential_id)
    await record_security_event(
        db,
        "super_admin_delete_passkey",
        "success" if count else "not_found",
        actor_user_id=user["user_id"],
        target_user_id=user_id,
        request=request,
        detail={"credential_db_id": credential_id},
    )
    await db.commit()
    return {"deleted": count}
