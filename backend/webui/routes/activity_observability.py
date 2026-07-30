"""Conversation-first activity observability REST/SSE surface."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, StreamingResponse

from backend.services.activity_observability.access_service import (
    ActivityAccessService,
    ActivityNotFoundError,
    CursorResetRequiredError,
)
from backend.services.activity_observability.conversation_service import (
    ConversationProjectionService,
)
from backend.services.activity_observability.tool_service import (
    ArtifactAuthorization,
    DefaultArtifactEncryptionProvider,
    ToolService,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    render_template,
    require_auth,
)
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


class _RequestArtifactAuthorizer:
    def __init__(
        self,
        *,
        access: ActivityAccessService,
        user: dict[str, Any],
        db: AsyncSession,
        session_id: int,
    ) -> None:
        self.access = access
        self.user = user
        self.db = db
        self.session_id = session_id

    async def authorize(
        self,
        *,
        artifact,
        session,
        require_trace: bool,
        **_: Any,
    ) -> ArtifactAuthorization:
        if self.user.get("role") != "super_admin":
            return ArtifactAuthorization(False, "repository", False)
        if int(session.id) != self.session_id:
            return ArtifactAuthorization(False, "repository", False)
        allowed = await self.access.may_view_reasoning_artifact(
            self.user,
            session,
            artifact,
            db=self.db,
        )
        return ArtifactAuthorization(
            allowed=allowed,
            authorization_scope=(
                "super_admin:trace" if require_trace else "super_admin"
            ),
            can_display=allowed,
        )


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
        return await _access_service(request, db).create_snapshot(
            session_id, user, db=db
        )
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


@router.get("/api/sessions/{session_id}/conversation")
async def activity_conversation(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    limit: int | None = None,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        access = _access_service(request, db)
        return await ConversationProjectionService(
            db,
            access_service=access,
        ).get_conversation(
            session_id,
            user,
            cursor=cursor,
            limit=limit,
        )
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    except CursorResetRequiredError as exc:
        raise HTTPException(status_code=409, detail="cursor reset required") from exc


@router.get("/api/sessions/{session_id}/conversation/events")
async def activity_conversation_events(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        access = _access_service(request, db)
        return await ConversationProjectionService(
            db,
            access_service=access,
        ).get_updates(
            session_id,
            user,
            cursor=cursor,
        )
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    except CursorResetRequiredError as exc:
        raise HTTPException(status_code=409, detail="cursor reset required") from exc


@router.get("/api/sessions/{session_id}/artifacts/{artifact_id}")
async def activity_artifact(
    session_id: int,
    artifact_id: int,
    request: Request,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    access = _access_service(request, db)
    try:
        await access.require_session_access(session_id, user, db)
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    authorizer = _RequestArtifactAuthorizer(
        access=access,
        user=user,
        db=db,
        session_id=session_id,
    )
    service = ToolService(
        encryption_provider=DefaultArtifactEncryptionProvider(),
        artifact_authorizer=authorizer,
    )
    view = await service.read_artifact_with_audit(
        artifact_id,
        reader=str(user.get("user_id") or user.get("sub") or "unknown"),
        require_trace=True,
    )
    if view is None:
        raise _not_found()
    return JSONResponse(
        content=jsonable_encoder(asdict(view)),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
                    message = await sse_manager.receive(queue, timeout=30)
                    if message is None:
                        return
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
            # 此流使用请求级数据库依赖；必须先释放会话再注销订阅，使清库流程
            # 可以通过 subscriber_count 准确确认数据库锁已经解除。
            try:
                await db.close()
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
