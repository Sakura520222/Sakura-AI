"""Notification provider abstraction and announcement delivery workers.

The rest of the application only deals with ``NotificationEndpoint`` and
``NotificationDelivery`` rows.  Telegram and SMTP are deliberately resolved
inside provider adapters, so either integration can be disabled or fail
without taking down the WebUI request that published an announcement.
"""

from __future__ import annotations

import asyncio
import html
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.error import RetryAfter

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models.announcement_models import (
    Announcement,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.identity_models import NotificationEndpoint
from backend.services.announcement_service import (
    announcement_type_label,
    markdown_to_telegram_html,
    sanitize_markdown,
)

_SMTP_SECURITY_MODES = frozenset({"ssl", "starttls", "none"})


def normalize_smtp_security(value: object, default: str = "starttls") -> str:
    """Normalize the ``smtp_security`` mode; unknown values fall back."""
    mode = str(value or "").strip().lower()
    return mode if mode in _SMTP_SECURITY_MODES else default


class NotificationProviderError(RuntimeError):
    """A provider could not deliver a message."""


class NotificationProviderDisabled(NotificationProviderError):
    """The provider is not configured in this deployment."""


class NotificationProviderRetryAfter(NotificationProviderError):
    """Provider throttled a request and supplied a retry delay."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


class NotificationProvider(Protocol):
    """Minimal provider contract used by the registry."""

    channel: str

    async def send(
        self,
        *,
        endpoint: NotificationEndpoint,
        title: str,
        content: str,
        content_html: str,
        announcement_type: str,
    ) -> None: ...


class NotificationProviderRegistry:
    """Registry that allows tests and deployments to replace providers."""

    def __init__(self, providers: dict[str, NotificationProvider] | None = None):
        self._providers: dict[str, NotificationProvider] = dict(providers or {})

    def register(self, provider: NotificationProvider) -> None:
        self._providers[str(provider.channel).lower()] = provider

    def unregister(self, channel: str) -> None:
        self._providers.pop(channel.lower(), None)

    def get(self, channel: str) -> NotificationProvider | None:
        return self._providers.get(channel.lower())

    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class WebUINotificationProvider:
    """Web notifications are persisted in the delivery/read tables."""

    channel = "web"

    async def send(
        self,
        *,
        endpoint: NotificationEndpoint,
        title: str,
        content: str,
        content_html: str,
        announcement_type: str,
    ) -> None:
        return


# 部署未配置真实域名（app_domain 缺省为 localhost）时，页脚链接回退开源仓库。
_REPOSITORY_URL = "https://github.com/Sakura520222/Sakura-AI"


def _email_site_url(settings: Any) -> str:
    """页脚站点链接：优先当前部署的 app_domain，否则回退开源仓库。"""
    domain = (getattr(settings, "sanitized_app_domain", "") or "").strip()
    if domain and domain != "localhost":
        return f"https://{domain}"
    return _REPOSITORY_URL


def _announcement_email_html(
    *, title: str, label: str, content_html: str, site_url: str
) -> str:
    """组装公告邮件的 HTML 文档（内联样式以兼容邮件客户端）。

    content_html 来自 announcement_service.sanitize_markdown，正文中的原始
    HTML 已在渲染前转义，此处标题与类型标签再各自转义一次。
    """
    site_link = (
        f'<a href="{html.escape(site_url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" '
        f'style="color:#be185d;text-decoration:none;">Sakura-AI</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh">
<body style="margin:0;padding:0;background-color:#f6f7f9;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
    <div style="background:#ffffff;border-radius:12px;padding:28px 32px;">
      <p style="margin:0 0 12px;">
        <span style="display:inline-block;padding:2px 10px;border-radius:9999px;background:#fce7f3;color:#be185d;font-size:12px;font-weight:600;">{html.escape(label)}</span>
      </p>
      <h2 style="margin:0 0 20px;font-size:20px;line-height:1.4;color:#111827;"><strong>{html.escape(title)}</strong></h2>
      <div style="font-size:14px;line-height:1.75;color:#374151;">{content_html}</div>
      <p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #f3f4f6;font-size:12px;color:#9ca3af;">此邮件由 {site_link} 公告系统发送 · Sent by {site_link}</p>
    </div>
  </div>
</body>
</html>"""


class EmailNotificationProvider:
    """SMTP adapter with strict no-secret logging."""

    channel = "email"

    async def send(
        self,
        *,
        endpoint: NotificationEndpoint,
        title: str,
        content: str,
        content_html: str,
        announcement_type: str,
    ) -> None:
        settings = get_settings()
        if not getattr(settings, "email_enabled", True):
            raise NotificationProviderDisabled("Email 通知未启用")
        host = (getattr(settings, "smtp_host", None) or "").strip()
        sender = (getattr(settings, "smtp_from", None) or "").strip()
        if not host or not sender:
            raise NotificationProviderDisabled("SMTP 未配置")
        recipient = (endpoint.address or "").strip()
        if not recipient:
            raise NotificationProviderError("Email 端点为空")

        label = announcement_type_label(announcement_type)
        from_name = (
            getattr(settings, "smtp_from_name", None) or ""
        ).strip() or "Sakura-AI"
        message = EmailMessage()
        message["Subject"] = f"【{label}】{title}"
        # formataddr 负责昵称中的特殊字符；非 ASCII 昵称由 EmailMessage
        # 在序列化时自动做 RFC 2047 编码。
        message["From"] = formataddr((from_name, sender))
        message["To"] = recipient
        message.set_content(f"【{label}】{title}\n\n{content}")
        message.add_alternative(
            _announcement_email_html(
                title=title,
                label=label,
                content_html=content_html,
                site_url=_email_site_url(settings),
            ),
            subtype="html",
        )
        username = (getattr(settings, "smtp_username", None) or "").strip()
        password = getattr(settings, "smtp_password", None) or ""
        port = int(getattr(settings, "smtp_port", 587) or 587)
        security = normalize_smtp_security(
            getattr(settings, "smtp_security", "starttls")
        )

        def _send() -> None:
            context = ssl.create_default_context()
            # ssl = 隐式 TLS（SMTPS，通常 465 端口）：连接建立即协商 TLS；
            # starttls = 显式升级（通常 587/25 端口）；none = 明文，仅限可信中继。
            smtp_class: Any = (
                smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
            )
            with smtp_class(host, port, timeout=15, context=context) as smtp:
                if security == "starttls":
                    smtp.starttls(context=context)
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)

        # SMTP is blocking stdlib I/O; isolate it from the event loop.
        try:
            await asyncio.to_thread(_send)
        except NotificationProviderError:
            raise
        except Exception as exc:
            # Never include the password or full SMTP connection string in the
            # persisted error.  The exception type is enough for diagnostics.
            raise NotificationProviderError(
                f"SMTP 发送失败（{type(exc).__name__}）"
            ) from exc


# Bot API 10.1+ 的 sendRichMessage 支持 GFM 风格 Rich Markdown，文本上限 32768。
_TELEGRAM_RICH_MARKDOWN_MAX_CHARS = 32768
_TELEGRAM_LEGACY_TEXT_LIMIT = 4096


async def _send_telegram_rich(
    bot: Any,
    *,
    chat_id: int,
    label: str,
    title: str,
    content: str,
) -> bool:
    """优先以 Rich Markdown 发送公告；不可用或被拒绝时返回 False 走旧 API。

    python-telegram-bot 尚未封装 sendRichMessage，因此按官方文档直接请求
    ``bot.base_url`` 指向的 REST 端点（自建 Bot API Server 部署同样适用）。
    标题用文档允许的内联 HTML 混排加粗，避免对管理员输入做 Markdown 转义；
    正文本身就是面向 Web 编辑的 GFM 子集，原样透传，仅做长度截断。
    布局为三段：类型标签、加粗标题、正文，空行分隔。
    """
    body = str(content or "").rstrip()
    if len(body) > _TELEGRAM_RICH_MARKDOWN_MAX_CHARS - 128:
        body = body[: _TELEGRAM_RICH_MARKDOWN_MAX_CHARS - 128] + "\n…"
    markdown = (
        f"[{html.escape(label, quote=False)}]\n\n"
        f"<b>{html.escape(title, quote=False)}</b>\n\n"
        f"{body}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{bot.base_url}/sendRichMessage",
                json={
                    "chat_id": chat_id,
                    "rich_message": {"markdown": markdown},
                },
            )
    except Exception:
        # 网络异常等交给旧 API 回退再尝试，由它产生规范化错误。
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if payload.get("ok"):
        return True
    if payload.get("error_code") == 429:
        parameters = payload.get("parameters") or {}
        try:
            retry_after = max(0.0, float(parameters.get("retry_after", 0)))
        except (TypeError, ValueError):
            retry_after = 0.0
        raise NotificationProviderRetryAfter(
            f"Telegram Rich 发送被限流（retry_after={retry_after:g}s）",
            retry_after,
        )
    # 其余错误（旧 Bot API Server 无此方法、正文含无法解析的 HTML 等）
    # 都静默回退到旧 sendMessage 路径。
    return False


def _legacy_telegram_text(header_html: str, content: str) -> str:
    """构建旧 sendMessage 的 HTML 文本；超限时在 Markdown 源码级截断。"""
    text = f"{header_html}\n\n{markdown_to_telegram_html(content)}"
    if len(text) <= _TELEGRAM_LEGACY_TEXT_LIMIT:
        return text
    budget = max(200, _TELEGRAM_LEGACY_TEXT_LIMIT - len(header_html) - 8)
    while budget >= 200:
        text = (
            f"{header_html}\n\n{markdown_to_telegram_html(content[:budget])}…"
        )
        if len(text) <= _TELEGRAM_LEGACY_TEXT_LIMIT:
            return text
        budget -= 256
    # 极端兜底：去标签纯文本截断，保证必定可解析且不超限。
    plain_header = html.unescape(re.sub(r"<[^>]+>", "", header_html))
    plain = html.escape(f"{plain_header}\n\n{content}", quote=False)
    return plain[: _TELEGRAM_LEGACY_TEXT_LIMIT - 1] + "…"


class TelegramNotificationProvider:
    """Optional Telegram adapter; it never owns authentication or users."""

    channel = "telegram"

    async def send(
        self,
        *,
        endpoint: NotificationEndpoint,
        title: str,
        content: str,
        content_html: str,
        announcement_type: str,
    ) -> None:
        settings = get_settings()
        if not getattr(settings, "telegram_enabled", True):
            raise NotificationProviderDisabled("Telegram 通知未启用")
        if not getattr(settings, "telegram_bot_token", None):
            raise NotificationProviderDisabled("Telegram 未配置")
        from backend.telegram.bot import get_telegram_bot

        bot = get_telegram_bot()
        if bot is None:
            raise NotificationProviderDisabled("Telegram Bot 未启动")
        try:
            chat_id = int(endpoint.address)
        except (TypeError, ValueError) as exc:
            raise NotificationProviderError("Telegram 端点 ID 无效") from exc
        label = announcement_type_label(announcement_type)
        if await _send_telegram_rich(
            bot, chat_id=chat_id, label=label, title=title, content=content
        ):
            return
        # 旧环境回退：sendMessage 仅支持固定 HTML 标签集且限 4096 字符。
        header = (
            f"[{html.escape(label, quote=False)}]\n\n"
            f"<b>{html.escape(title, quote=False)}</b>"
        )
        text = _legacy_telegram_text(header, content)
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except RetryAfter as exc:
            raise NotificationProviderRetryAfter(
                f"Telegram 发送被限流（retry_after={exc.retry_after:g}s）",
                exc.retry_after,
            ) from exc
        except Exception as exc:
            raise NotificationProviderError(
                f"Telegram 发送失败（{type(exc).__name__}）"
            ) from exc


def default_notification_registry() -> NotificationProviderRegistry:
    registry = NotificationProviderRegistry()
    registry.register(WebUINotificationProvider())
    registry.register(EmailNotificationProvider())
    registry.register(TelegramNotificationProvider())
    return registry


notification_registry = default_notification_registry()


def _safe_error_message(exc: BaseException) -> str:
    """Bound and redact provider errors before persisting them."""
    value = str(exc).replace("\x00", " ").strip()
    settings = get_settings()
    for secret_name in ("smtp_password", "telegram_bot_token"):
        secret = getattr(settings, secret_name, None)
        if secret:
            value = value.replace(str(secret), "***")
    return value[:1000] or type(exc).__name__


class NotificationService:
    """Deliver persisted rows with bounded concurrency, retries and isolation."""

    def __init__(self, registry: NotificationProviderRegistry | None = None):
        self.registry = registry or notification_registry
        self._rate_locks: dict[str, asyncio.Lock] = {}
        self._last_rate_limited_at: dict[str, float] = {}

    async def _throttle(self, channel: str, interval: float) -> None:
        """Enforce one provider-wide start interval across concurrent users."""

        if interval <= 0:
            return
        channel = str(channel).lower()
        lock = self._rate_locks.setdefault(channel, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_for = max(
                0.0,
                self._last_rate_limited_at.get(channel, 0.0) + interval - now,
            )
            if wait_for:
                await asyncio.sleep(wait_for)
            self._last_rate_limited_at[channel] = time.monotonic()

    async def _deliver_one(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        endpoint: NotificationEndpoint | None,
        lock: asyncio.Lock,
    ) -> bool:
        provider = self.registry.get(delivery.channel)
        if provider is None:
            error = NotificationProviderDisabled(
                f"未注册通知 Provider: {delivery.channel}"
            )
            async with lock:
                delivery.status = DeliveryStatus.FAILED.value
                delivery.error_message = _safe_error_message(error)
                delivery.attempts = int(delivery.attempts or 0) + 1
                await db.commit()
            return False
        if endpoint is None and delivery.channel != "web":
            error = NotificationProviderError("通知端点不存在或已禁用")
            async with lock:
                delivery.status = DeliveryStatus.FAILED.value
                delivery.error_message = _safe_error_message(error)
                delivery.attempts = int(delivery.attempts or 0) + 1
                await db.commit()
            return False

        settings = get_settings()
        max_attempts = max(
            1, int(getattr(settings, "notification_retry_max_attempts", 3) or 3)
        )
        delay = max(
            0.0,
            float(
                getattr(settings, "notification_retry_initial_delay_seconds", 1.0)
                or 0.0
            ),
        )
        backoff = max(
            1.0,
            float(getattr(settings, "notification_retry_backoff_factor", 2.0) or 1.0),
        )
        rate_limit = max(
            0.0,
            float(getattr(settings, "notification_rate_limit_seconds", 0.05) or 0.0),
        )
        last_error: BaseException | None = None
        initial_attempts = int(delivery.attempts or 0)
        for attempt in range(max_attempts):
            await self._throttle(delivery.channel, rate_limit)
            try:
                await provider.send(
                    endpoint=endpoint,
                    title=announcement.title,
                    content=announcement.content,
                    # 公告正文以 Markdown 存储；邮件 HTML 部分用服务端的
                    # 保守渲染器生成（XSS 安全），不再使用纯转义兜底。
                    content_html=sanitize_markdown(announcement.content),
                    announcement_type=str(
                        getattr(announcement, "announcement_type", "") or ""
                    ),
                )
            except Exception as exc:  # isolate this endpoint/provider only
                last_error = exc
                if attempt + 1 < max_attempts:
                    # A zero delay is useful in tests and low-latency
                    # deployments, but it must not disable the retry budget.
                    retry_after = (
                        exc.retry_after
                        if isinstance(exc, NotificationProviderRetryAfter)
                        else 0.0
                    )
                    wait_for = max(delay, retry_after)
                    if wait_for:
                        await asyncio.sleep(wait_for)
                    delay *= backoff
                    continue
                break
            else:
                async with lock:
                    delivery.status = DeliveryStatus.SENT.value
                    delivery.error_message = None
                    delivery.attempts = initial_attempts + attempt + 1
                    delivery.sent_at = now_utc()
                    delivery.next_retry_at = None
                    await db.commit()
                return True

        async with lock:
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error_message = _safe_error_message(last_error or RuntimeError())
            delivery.attempts = initial_attempts + max_attempts
            delivery.next_retry_at = None
            await db.commit()
        return False

    async def broadcast_announcement(
        self,
        db: AsyncSession,
        announcement_or_id: Announcement | int,
    ) -> dict[str, int]:
        """Broadcast one announcement; each provider/user failure is isolated."""
        if isinstance(announcement_or_id, Announcement):
            announcement = announcement_or_id
        else:
            announcement = (
                await db.execute(
                    select(Announcement).where(Announcement.id == announcement_or_id)
                )
            ).scalar_one_or_none()
        if announcement is None:
            return {"sent": 0, "failed": 0, "skipped": 0}
        # A publish task may still be queued when an administrator withdraws
        # the announcement.  Re-check the lifecycle state in the worker so a
        # withdrawn item is never delivered merely because it was published
        # when the task was scheduled.
        if str(getattr(announcement, "status", "")).lower() != "published":
            return {"sent": 0, "failed": 0, "skipped": 0}
        deliveries = (
            await db.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.announcement_id == announcement.id,
                    NotificationDelivery.status.in_(
                        [DeliveryStatus.PENDING.value, DeliveryStatus.FAILED.value]
                    ),
                )
            )
        ).scalars().all()
        endpoint_rows = (
            await db.execute(
                select(NotificationEndpoint)
                .where(NotificationEndpoint.enabled.is_(True))
                .order_by(NotificationEndpoint.id)
            )
        ).scalars().all()
        # Delivery rows are unique by (user, channel).  Binding a new
        # Telegram endpoint disables the previous one, but setdefault also
        # makes old databases with duplicate enabled rows deterministic.
        endpoint_by_user_channel: dict[tuple[int, str], NotificationEndpoint] = {}
        for row in endpoint_rows:
            endpoint_by_user_channel.setdefault(
                (row.user_id, str(row.provider).lower()), row
            )
        # The application-level factory gives every delivery its own session.
        # This matters for concurrent providers: committing one ORM object on a
        # shared AsyncSession can flush another task's uncommitted state.  Test
        # fakes and direct callers without an initialized application factory
        # intentionally fall back to the supplied session below.
        from backend.models import database as db_module

        session_factory = db_module.async_session
        semaphore = asyncio.Semaphore(
            max(1, int(getattr(get_settings(), "notification_max_concurrency", 5) or 5))
        )
        lock = asyncio.Lock()

        async def run(delivery: NotificationDelivery) -> bool:
            async with semaphore:
                endpoint = endpoint_by_user_channel.get(
                    (delivery.user_id, str(delivery.channel).lower())
                )
                if session_factory is not None:
                    async with session_factory() as delivery_db:
                        # Re-read the rows in this session.  The status check
                        # also prevents a queued retry from sending after a
                        # withdrawal raced with the worker.
                        fresh_announcement = await delivery_db.get(
                            Announcement, announcement.id
                        )
                        fresh_delivery = await delivery_db.get(
                            NotificationDelivery, delivery.id
                        )
                        if fresh_announcement is None or fresh_delivery is None:
                            return False
                        if (
                            str(getattr(fresh_announcement, "status", "")).lower()
                            != "published"
                        ):
                            return False
                        fresh_endpoint = endpoint
                        if endpoint is not None:
                            fresh_endpoint = await delivery_db.get(
                                NotificationEndpoint, endpoint.id
                            )
                            if (
                                fresh_endpoint is None
                                or not bool(fresh_endpoint.enabled)
                                or str(fresh_endpoint.provider).lower()
                                != str(delivery.channel).lower()
                            ):
                                # Keep the row pending.  If the user binds a
                                # new endpoint, a later retry can deliver it;
                                # most importantly, an unbind racing with a
                                # queued task cannot send to the old address.
                                return False
                        return await self._deliver_one(
                            delivery_db,
                            fresh_delivery,
                            fresh_announcement,
                            fresh_endpoint,
                            lock,
                        )
                return await self._deliver_one(db, delivery, announcement, endpoint, lock)

        results = await asyncio.gather(*(run(delivery) for delivery in deliveries))
        return {
            "sent": sum(results),
            "failed": len(results) - sum(results),
            "skipped": 0,
        }


notification_service = NotificationService()


async def broadcast_announcement(
    db: AsyncSession,
    announcement_or_id: Announcement | int,
) -> dict[str, int]:
    """Compatibility function for workers/tests that do not need the service."""
    return await notification_service.broadcast_announcement(db, announcement_or_id)


__all__ = [
    "EmailNotificationProvider",
    "NotificationProvider",
    "NotificationProviderDisabled",
    "NotificationProviderError",
    "NotificationProviderRegistry",
    "NotificationProviderRetryAfter",
    "NotificationService",
    "TelegramNotificationProvider",
    "WebUINotificationProvider",
    "broadcast_announcement",
    "default_notification_registry",
    "normalize_smtp_security",
    "notification_registry",
    "notification_service",
]
