"""SSE 事件流路由"""

import asyncio
import json
from starlette.responses import StreamingResponse
from starlette.requests import Request
from fastapi import APIRouter, Depends

from backend.webui.deps import require_auth
from backend.webui.sse import sse_manager

router = APIRouter()


@router.get("/sse/events")
async def sse_events(
    request: Request,
    user: dict = Depends(require_auth),
):
    """SSE 事件流端点"""
    channel = "webui:events"

    async def event_generator():
        queue = sse_manager.subscribe(channel)
        try:
            while True:
                # 检查客户端是否断开
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                except asyncio.TimeoutError:
                    # 发送心跳防止连接超时
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
