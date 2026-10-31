"""Versioned JSON/SSE boundary for the existing authorized Activity service."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.webui.deps import get_db
from backend.webui.routes import activity_observability as activity

router = APIRouter(prefix="/activity", tags=["Activity"], route_class=PrivateAPIRoute)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity.activity_sessions(
        request=request, limit=limit, user=user, db=db
    )


@router.get("/sessions/{session_id}/snapshot")
async def snapshot(
    session_id: int,
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity.activity_snapshot(session_id, request, user=user, db=db)


@router.get("/sessions/{session_id}/events")
async def events(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity.activity_events(
        session_id, request, cursor=cursor, user=user, db=db
    )


@router.get("/sessions/{session_id}/conversation")
async def conversation(
    session_id: int,
    request: Request,
    cursor: str | None = None,
    limit: int | None = Query(None, ge=1, le=100),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity.activity_conversation(
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
    return await activity.activity_conversation_events(
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
    return await activity.activity_artifact(
        session_id, artifact_id, request, user=user, db=db
    )


@router.get("/sessions/{session_id}/logs")
async def logs(
    session_id: int,
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await activity.activity_operational_logs(
        session_id, request, user=user, db=db
    )


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: int, request: Request, user: dict = Depends(require_api_auth)
):
    return await activity.activity_stream(session_id, request, user=user)
