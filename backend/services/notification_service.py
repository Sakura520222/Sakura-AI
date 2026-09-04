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
import secrets
import smtplib
import ssl
import time
from contextlib import suppress
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Protocol

import httpx
from sqlalchemy import exists, or_, select, update
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

# A delivery row deliberately remains ``pending``/``failed`` while a provider
# call is in flight.  The token and expiry are the worker's lease, so adding a
# third status cannot break existing counters, dashboards, or old databases.
# Keep this conservative enough to cover a normal provider request while the
# heartbeat below keeps unusually slow calls alive.
_DELIVERY_LEASE_SECONDS = 120.0


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
            if security == "ssl":
                # SMTP_SSL accepts the TLS context in its constructor.  The
                # plain SMTP constructor does not on all supported Python
                # versions, so only pass it for implicit TLS.
                smtp_context = smtplib.SMTP_SSL(
                    host, port, timeout=15, context=context
                )
            else:
                smtp_context = smtplib.SMTP(host, port, timeout=15)
            with smtp_context as smtp:
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

    @staticmethod
    def _row_publication_version(row: object) -> int:
        try:
            value = int(getattr(row, "publication_version", 1) or 1)
        except (TypeError, ValueError):
            return 1
        return max(1, value)

    @staticmethod
    def _set_local_committed_value(row: object, key: str, value: object) -> None:
        """Mirror a committed UPDATE without scheduling an ORM flush.

        Broadcast rows are selected in one session and claimed in another.
        Assigning the claim fields to the selected instance would leave that
        first session dirty; a later read or commit could then overwrite a
        newer worker's token.  SQLAlchemy's committed-value helper keeps the
        local identity map in sync without creating an unguarded write.
        """
        try:
            from sqlalchemy.orm.attributes import set_committed_value

            set_committed_value(row, key, value)
        except Exception:
            # Lightweight object fakes have no SQLAlchemy state.  They do not
            # participate in a later ORM flush, so a plain assignment is safe.
            setattr(row, key, value)

    async def _fresh_row(
        self, db: AsyncSession, model: type, row_id: int
    ) -> object | None:
        """Reload a row when the supplied session supports ORM ``get``."""
        getter = getattr(db, "get", None)
        if not callable(getter):
            return None
        try:
            return await getter(model, row_id, populate_existing=True)
        except TypeError:
            # Lightweight test/session adapters may expose only the two
            # positional arguments accepted by older SQLAlchemy versions.
            return await getter(model, row_id)

    async def _fresh_publication_rows(
        self,
        db: AsyncSession,
        announcement_id: int,
        delivery_id: int,
    ) -> tuple[object | None, object | None, bool]:
        """Read publication identity from a short-lived independent session.

        A long-lived MySQL transaction under the default REPEATABLE READ
        isolation can otherwise keep seeing the pre-republish snapshot even
        when ``populate_existing`` is requested.  The boolean reports whether
        an independent probe was available; callers fail closed if that probe
        exists but cannot find either row.
        """
        from backend.models import database as db_module

        factory = db_module.async_session
        if factory is not None:
            probe = factory()
            try:
                if hasattr(probe, "__aenter__"):
                    async with probe as scoped:
                        return (
                            await self._fresh_row(
                                scoped, Announcement, announcement_id
                            ),
                            await self._fresh_row(
                                scoped, NotificationDelivery, delivery_id
                            ),
                            True,
                        )
                rows = (
                    await self._fresh_row(probe, Announcement, announcement_id),
                    await self._fresh_row(probe, NotificationDelivery, delivery_id),
                    True,
                )
                close = getattr(probe, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                return rows
            except Exception:
                close = getattr(probe, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                return None, None, True

        getter = getattr(db, "get", None)
        if not callable(getter):
            return None, None, False
        return (
            await self._fresh_row(db, Announcement, announcement_id),
            await self._fresh_row(db, NotificationDelivery, delivery_id),
            True,
        )

    @staticmethod
    def _delivery_lease_seconds() -> float:
        """Return the bounded lease duration used by every worker."""
        return _DELIVERY_LEASE_SECONDS

    async def _run_in_fresh_session(self, fallback: AsyncSession, operation):
        """Run a short database operation in an independent transaction.

        MySQL's default REPEATABLE READ can keep a long-lived worker session
        on an old snapshot.  Claims and heartbeats therefore use the
        application session factory whenever it is available.  Lightweight
        test adapters (and legacy direct callers) deliberately fall back to
        their supplied session.
        """
        from backend.models import database as db_module

        factory = db_module.async_session
        if factory is None:
            return await operation(fallback)

        scoped = factory()
        if hasattr(scoped, "__aenter__"):
            async with scoped as session:
                return await operation(session)

        try:
            return await operation(scoped)
        finally:
            close = getattr(scoped, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def _claim_delivery(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        *,
        expected_version: int | None,
    ) -> str | None:
        """Atomically claim one delivery row for this worker.

        The claim is intentionally a conditional UPDATE rather than a
        read-then-write sequence.  Two retry requests can have selected the
        same row from their own snapshots, but only one UPDATE can win while
        the row is pending/failed and its previous lease is absent or expired.
        No database lock is held while the provider is called.
        """
        token = secrets.token_urlsafe(32)
        claimed_at = now_utc()
        claim_until = claimed_at + timedelta(
            seconds=self._delivery_lease_seconds()
        )
        conditions = [
            NotificationDelivery.id == delivery.id,
            NotificationDelivery.announcement_id == announcement.id,
            NotificationDelivery.status.in_(
                [DeliveryStatus.PENDING.value, DeliveryStatus.FAILED.value]
            ),
            or_(
                NotificationDelivery.claim_token.is_(None),
                NotificationDelivery.claim_until.is_(None),
                NotificationDelivery.claim_until <= claimed_at,
            ),
        ]
        current_announcement = exists(
            select(Announcement.id).where(
                Announcement.id == announcement.id,
                Announcement.status == "published",
            )
        )
        if expected_version is not None:
            conditions.extend(
                [
                    NotificationDelivery.publication_version == expected_version,
                ]
            )
            current_announcement = exists(
                select(Announcement.id).where(
                    Announcement.id == announcement.id,
                    Announcement.status == "published",
                    Announcement.publication_version == expected_version,
                )
            )
        conditions.append(current_announcement)

        try:
            result = await db.execute(
                update(NotificationDelivery)
                .where(*conditions)
                .values(claim_token=token, claim_until=claim_until)
            )
            if getattr(result, "rowcount", None) != 1:
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    await rollback()
                return None
            await db.commit()
        except Exception:
            rollback = getattr(db, "rollback", None)
            if callable(rollback):
                with suppress(Exception):
                    await rollback()
            # A missing lease column or a database that cannot execute the
            # conditional update means this process cannot prove ownership.
            # Failing closed is safer than sending without a claim.
            return None

        self._set_local_committed_value(delivery, "claim_token", token)
        self._set_local_committed_value(delivery, "claim_until", claim_until)
        return token

    async def _heartbeat_delivery(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        *,
        worker_token: str,
        expected_version: int | None,
    ) -> bool:
        """Extend a worker lease using token/version compare-and-set."""
        if not worker_token:
            return False
        renewed_until = now_utc() + timedelta(
            seconds=self._delivery_lease_seconds()
        )

        async def renew(scoped: AsyncSession) -> bool:
            conditions = [
                NotificationDelivery.id == delivery.id,
                NotificationDelivery.claim_token == worker_token,
                NotificationDelivery.status.in_(
                    [DeliveryStatus.PENDING.value, DeliveryStatus.FAILED.value]
                ),
                exists(
                    select(Announcement.id).where(
                        Announcement.id == announcement.id,
                        Announcement.status == "published",
                    )
                ),
            ]
            if expected_version is not None:
                conditions.append(
                    NotificationDelivery.publication_version == expected_version
                )
                conditions.append(
                    exists(
                        select(Announcement.id).where(
                            Announcement.id == announcement.id,
                            Announcement.status == "published",
                            Announcement.publication_version == expected_version,
                        )
                    )
                )
            result = await scoped.execute(
                update(NotificationDelivery)
                .where(*conditions)
                .values(claim_until=renewed_until, updated_at=now_utc())
            )
            if getattr(result, "rowcount", None) != 1:
                rollback = getattr(scoped, "rollback", None)
                if callable(rollback):
                    await rollback()
                return False
            await scoped.commit()
            return True

        try:
            renewed = await self._run_in_fresh_session(db, renew)
        except Exception:
            return False
        if renewed:
            self._set_local_committed_value(delivery, "claim_until", renewed_until)
        return bool(renewed)

    async def _lease_heartbeat_loop(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        *,
        worker_token: str,
        expected_version: int | None,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.01, self._delivery_lease_seconds() / 3.0)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                if not await self._heartbeat_delivery(
                    db,
                    delivery,
                    announcement,
                    worker_token=worker_token,
                    expected_version=expected_version,
                ):
                    return
            else:
                return

    async def _with_lease_heartbeat(
        self,
        operation,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        *,
        worker_token: str,
        expected_version: int | None,
    ):
        """Run an awaited operation while periodically extending its lease."""
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat_loop(
                db,
                delivery,
                announcement,
                worker_token=worker_token,
                expected_version=expected_version,
                stop=stop,
            )
        )
        try:
            return await operation()
        finally:
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _publication_is_current(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        expected_version: int | None,
    ) -> bool:
        if expected_version is None:
            return str(getattr(announcement, "status", "")).lower() == "published"
        if (
            self._row_publication_version(announcement) != expected_version
            or self._row_publication_version(delivery) != expected_version
            or str(getattr(announcement, "status", "")).lower() != "published"
        ):
            return False
        fresh_announcement, fresh_delivery, has_probe = (
            await self._fresh_publication_rows(
                db, int(announcement.id), int(delivery.id)
            )
        )
        if not has_probe:
            # Lightweight adapters used by legacy unit tests do not expose a
            # getter; the in-memory row checks above still protect them.
            return True
        if fresh_announcement is None or fresh_delivery is None:
            return False
        return (
            self._row_publication_version(fresh_announcement) == expected_version
            and self._row_publication_version(fresh_delivery) == expected_version
            and str(getattr(fresh_announcement, "status", "")).lower()
            == "published"
        )

    async def _mark_terminal_state(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        *,
        lock: asyncio.Lock,
        expected_version: int | None,
        values: dict[str, Any],
        worker_token: str,
    ) -> bool | None:
        """Write a terminal result only if its publication lease is current.

        Returning ``None`` means the worker became stale (for example because
        an administrator republished while a provider call was in flight).
        The broadcast caller reports that as skipped, never as a failure in
        the new round.
        """
        # A terminal result always belongs to the worker that claimed this
        # row.  Keep the token out of callers' value dictionaries so a stale
        # worker cannot accidentally clear a newer worker's lease.
        persisted_values = dict(values)
        persisted_values.setdefault("claim_token", None)
        persisted_values.setdefault("claim_until", None)
        async with lock:
            if not await self._publication_is_current(
                db, delivery, announcement, expected_version
            ):
                return None
            # Use a conditional UPDATE as the final race guard.  A
            # concurrent reset or claim replacement makes this statement a
            # no-op even if it happened after the read check.  Every session,
            # including test adapters, must support this fail-closed path.
            conditions = [
                NotificationDelivery.id == delivery.id,
                NotificationDelivery.claim_token == worker_token,
                exists(
                    select(Announcement.id).where(
                        Announcement.id == NotificationDelivery.announcement_id,
                        Announcement.id == announcement.id,
                        Announcement.status == "published",
                    )
                ),
            ]
            if expected_version is not None:
                conditions.extend(
                    [
                        NotificationDelivery.publication_version == expected_version,
                        exists(
                            select(Announcement.id).where(
                                Announcement.id == announcement.id,
                                Announcement.publication_version == expected_version,
                                Announcement.status == "published",
                            )
                        ),
                    ]
                )
            result = await db.execute(
                update(NotificationDelivery)
                .where(*conditions)
                .values(**persisted_values)
            )
            if getattr(result, "rowcount", None) != 1:
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    await rollback()
                return None
            await db.commit()
            for key, value in persisted_values.items():
                self._set_local_committed_value(delivery, key, value)
            return True

    async def _deliver_one(
        self,
        db: AsyncSession,
        delivery: NotificationDelivery,
        announcement: Announcement,
        endpoint: NotificationEndpoint | None,
        lock: asyncio.Lock,
        expected_version: int | None = None,
        worker_token: str | None = None,
    ) -> bool | None:
        if worker_token is None:
            worker_token = await self._claim_delivery(
                db,
                delivery,
                announcement,
                expected_version=expected_version,
            )
            if worker_token is None:
                return None
        if not await self._publication_is_current(
            db, delivery, announcement, expected_version
        ):
            return None
        if not await self._heartbeat_delivery(
            db,
            delivery,
            announcement,
            worker_token=worker_token,
            expected_version=expected_version,
        ):
            return None
        provider = self.registry.get(delivery.channel)
        if provider is None:
            error = NotificationProviderDisabled(
                f"未注册通知 Provider: {delivery.channel}"
            )
            marked = await self._mark_terminal_state(
                db,
                delivery,
                announcement,
                lock=lock,
                expected_version=expected_version,
                worker_token=worker_token,
                values={
                    "status": DeliveryStatus.FAILED.value,
                    "error_message": _safe_error_message(error),
                    "attempts": int(delivery.attempts or 0) + 1,
                    "updated_at": now_utc(),
                },
            )
            return None if marked is None else False
        if endpoint is None and delivery.channel != "web":
            error = NotificationProviderError("通知端点不存在或已禁用")
            marked = await self._mark_terminal_state(
                db,
                delivery,
                announcement,
                lock=lock,
                expected_version=expected_version,
                worker_token=worker_token,
                values={
                    "status": DeliveryStatus.FAILED.value,
                    "error_message": _safe_error_message(error),
                    "attempts": int(delivery.attempts or 0) + 1,
                    "updated_at": now_utc(),
                },
            )
            return None if marked is None else False

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
            if not await self._publication_is_current(
                db, delivery, announcement, expected_version
            ):
                return None
            if not await self._heartbeat_delivery(
                db,
                delivery,
                announcement,
                worker_token=worker_token,
                expected_version=expected_version,
            ):
                return None
            await self._with_lease_heartbeat(
                lambda: self._throttle(delivery.channel, rate_limit),
                db,
                delivery,
                announcement,
                worker_token=worker_token,
                expected_version=expected_version,
            )
            # Republish can race while a provider-wide rate-limit lock is
            # sleeping; check again immediately before the external call.
            if not await self._heartbeat_delivery(
                db,
                delivery,
                announcement,
                worker_token=worker_token,
                expected_version=expected_version,
            ) or not await self._publication_is_current(
                db, delivery, announcement, expected_version
            ):
                return None
            try:
                await self._with_lease_heartbeat(
                    lambda: provider.send(
                        endpoint=endpoint,
                        title=announcement.title,
                        content=announcement.content,
                        # 公告正文以 Markdown 存储；邮件 HTML 部分用服务端的
                        # 保守渲染器生成（XSS 安全），不再使用纯转义兜底。
                        content_html=sanitize_markdown(announcement.content),
                        announcement_type=str(
                            getattr(announcement, "announcement_type", "") or ""
                        ),
                    ),
                    db,
                    delivery,
                    announcement,
                    worker_token=worker_token,
                    expected_version=expected_version,
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
                        await self._with_lease_heartbeat(
                            lambda wait_for=wait_for: asyncio.sleep(wait_for),
                            db,
                            delivery,
                            announcement,
                            worker_token=worker_token,
                            expected_version=expected_version,
                        )
                    delay *= backoff
                    continue
                break
            else:
                marked = await self._mark_terminal_state(
                    db,
                    delivery,
                    announcement,
                    lock=lock,
                    expected_version=expected_version,
                    worker_token=worker_token,
                    values={
                        "status": DeliveryStatus.SENT.value,
                        "error_message": None,
                        "attempts": initial_attempts + attempt + 1,
                        "sent_at": now_utc(),
                        "next_retry_at": None,
                        "updated_at": now_utc(),
                    },
                )
                return marked

        marked = await self._mark_terminal_state(
            db,
            delivery,
            announcement,
            lock=lock,
            expected_version=expected_version,
            worker_token=worker_token,
            values={
                "status": DeliveryStatus.FAILED.value,
                "error_message": _safe_error_message(
                    last_error or RuntimeError()
                ),
                "attempts": initial_attempts + max_attempts,
                "next_retry_at": None,
                "updated_at": now_utc(),
            },
        )
        return None if marked is None else False

    async def broadcast_announcement(
        self,
        db: AsyncSession,
        announcement_or_id: Announcement | int,
        *,
        expected_version: int | None = None,
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
        if expected_version is None:
            try:
                expected_version = int(
                    getattr(announcement, "publication_version", 1) or 1
                )
            except (TypeError, ValueError):
                expected_version = 1
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
                    NotificationDelivery.publication_version == expected_version,
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

        async def run(delivery: NotificationDelivery) -> bool | None:
            async with semaphore:
                endpoint = endpoint_by_user_channel.get(
                    (delivery.user_id, str(delivery.channel).lower())
                )
                if session_factory is not None:
                    async with session_factory() as delivery_db:
                        # Claim before reading the worker snapshot.  The
                        # initial broadcast query may have selected a stale
                        # pending row while another retry already sent it.
                        worker_token = await self._claim_delivery(
                            delivery_db,
                            delivery,
                            announcement,
                            expected_version=expected_version,
                        )
                        if worker_token is None:
                            return None
                        # The claim committed its own transaction.  Reading
                        # these rows afterwards starts a fresh snapshot, which
                        # is important under MySQL REPEATABLE READ.
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
                            or self._row_publication_version(fresh_announcement)
                            != expected_version
                            or self._row_publication_version(fresh_delivery)
                            != expected_version
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
                            expected_version,
                            worker_token,
                        )
                return await self._deliver_one(
                    db,
                    delivery,
                    announcement,
                    endpoint,
                    lock,
                    expected_version,
                )

        results = await asyncio.gather(*(run(delivery) for delivery in deliveries))
        sent = sum(result is True for result in results)
        skipped = sum(result is None for result in results)
        return {
            "sent": sent,
            "failed": len(results) - sent - skipped,
            "skipped": skipped,
        }


notification_service = NotificationService()


async def broadcast_announcement(
    db: AsyncSession,
    announcement_or_id: Announcement | int,
    *,
    expected_version: int | None = None,
) -> dict[str, int]:
    """Compatibility function for workers/tests that do not need the service."""
    return await notification_service.broadcast_announcement(
        db, announcement_or_id, expected_version=expected_version
    )


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
