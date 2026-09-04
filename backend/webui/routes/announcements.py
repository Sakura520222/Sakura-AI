"""Announcement center WebUI pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.announcement_service import (
    announcement_to_dict,
    delivery_stats,
    get_announcement,
    list_announcements,
    mark_read,
)
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    render_template,
    require_auth,
    require_csrf,
    require_super_admin,
    toast_redirect,
)
from backend.webui.i18n import resolve_language

router = APIRouter(prefix="/announcements", tags=["WebUI Announcements"])


def _publish_action(action: str | None) -> bool:
    """Return whether an admin form requested an immediate send round."""
    return str(action or "publish").strip().lower() in {
        "publish",
        "send",
        "save_and_publish",
        "save-and-publish",
    }


@router.get("")
@router.get("/")
async def announcements_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    rows = await list_announcements(db, user_id=int(user["user_id"]))
    items = [announcement_to_dict(item, read=read) for item, read in rows]
    return render_template(
        "announcements.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="announcements",
        csrf_token=get_csrf_serializer().dumps({}),
        announcements=items,
    )


@router.post("/read-all")
async def announcements_mark_all_read(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    from backend.services.announcement_service import mark_all_read

    return {"marked": await mark_all_read(db, int(user["user_id"]))}


@router.post("/{announcement_id}/read")
async def announcements_mark_read(
    announcement_id: int,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    if not await mark_read(db, int(user["user_id"]), announcement_id):
        raise HTTPException(status_code=404, detail="公告不存在或未发布")
    return {"ok": True}


@router.get("/admin")
async def announcement_admin_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    rows = await list_announcements(db, include_drafts=True)
    items = []
    for item, read in rows:
        stats = await delivery_stats(db, item.id)
        items.append(
            announcement_to_dict(item, read=read, delivery_stats=stats)
        )
    return render_template(
        "announcements_admin.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="announcements",
        csrf_token=get_csrf_serializer().dumps({}),
        announcements=items,
    )


@router.post("/admin/create")
async def announcement_admin_create(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    announcement_type: str = Form("general"),
    action: str = Form("publish"),
    user: dict = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    from backend.services.announcement_service import create_announcement

    try:
        await create_announcement(
            db,
            title=title,
            content=content,
            announcement_type=announcement_type,
            created_by=int(user["user_id"]),
            publish=_publish_action(action),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return toast_redirect(
        "/announcements/admin",
        "announcements.published" if _publish_action(action) else "announcements.created",
        lang=resolve_language(request),
    )


@router.post("/admin/{announcement_id}/edit")
async def announcement_admin_edit(
    announcement_id: int,
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    announcement_type: str = Form("general"),
    action: str = Form("publish"),
    user: dict = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    del user
    from backend.services.announcement_service import update_announcement

    try:
        await update_announcement(
            db,
            announcement_id,
            title=title,
            content=content,
            announcement_type=announcement_type,
            publish=_publish_action(action),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return toast_redirect(
        "/announcements/admin",
        "announcements.published" if _publish_action(action) else "announcements.updated",
        lang=resolve_language(request),
    )


@router.post("/admin/{announcement_id}/publish")
async def announcement_admin_publish(
    announcement_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    del user
    from backend.services.announcement_service import publish_announcement

    try:
        await publish_announcement(db, announcement_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return toast_redirect(
        "/announcements/admin",
        "announcements.published",
        lang=resolve_language(request),
    )


@router.post("/admin/{announcement_id}/withdraw")
async def announcement_admin_withdraw(
    announcement_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    del user
    from backend.services.announcement_service import withdraw_announcement

    try:
        await withdraw_announcement(db, announcement_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return toast_redirect(
        "/announcements/admin",
        "announcements.withdrawn",
        lang=resolve_language(request),
    )


@router.post("/admin/{announcement_id}/delete")
async def announcement_admin_delete(
    announcement_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf),
):
    del user
    from backend.services.announcement_service import delete_announcement

    try:
        deleted = await delete_announcement(db, announcement_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="公告不存在")
    return toast_redirect(
        "/announcements/admin",
        "announcements.deleted",
        lang=resolve_language(request),
    )


@router.get("/{announcement_id}")
async def announcement_detail_page(
    announcement_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    announcement = await get_announcement(db, announcement_id)
    if announcement is None or announcement.status != "published":
        raise HTTPException(status_code=404, detail="公告不存在")
    await mark_read(db, int(user["user_id"]), announcement_id)
    return render_template(
        "announcement_detail.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="announcements",
        csrf_token=get_csrf_serializer().dumps({}),
        announcement=announcement_to_dict(announcement, read=True),
    )


__all__ = ["router"]
