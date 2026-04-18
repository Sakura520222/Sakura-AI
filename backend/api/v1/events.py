"""API v1 SSE 事件流端点"""

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from backend.webui.sse import sse_manager
from backend.api.v1.deps import require_api_auth

router = APIRouter(tags=["Events"])


@router.get("/events")
async def sse_events(
    request: Request,
    user: dict = Depends(require_api_auth),
):
    """SSE 事件流（Bearer Token 认证）"""
    channel = "webui:events"

    async def event_generator():
        queue = sse_manager.subscribe(channel)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
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
