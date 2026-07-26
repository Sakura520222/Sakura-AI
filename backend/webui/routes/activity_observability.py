"""New activity observability REST/SSE helpers.

This intentionally stops at snapshot/cursor access primitives; Task 10 owns the
full page and route surface.  It is safe to mount as a small API router now.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from backend.services.activity_observability.access_service import (
    ActivityAccessService,
    ActivityNotFoundError,
    CursorResetRequiredError,
)
from backend.webui.deps import get_db, get_user_preferences, render_template, require_auth
from backend.webui.sse import sse_manager, user_activity_channel

router = APIRouter(prefix="/activity/observability", tags=["Activity Observability"])


def _access_service(request: Request, db: AsyncSession) -> ActivityAccessService:
    service = getattr(request.app.state, "activity_access_service", None)
    if service is not None:
        service.db = db
        return service
    raise HTTPException(status_code=503, detail="activity observability unavailable")


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


@router.get("/")
async def activity_observability_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """新版实时活动监控页面（基于长期 Session/Invocation/Attempt）。"""
    return render_template(
        "activity_observability.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="activity_observability",
    )


@router.get("/api/sessions")
async def activity_sessions(
    request: Request,
    limit: int = 20,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _access_service(request, db).list_sessions(user, limit=limit, db=db)


@router.get("/api/sessions/{session_id}/snapshot")
async def activity_snapshot(
    session_id: int,
    request: Request,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await _access_service(request, db).create_snapshot(session_id, user, db=db)
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    except CursorResetRequiredError as exc:
        raise HTTPException(status_code=409, detail="cursor reset required") from exc


@router.get("/api/sessions/{session_id}/events")
async def activity_events(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await _access_service(request, db).list_events_after(
            session_id, user, cursor=cursor, db=db
        )
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    except CursorResetRequiredError as exc:
        raise HTTPException(status_code=409, detail="cursor reset required") from exc


@router.get("/api/sessions/{session_id}/stream")
async def activity_stream(
    session_id: int,
    request: Request,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    service = _access_service(request, db)
    try:
        await service.require_session_access(session_id, user, db)
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    user_id = user.get("user_id")
    channel = user_activity_channel(user_id)
    queue = sse_manager.subscribe(channel)
    initial_auth_version = await service.authorization_version(user, db)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Revalidate before every heartbeat.  A revoked scope closes
                    # the stream without leaking which session caused the revoke.
                    current = await service.authorization_version(user, db)
                    if current != initial_auth_version:
                        return
                    yield ": keepalive\n\n"
                    continue
                data = message.get("data")
                if not isinstance(data, dict):
                    continue
                if set(data) != {"event_id", "sequence", "projection_version"}:
                    continue
                yield f"event: activity:notification\ndata: {json.dumps(data)}\n\n"
        finally:
            sse_manager.unsubscribe(channel, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
