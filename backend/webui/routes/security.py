"""Super-admin security management routes."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser
from backend.services.security_admin_service import (
    delete_user_passkey,
    delete_user_passkeys,
    get_recent_security_events,
    get_user_passkeys,
    get_user_security_summaries,
    get_user_security_summary,
    is_global_mfa_required,
    reset_user_mfa,
    reset_user_totp,
    set_global_mfa_required,
    set_user_mfa_required,
)
from backend.services.security_audit_service import record_security_event
from backend.services.mfa_notification_service import notify_mfa_event
from backend.webui.deps import (
    error_page,
    get_csrf_serializer,
    get_db,
    get_templates,
    get_user_preferences,
    render_template,
    require_csrf,
    require_super_admin,
    toast_redirect,
)

router = APIRouter(prefix="/security", tags=["WebUI Security"])
templates = get_templates()


@router.get("/")
async def security_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Super-admin security management dashboard."""
    summaries = await get_user_security_summaries(db)
    recent_events = await get_recent_security_events(db, limit=30)
    global_mfa_required = await is_global_mfa_required(db)
    return render_template(
        "security.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="security",
        summaries=summaries,
        recent_events=recent_events,
        global_mfa_required=global_mfa_required,
    )


@router.post("/global-mfa/enable")
async def enable_global_mfa_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Require all users to enroll at least one MFA method."""
    await set_global_mfa_required(db, True)
    await record_security_event(
        db,
        "super_admin_enable_global_mfa",
        "success",
        actor_user_id=int(user["user_id"]),
        request=request,
    )
    await db.commit()
    return toast_redirect("/security/", "toast.security_global_mfa_enabled")


@router.post("/global-mfa/disable")
async def disable_global_mfa_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Disable global MFA enrollment requirement."""
    await set_global_mfa_required(db, False)
    await record_security_event(
        db,
        "super_admin_disable_global_mfa",
        "success",
        actor_user_id=int(user["user_id"]),
        request=request,
    )
    await db.commit()
    return toast_redirect("/security/", "toast.security_global_mfa_disabled")


@router.get("/users/{target_user_id}")
async def security_user_detail(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Security detail page for one user."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return error_page(request, 404, "用户不存在", user=user, user_prefs=user_prefs)
    summary = await get_user_security_summary(db, target_user)
    passkeys = await get_user_passkeys(db, target_user_id)
    events = await get_recent_security_events(db, target_user_id, limit=50)
    return render_template(
        "security_user_detail.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="security",
        target_user=target_user,
        summary=summary,
        passkeys=passkeys,
        events=events,
    )


@router.post("/users/{target_user_id}/totp/reset")
async def reset_totp_route(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Reset target user's TOTP and recovery codes."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    await reset_user_totp(db, target_user)
    await record_security_event(
        db,
        "super_admin_reset_totp",
        "success",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "totp_reset_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_totp_reset"
    )


@router.post("/users/{target_user_id}/mfa/require")
async def require_mfa_route(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Require target user to enroll at least one MFA method."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    await set_user_mfa_required(db, target_user, True)
    await record_security_event(
        db,
        "super_admin_require_mfa",
        "success",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "mfa_required_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_mfa_required"
    )


@router.post("/users/{target_user_id}/mfa/unrequire")
async def unrequire_mfa_route(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Remove forced MFA enrollment requirement for target user."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    await set_user_mfa_required(db, target_user, False)
    await record_security_event(
        db,
        "super_admin_unrequire_mfa",
        "success",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "mfa_unrequired_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_mfa_unrequired"
    )


@router.post("/users/{target_user_id}/passkeys/delete-all")
async def delete_all_passkeys_route(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Delete all target user's passkeys."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    deleted_count = await delete_user_passkeys(db, target_user_id)
    await record_security_event(
        db,
        "super_admin_delete_passkeys",
        "success",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
        detail={"deleted_count": deleted_count},
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "passkey_deleted_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_passkeys_deleted"
    )


@router.post("/users/{target_user_id}/passkeys/{credential_id}/delete")
async def delete_passkey_route(
    request: Request,
    target_user_id: int,
    credential_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Delete one target user's passkey."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    deleted_count = await delete_user_passkey(db, target_user_id, credential_id)
    await record_security_event(
        db,
        "super_admin_delete_passkey",
        "success" if deleted_count else "not_found",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
        detail={"credential_db_id": credential_id},
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "passkey_deleted_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_passkey_deleted"
    )


@router.post("/users/{target_user_id}/mfa/reset")
async def reset_mfa_route(
    request: Request,
    target_user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """Reset all MFA methods for a target user."""
    target_user = await _get_target_user(db, target_user_id)
    if not target_user:
        return toast_redirect("/security/", "toast.user_not_found", "error")
    await reset_user_mfa(db, target_user)
    await record_security_event(
        db,
        "super_admin_reset_mfa",
        "success",
        actor_user_id=int(user["user_id"]),
        target_user_id=target_user_id,
        request=request,
    )
    await db.commit()
    await notify_mfa_event(db, target_user_id, "mfa_reset_by_admin")
    return toast_redirect(
        f"/security/users/{target_user_id}", "toast.security_mfa_reset"
    )


async def _get_target_user(
    db: AsyncSession, target_user_id: int
) -> TelegramUser | None:
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == target_user_id)
    )
    return result.scalar_one_or_none()
