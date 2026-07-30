"""数据库全量重置前的运行时静默处理。"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

_OUTBOX_STOP_TIMEOUT_SECONDS = 5.0
_SSE_STOP_TIMEOUT_SECONDS = 5.0


async def quiesce_database_reset_runtime(app: Any) -> None:
    """停止并回收会持续访问数据库的 SSE 与活动 Outbox 任务。

    ``connection.json`` 已在调用本函数前切换到 Setup 模式，因此不再接收新的
    正常业务请求。这里必须在 DROP TABLE 前等待任务结束，避免任务在表删除后
    再次查询并让 FastAPI lifespan 以异常结束。
    """

    state = app.state

    # 活动观测 SSE 会在整个流生命周期持有请求级数据库会话。先结束全部
    # EventSource，并等待生成器的 finally 释放会话，避免 DROP TABLE 等待锁。
    from backend.webui.sse import sse_manager

    closed_sse = sse_manager.close_all()
    if closed_sse:
        remaining_sse = await sse_manager.wait_until_closed(
            timeout=_SSE_STOP_TIMEOUT_SECONDS
        )
        if remaining_sse:
            logger.warning(
                "{} 个 SSE 长连接未在 {} 秒内完成清理，清库流程继续",
                remaining_sse,
                _SSE_STOP_TIMEOUT_SECONDS,
            )

    dispatcher = getattr(state, "activity_outbox_dispatcher", None)
    task = getattr(state, "activity_outbox_task", None)

    if task is None:
        return

    if dispatcher is not None:
        try:
            dispatcher.stop()
        except Exception as exc:
            logger.warning(
                "清库前停止活动 Outbox dispatcher 失败，将直接取消任务: error_type={}",
                type(exc).__name__,
            )

    try:
        if dispatcher is None:
            task.cancel()
        elif not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_OUTBOX_STOP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "活动 Outbox dispatcher 未在 {} 秒内停止，将强制取消",
                    _OUTBOX_STOP_TIMEOUT_SECONDS,
                )
                task.cancel()

        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        # 已结束的失败任务不会再访问数据库；消费异常，避免 lifespan 再次 await
        # 同一个 Task 时把清库前的历史错误升级为 application shutdown failed。
        logger.warning(
            "活动 Outbox dispatcher 已异常结束，清库流程继续: error_type={}",
            type(exc).__name__,
        )
    finally:
        state.activity_outbox_task = None
        state.activity_outbox_dispatcher = None

    logger.info("✅ 清库前活动观测 Outbox dispatcher 已停止")
