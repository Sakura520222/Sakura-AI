"""API v1 SSE 事件流端点"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from backend.api.v1.deps import require_api_auth

router = APIRouter(tags=["Events"])


@router.get("/events")
async def sse_events(
    request: Request,
    user: dict = Depends(require_api_auth),
):
    """SSE 事件流（Bearer Token 认证）"""
    from backend.webui.sse import SSEManager

    sse_manager = SSEManager()

    async def event_generator():
        async for event in sse_manager.subscribe(user["user_id"]):
            if await request.is_disconnected():
                break
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
