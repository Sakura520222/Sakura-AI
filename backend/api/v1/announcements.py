"""Announcement API: user read state and super-admin lifecycle controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.models.announcement_models import AnnouncementType
from backend.services.announcement_service import (
    announcement_to_dict,
    create_announcement,
    delete_announcement,
    delivery_stats_many,
    get_announcement,
    mark_all_read,
    mark_read,
    paginate_announcements,
    publish_announcement,
    schedule_announcement_broadcast,
    update_announcement,
    withdraw_announcement,
)
from backend.webui.deps import get_db

router = APIRouter(prefix="/announcements", tags=["Announcements"])


class AnnouncementCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    type: str = Field(default=AnnouncementType.GENERAL.value, max_length=50)
    # The admin UI uses this to make the primary action a one-step publish;
    # callers can leave it false to create a draft explicitly.
    publish: bool = False
    send: bool | None = None
    action: str | None = None


class AnnouncementUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1)
    type: str | None = Field(default=None, max_length=50)
    publish: bool = False
    send: bool | None = None
    action: str | None = None


def _request_publishes(body: AnnouncementCreateRequest | AnnouncementUpdateRequest) -> bool:
    """Accept both JSON booleans and UI-style action names."""
    if body.send is not None:
        return body.send
    if body.action is not None:
        return body.action.strip().lower() in {
            "publish",
            "send",
            "save_and_publish",
            "save-and-publish",
        }
    return body.publish


async def _user_announcements(
    db: AsyncSession,
    user_id: int,
    *,
    include_drafts: bool = False,
    page: int = 1,
    per_page: int = 100,
    unread_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    result = await paginate_announcements(
        db,
        user_id=user_id,
        include_drafts=include_drafts,
        page=page,
        per_page=per_page,
        unread_only=unread_only,
    )
    items = [
        announcement_to_dict(item, read=read) for item, read in result.items
    ]
    metadata = {
        "page": result.page,
        "per_page": result.per_page,
        "total": result.total,
        "total_pages": result.total_pages,
    }
    return items, metadata


@router.get("")
@router.get("/")
async def get_announcements(
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """List published announcements for the current internal user id."""
    items, metadata = await _user_announcements(
        db,
        int(user["user_id"]),
        page=page,
        per_page=per_page,
    )
    return {"items": items, **metadata}


@router.get("/unread")
async def get_unread_announcements(
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    user_id = int(user["user_id"])
    items, metadata = await _user_announcements(
        db,
        user_id,
        page=page,
        per_page=per_page,
        unread_only=True,
    )
    return {
        "count": metadata["total"],
        "unread_count": metadata["total"],
        "items": items,
        **metadata,
    }


@router.post("/read-all")
async def read_all_announcements(
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return {"marked": await mark_all_read(db, int(user["user_id"]))}


@router.post("/{announcement_id}/read")
async def read_announcement(
    announcement_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    if not await mark_read(db, int(user["user_id"]), announcement_id):
        raise HTTPException(status_code=404, detail="公告不存在或未发布")
    return {"ok": True, "announcement_id": announcement_id}


@router.get("/admin")
async def admin_list_announcements(
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    page_result = await paginate_announcements(
        db,
        include_drafts=True,
        page=page,
        per_page=per_page,
    )
    stats_by_id = await delivery_stats_many(
        db, [announcement for announcement, _read in page_result.items]
    )
    items = []
    for announcement, read in page_result.items:
        stats = stats_by_id.get(announcement.id)
        items.append(
            announcement_to_dict(announcement, read=read, delivery_stats=stats)
        )
    return {
        "items": items,
        "page": page_result.page,
        "per_page": page_result.per_page,
        "total": page_result.total,
        "total_pages": page_result.total_pages,
    }


@router.post("/admin")
async def admin_create_announcement(
    body: AnnouncementCreateRequest,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        announcement = await create_announcement(
            db,
            title=body.title,
            content=body.content,
            announcement_type=body.type,
            created_by=int(user["user_id"]),
            publish=_request_publishes(body),
            send=body.send,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return announcement_to_dict(announcement)


@router.patch("/admin/{announcement_id}")
async def admin_update_announcement(
    announcement_id: int,
    body: AnnouncementUpdateRequest,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    del user
    try:
        announcement = await update_announcement(
            db,
            announcement_id,
            title=body.title,
            content=body.content,
            announcement_type=body.type,
            publish=_request_publishes(body),
            send=body.send,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return announcement_to_dict(announcement)


@router.delete("/admin/{announcement_id}")
async def admin_delete_announcement(
    announcement_id: int,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    del user
    try:
        deleted = await delete_announcement(db, announcement_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="公告不存在")
    return {"ok": True}


@router.post("/admin/{announcement_id}/publish")
async def admin_publish_announcement(
    announcement_id: int,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    del user
    try:
        announcement = await publish_announcement(db, announcement_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return announcement_to_dict(announcement)


@router.post("/admin/{announcement_id}/withdraw")
async def admin_withdraw_announcement(
    announcement_id: int,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    del user
    try:
        announcement = await withdraw_announcement(db, announcement_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return announcement_to_dict(announcement)


@router.post("/admin/{announcement_id}/retry")
async def admin_retry_announcement(
    announcement_id: int,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    del user
    target = await get_announcement(db, announcement_id)
    if target is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    if target.status != "published":
        raise HTTPException(status_code=409, detail="仅已发布公告可重试投递")
    schedule_announcement_broadcast(
        announcement_id,
        expected_version=getattr(target, "publication_version", 1) or 1,
    )
    return {"ok": True, "scheduled": True}


__all__ = ["router"]
