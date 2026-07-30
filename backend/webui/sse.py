"""SSE (Server-Sent Events) 实时推送模块"""

import asyncio
import hashlib
import json
import re
from typing import Any

import redis.exceptions  # 仅异常类型引用，不触发连接初始化
from loguru import logger

from backend.services.activity_observability.contracts import PublicActivityNotification

_SSE_SHUTDOWN = object()


class SSEManager:
    """SSE 连接管理器（进程内）"""

    def __init__(self, queue_size: int = 100):
        self._queue_size = queue_size
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, channel: str) -> asyncio.Queue:
        """订阅频道，返回消息队列"""
        if channel not in self._subscribers:
            self._subscribers[channel] = []
        queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[channel].append(queue)
        logger.debug(
            f"SSE 客户端订阅频道: {channel}, 当前订阅数: {len(self._subscribers[channel])}"
        )
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue):
        """取消订阅"""
        if channel in self._subscribers:
            try:
                self._subscribers[channel].remove(queue)
            except ValueError:
                pass
            if not self._subscribers[channel]:
                del self._subscribers[channel]
            logger.debug(f"SSE 客户端取消订阅频道: {channel}")

    async def publish(self, channel: str, event: dict[str, Any]):
        """向频道所有订阅者广播事件"""
        if channel not in self._subscribers:
            return
        dead_queues = []
        for queue in self._subscribers[channel]:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead_queues.append(queue)
        for q in dead_queues:
            self.unsubscribe(channel, q)

    async def receive(
        self,
        queue: asyncio.Queue,
        *,
        timeout: float,
    ) -> Dict[str, Any] | None:
        """等待下一条事件；应用准备退出时返回 ``None``。"""

        event = await asyncio.wait_for(queue.get(), timeout=timeout)
        if event is _SSE_SHUTDOWN:
            return None
        return event

    def close_all(self) -> int:
        """立即唤醒并关闭所有进程内 SSE 流。"""

        queues = [
            queue
            for subscribers in self._subscribers.values()
            for queue in tuple(subscribers)
        ]
        for queue in queues:
            # 丢弃尚未发送的业务事件，保证关闭哨兵位于队首并立即唤醒生成器。
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(_SSE_SHUTDOWN)

        if queues:
            logger.info("已通知 {} 个 SSE 长连接关闭", len(queues))
        return len(queues)

    @property
    def subscriber_count(self) -> int:
        """返回当前进程内仍登记的 SSE 订阅数。"""

        return sum(len(subscribers) for subscribers in self._subscribers.values())

    async def wait_until_closed(self, *, timeout: float) -> int:
        """等待生成器完成清理，返回超时后仍存活的订阅数。"""

        deadline = asyncio.get_running_loop().time() + timeout
        while self.subscriber_count:
            remaining_time = deadline - asyncio.get_running_loop().time()
            if remaining_time <= 0:
                return self.subscriber_count
            await asyncio.sleep(min(0.01, remaining_time))
        return 0


_ACTIVITY_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{1,255}$")
_ACTIVITY_CHANNEL_PREFIX = "activity:user:"


def user_activity_channel(user_id: str | int) -> str:
    """Return a non-injectable, opaque channel for one authenticated user."""
    value = str(user_id).strip()
    if not _ACTIVITY_USER_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid activity user id")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_ACTIVITY_CHANNEL_PREFIX}{digest}"


async def publish_user_activity_notification(
    user_id: str | int, notification: PublicActivityNotification
) -> None:
    """Publish only the three-field activity notification to one user channel."""
    channel = user_activity_channel(user_id)
    data = notification.to_sse_data()
    if set(data) != {"event_id", "sequence", "projection_version"}:
        raise ValueError("activity SSE projection must contain exactly three fields")
    await publish_event("activity:notification", data, channel=channel)


async def subscribe_user_activity(user_id: str | int) -> asyncio.Queue:
    """Subscribe to a validated user-scoped activity channel."""
    queue = sse_manager.subscribe(user_activity_channel(user_id))
    return queue


# 全局 SSE 管理器单例
sse_manager = SSEManager()


async def publish_event(
    event_type: str, data: dict[str, Any], channel: str = "webui:events"
):
    """发布事件到所有 SSE 订阅者 + Redis Pub/Sub（支持多进程）"""
    event = {"type": event_type, "data": data, "channel": channel}

    # 本进程内广播
    await sse_manager.publish(channel, event)

    # 通过 Redis Pub/Sub 广播到其他进程
    try:
        from backend.core.redis import get_async_redis

        redis_client = await get_async_redis()
        await redis_client.publish(f"sse:{channel}", json.dumps(event))
    except Exception as e:
        logger.warning(f"Redis Pub/Sub 发布失败（仅影响多进程部署）: {e}")


# 重连配置
_RECONNECT_INITIAL_DELAY = 1.0  # 初始延迟（秒）
_RECONNECT_MAX_DELAY = 60.0  # 最大延迟（秒）
_RECONNECT_BACKOFF_FACTOR = 2.0  # 退避因子
_RECONNECT_MAX_ATTEMPTS = 20  # 最大重试次数

# 重连任务引用（防止 GC 回收）
_reconnect_task: asyncio.Task | None = None


async def start_redis_listener(_attempt: int = 1):
    """启动 Redis Pub/Sub 监听任务（带指数退避重连）

    Args:
        _attempt: 当前重试次数（内部递归使用）
    """
    global _reconnect_task
    try:
        from backend.core.redis import get_async_redis

        redis_client = await get_async_redis()
        pubsub = redis_client.pubsub()

        # 订阅所有频道前缀
        await pubsub.psubscribe("sse:*")
        logger.info("Redis Pub/Sub 监听已启动")

        # 成功连接后重置重试计数
        _attempt = 1

        # 外层循环：Redis 空闲读超时只重新进入 listen()；真实连接异常交给外层重连
        while True:
            try:
                async for message in pubsub.listen():
                    if message["type"] == "pmessage":
                        try:
                            event = json.loads(message["data"])
                            channel = event.get("channel", "webui:events")
                            # 转发给本地 SSE 订阅者（避免重复广播到 Redis）
                            await sse_manager.publish(channel, event)
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(f"解析 SSE 事件失败: {e}")
            except (redis.exceptions.TimeoutError, TimeoutError) as e:
                logger.debug(f"Redis listen 空闲读超时，继续等待消息: {e}")
                continue
    except asyncio.CancelledError:
        logger.info("Redis Pub/Sub 监听已停止")
        raise
    except Exception as e:
        # 检查是否超过最大重试次数
        if _RECONNECT_MAX_ATTEMPTS > 0 and _attempt >= _RECONNECT_MAX_ATTEMPTS:
            logger.error(
                f"Redis Pub/Sub 监听已达最大重试次数 ({_RECONNECT_MAX_ATTEMPTS})，停止重连。"
                f"最后错误: {e}"
            )
            return

        # 计算指数退避延迟
        delay = min(
            _RECONNECT_INITIAL_DELAY * (_RECONNECT_BACKOFF_FACTOR ** (_attempt - 1)),
            _RECONNECT_MAX_DELAY,
        )
        logger.warning(
            f"Redis Pub/Sub 监听异常: {e}，"
            f"{delay:.1f}秒后重连 (第 {_attempt}/{_RECONNECT_MAX_ATTEMPTS} 次)"
        )
        await asyncio.sleep(delay)
        _reconnect_task = asyncio.create_task(start_redis_listener(_attempt + 1))
