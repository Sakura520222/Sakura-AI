"""Versioned JSON/SSE boundary for the existing authorized Activity service."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, StreamingResponse

from backend.api.v1.deps import require_api_auth
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.models import database as db_module
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
from backend.webui.deps import get_db, require_auth
from backend.webui.sse import sse_manager, user_activity_channel

router = APIRouter(prefix="/activity", tags=["Activity"], route_class=PrivateAPIRoute)


# 以下边界处理与处理器自 backend/webui/routes/activity_observability.py 原样迁入。
# Boundary helpers and handlers relocated verbatim from the legacy WebUI routes.


def _access_service(
    request: Request, db: AsyncSession | None = None
) -> ActivityAccessService:
    service = getattr(request.app.state, "activity_access_service", None)
    if service is not None:
        if db is not None:
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


async def activity_sessions(
    request: Request,
    limit: int = 20,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _access_service(request, db).list_sessions(user, limit=limit, db=db)


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


async def activity_operational_logs(
    session_id: int,
    request: Request,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Super-admin diagnostics with the same session ownership check as Activity."""
    from fastapi.responses import PlainTextResponse
    from starlette.concurrency import run_in_threadpool

    from backend.services.operational_log_service import session_logs

    if user.get("role") != "super_admin":
        raise _not_found()
    try:
        await _access_service(request, db).require_session_access(session_id, user, db)
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    text = await run_in_threadpool(session_logs, session_id)
    return PlainTextResponse(
        text,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


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


async def activity_stream(
    session_id: int,
    request: Request,
    user: dict = Depends(require_auth),
):
    service = _access_service(request)
    try:
        async with db_module.async_session() as db:
            await service.require_session_access(session_id, user, db)
            initial_auth_version = await service.authorization_version(user, db)
    except ActivityNotFoundError as exc:
        raise _not_found() from exc
    user_id = user.get("user_id")
    channel = user_activity_channel(user_id)
    queue = sse_manager.subscribe(channel)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await sse_manager.receive(queue, timeout=30)
                    if message is None:
                        return
                except TimeoutError:
                    # Revalidate before every heartbeat.  A revoked scope closes
                    # the stream without leaking which session caused the revoke.
                    async with db_module.async_session() as heartbeat_db:
                        current = await service.authorization_version(
                            user, heartbeat_db
                        )
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


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_sessions(request=request, limit=limit, user=user, db=db)


@router.get("/sessions/{session_id}/snapshot")
async def snapshot(
    session_id: int,
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_snapshot(session_id, request, user=user, db=db)


@router.get("/sessions/{session_id}/events")
async def events(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_events(session_id, request, cursor=cursor, user=user, db=db)


@router.get("/sessions/{session_id}/conversation")
async def conversation(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    limit: int | None = Query(None, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_conversation(
        session_id, request, cursor=cursor, limit=limit, user=user, db=db
    )


@router.get("/sessions/{session_id}/conversation/events")
async def conversation_events(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_conversation_events(
        session_id, request, cursor=cursor, user=user, db=db
    )


@router.get("/sessions/{session_id}/artifacts/{artifact_id}")
async def artifact(
    session_id: int,
    artifact_id: int,
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_artifact(session_id, artifact_id, request, user=user, db=db)


@router.get("/sessions/{session_id}/logs")
async def logs(
    session_id: int,
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity_operational_logs(session_id, request, user=user, db=db)


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: int, request: Request, user: dict = Depends(require_api_auth)
):
    return await activity_stream(session_id, request, user=user)
