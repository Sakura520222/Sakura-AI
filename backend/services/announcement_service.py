"""Announcement lifecycle, safe Markdown rendering and read-state helpers."""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import now_utc
from backend.models.announcement_models import (
    Announcement,
    AnnouncementDeliveryHistory,
    AnnouncementPublicationHistory,
    AnnouncementRead,
    AnnouncementStatus,
    AnnouncementType,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.identity_models import NotificationEndpoint
from backend.models.telegram_models import TelegramUser
from backend.services.database_reset_runtime_service import (
    create_registered_background_task,
)

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,500})\]\(([^)\s]{1,2048})\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_UNORDERED_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")


def _safe_href(value: str) -> str:
    """Allow only harmless links in announcement Markdown."""
    candidate = html.unescape(value.strip())
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "#"
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return "#"
    if candidate.startswith("//"):
        return "#"
    return html.escape(candidate, quote=True)


def _render_inline_html(value: str, *, telegram: bool = False) -> str:
    """Render the shared inline Markdown subset without touching attributes.

    Link markup is emitted as real HTML before emphasis is applied.  Running
    the emphasis regexes over that HTML used to turn an innocuous URL such as
    ``https://host/foo_bar_baz`` into a malformed ``href``.  Code spans have
    the same problem for underscores and asterisks in their contents.  Keep
    both constructs behind private placeholders until all text substitutions
    are complete, then restore the already-escaped HTML fragments.
    """

    # NUL is not valid in useful announcement text and gives us a marker that
    # cannot be matched by the Markdown regexes.  Replace user supplied NULs
    # first so a caller cannot forge a placeholder that is restored as HTML.
    escaped = html.escape(str(value).replace("\x00", "\ufffd"), quote=False)
    protected: list[str] = []
    marker = "\x00SakuraInlineToken{}\x00"

    def protect(fragment: str) -> str:
        protected.append(fragment)
        return marker.format(len(protected) - 1)

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        if telegram:
            candidate = html.unescape(match.group(2).strip())
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                return label
            if (
                parsed.scheme.lower() not in _TELEGRAM_LINK_SCHEMES
                or candidate.startswith("//")
            ):
                return label
            fragment = (
                f'<a href="{html.escape(candidate, quote=True)}">{label}</a>'
            )
        else:
            href = _safe_href(match.group(2))
            fragment = (
                f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
                f"{label}</a>"
            )
        return protect(fragment)

    escaped = _MARKDOWN_LINK_RE.sub(link, escaped)
    # Links are protected before code spans are parsed.  Otherwise a backtick
    # in an URL could become a ``<code>`` token inside the href attribute.
    # Code fragments are already HTML-escaped and safe to restore verbatim.
    escaped = re.sub(
        r"`([^`\n]+)`",
        lambda match: protect(f"<code>{match.group(1)}</code>"),
        escaped,
    )
    if telegram:
        escaped = re.sub(
            r"\*\*([^*\n]+)\*\*|__([^_\n]+)__",
            lambda match: f"<b>{match.group(1) or match.group(2)}</b>",
            escaped,
        )
        escaped = re.sub(
            r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)",
            lambda match: f"<i>{match.group(1) or match.group(2)}</i>",
            escaped,
        )
    else:
        escaped = re.sub(
            r"\*\*([^*\n]+)\*\*|__([^_\n]+)__",
            lambda match: f"<strong>{match.group(1) or match.group(2)}</strong>",
            escaped,
        )
        escaped = re.sub(
            r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)",
            lambda match: f"<em>{match.group(1) or match.group(2)}</em>",
            escaped,
        )

    # A code token may contain a link token (for example
    # ``[`code`](https://example.invalid)``).  Restore outer fragments first
    # so no private marker can leak into the returned HTML.
    for index in range(len(protected) - 1, -1, -1):
        fragment = protected[index]
        escaped = escaped.replace(marker.format(index), fragment)
    return escaped


def sanitize_markdown(markdown_text: str | None) -> str:
    """Render a conservative Markdown subset to XSS-safe HTML.

    The frontend also runs DOMPurify, but server-side sanitization is required
    for email delivery and for clients that consume the announcement API.
    Raw HTML is escaped before any Markdown substitutions, so script/style/event
    attributes can never become active markup.
    """
    source = str(markdown_text or "")
    output: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            output.append("</ul>")
            in_ul = False
        if in_ol:
            output.append("</ol>")
            in_ol = False

    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if heading:
            close_lists()
            level = len(heading.group(1))
            output.append(
                f"<h{level}>{_render_inline_html(heading.group(2))}</h{level}>"
            )
        elif unordered:
            if in_ol:
                output.append("</ol>")
                in_ol = False
            if not in_ul:
                output.append("<ul>")
                in_ul = True
            output.append(
                f"<li>{_render_inline_html(unordered.group(1))}</li>"
            )
        elif ordered:
            if in_ul:
                output.append("</ul>")
                in_ul = False
            if not in_ol:
                output.append("<ol>")
                in_ol = True
            output.append(f"<li>{_render_inline_html(ordered.group(1))}</li>")
        elif not line.strip():
            close_lists()
            output.append("<br>")
        else:
            close_lists()
            output.append(f"<p>{_render_inline_html(line)}</p>")
    close_lists()
    return "\n".join(output)


# Alias with an explicit security-oriented name for API/tests.
render_markdown_safe = sanitize_markdown


# 通知渠道（邮件/Telegram）展示公告类型时使用的标签；与 WebUI 的
# announcements.type_* 翻译含义一致，面向最终用户而非管理员页面。
ANNOUNCEMENT_TYPE_LABELS = {
    AnnouncementType.GENERAL.value: "公告",
    AnnouncementType.IMPORTANT.value: "重要公告",
    AnnouncementType.FEATURE.value: "功能更新",
    AnnouncementType.MAINTENANCE.value: "维护通知",
    AnnouncementType.RELEASE.value: "版本发布",
}

_TELEGRAM_LINK_SCHEMES = {"http", "https", "mailto"}


def announcement_type_label(value: object) -> str:
    """Map a stored announcement type to its notification display label."""
    return ANNOUNCEMENT_TYPE_LABELS.get(
        str(value or "").strip().lower(), ANNOUNCEMENT_TYPE_LABELS[AnnouncementType.GENERAL.value]
    )


def markdown_to_telegram_html(markdown_text: str | None) -> str:
    """Render the conservative announcement Markdown subset as Bot API HTML.

    ``sendMessage(parse_mode="HTML")`` only accepts a fixed tag set
    (b/strong/i/em/u/s/a/code/pre/blockquote)，因此标题渲染为加粗行、
    列表渲染为「• / 1.」纯文本行。所有非 Markdown 文本先转义，输出天然
    可安全提交给 Telegram 解析。
    """
    source = str(markdown_text or "")
    lines: list[str] = []
    ordered_counter = 0

    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if heading:
            ordered_counter = 0
            lines.append(
                f"<b>{_render_inline_html(heading.group(2), telegram=True)}</b>"
            )
        elif unordered:
            ordered_counter = 0
            lines.append(
                f"• {_render_inline_html(unordered.group(1), telegram=True)}"
            )
        elif ordered:
            ordered_counter += 1
            lines.append(
                f"{ordered_counter}. "
                f"{_render_inline_html(ordered.group(1), telegram=True)}"
            )
        elif not line.strip():
            lines.append("")
        else:
            ordered_counter = 0
            lines.append(_render_inline_html(line, telegram=True))
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class AnnouncementDeliveryStats:
    pending: int
    sent: int
    failed: int
    history: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        current = {
            "pending": self.pending,
            "sent": self.sent,
            "failed": self.failed,
        }
        return {
            **current,
            "current": current,
            "history": list(self.history),
            "history_count": len(self.history),
        }


@dataclass(frozen=True)
class AnnouncementListPage:
    """A page of announcements plus stable pagination metadata.

    ``list_announcements`` remains the compatibility-oriented list API used by
    older callers.  New HTTP endpoints use this value so they can expose the
    total number of rows and make every older announcement reachable.
    """

    items: list[tuple[Announcement, bool]]
    total: int
    page: int
    per_page: int
    total_pages: int


def announcement_to_dict(
    announcement: Announcement,
    *,
    read: bool = False,
    delivery_stats: AnnouncementDeliveryStats | None = None,
) -> dict:
    return {
        "id": announcement.id,
        "title": announcement.title,
        "content": announcement.content,
        "content_html": sanitize_markdown(announcement.content),
        "type": announcement.announcement_type,
        "status": announcement.status,
        "publication_version": int(
            getattr(announcement, "publication_version", 1) or 1
        ),
        "created_by": announcement.created_by,
        "created_at": announcement.created_at.isoformat()
        if announcement.created_at
        else None,
        "published_at": announcement.published_at.isoformat()
        if announcement.published_at
        else None,
        "updated_at": announcement.updated_at.isoformat()
        if announcement.updated_at
        else None,
        "read": read,
        "delivery": delivery_stats.as_dict() if delivery_stats else None,
        "publication_history": list(delivery_stats.history)
        if delivery_stats
        else [],
    }


def _validate_type(value: str | None) -> str:
    candidate = str(value or AnnouncementType.GENERAL.value).lower()
    allowed = {item.value for item in AnnouncementType}
    if candidate not in allowed:
        raise ValueError("公告类型无效")
    return candidate


async def create_announcement(
    db: AsyncSession,
    *,
    title: str,
    content: str,
    announcement_type: str = AnnouncementType.GENERAL.value,
    created_by: int | None = None,
    publish: bool = False,
    send: bool | None = None,
) -> Announcement:
    title = str(title or "").strip()
    content = str(content or "").strip()
    if not title or len(title) > 500:
        raise ValueError("公告标题不能为空且不得超过 500 个字符")
    if not content:
        raise ValueError("公告内容不能为空")
    announcement = Announcement(
        title=title,
        content=content,
        announcement_type=_validate_type(announcement_type),
        status=AnnouncementStatus.DRAFT.value,
        created_by=created_by,
    )
    publish_now = bool(publish if send is None else send)
    if publish_now:
        announcement.status = AnnouncementStatus.PUBLISHED.value
        announcement.published_at = now_utc()
        announcement.updated_at = now_utc()
    db.add(announcement)
    if publish_now:
        # Flush first so the initial immutable snapshot and delivery rows can
        # use the stable announcement FK in the same transaction.
        await db.flush()
        await _ensure_publication_snapshot(db, announcement)
        await _ensure_delivery_rows(
            db, announcement.id, _publication_version(announcement)
        )
    await db.commit()
    await db.refresh(announcement)
    if publish_now:
        schedule_announcement_broadcast(
            int(announcement.id),
            expected_version=_publication_version(announcement),
        )
    return announcement


async def update_announcement(
    db: AsyncSession,
    announcement_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    announcement_type: str | None = None,
    publish: bool = False,
    send: bool | None = None,
) -> Announcement:
    announcement = await _get_lifecycle_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    publish_now = bool(publish if send is None else send)
    if (
        announcement.status == AnnouncementStatus.PUBLISHED.value
        and not publish_now
    ):
        raise ValueError("已发布公告不可直接修改，请先撤回")

    # Validate every requested field before archiving/resetting anything.  A
    # rejected edit must leave the old round fully intact, including its
    # publication version and delivery state.
    normalized_title: str | None = None
    if title is not None:
        normalized_title = str(title).strip()
        if not normalized_title or len(normalized_title) > 500:
            raise ValueError("公告标题不能为空且不得超过 500 个字符")
    normalized_content: str | None = None
    if content is not None:
        normalized_content = str(content).strip()
        if not normalized_content:
            raise ValueError("公告内容不能为空")
    normalized_type: str | None = None
    if announcement_type is not None:
        normalized_type = _validate_type(announcement_type)

    # An older installation may contain a withdrawn publication created
    # before the history table existed.  Capture its still-current body
    # before saving a new draft, otherwise the next publish would mistake the
    # edited draft for the old round's snapshot.
    if announcement.status == AnnouncementStatus.WITHDRAWN.value and not publish_now:
        await _archive_publication(db, announcement)

    # Archive/reset the old publication before changing the content.  The
    # snapshot was created at publication time, so withdrawn -> edit -> send
    # still retains the actual old body rather than the edited draft.
    starts_new_round = publish_now and (
        announcement.status
        in {AnnouncementStatus.PUBLISHED.value, AnnouncementStatus.WITHDRAWN.value}
    )
    if starts_new_round:
        await _archive_and_reset_publication(db, announcement)

    if normalized_title is not None:
        announcement.title = normalized_title
    if normalized_content is not None:
        announcement.content = normalized_content
    if normalized_type is not None:
        announcement.announcement_type = normalized_type
    if publish_now:
        announcement.status = AnnouncementStatus.PUBLISHED.value
        announcement.published_at = now_utc()
    announcement.updated_at = now_utc()
    if publish_now:
        await db.flush()
        await _ensure_publication_snapshot(db, announcement)
        await _ensure_delivery_rows(
            db, announcement.id, _publication_version(announcement)
        )
    await db.commit()
    await db.refresh(announcement)
    if publish_now:
        schedule_announcement_broadcast(
            int(announcement.id),
            expected_version=_publication_version(announcement),
        )
    return announcement


async def get_announcement(
    db: AsyncSession, announcement_id: int
) -> Announcement | None:
    return (
        await db.execute(select(Announcement).where(Announcement.id == announcement_id))
    ).scalar_one_or_none()


async def _get_lifecycle_announcement(
    db: AsyncSession, announcement_id: int
) -> Announcement | None:
    """Load a lifecycle row with a database lock when supported.

    The lock is held only through the metadata/archive transaction; provider
    network calls happen later in the worker and never hold this row lock.
    SQLite ignores ``FOR UPDATE`` but still exercises the same transaction
    ordering in integration tests.
    """
    return (
        await db.execute(
            select(Announcement)
            .where(Announcement.id == announcement_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


def _publication_version(value: Announcement | NotificationDelivery) -> int:
    """Return a valid publication version for old ORM/fake rows."""
    try:
        version = int(getattr(value, "publication_version", 1) or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, version)


async def _current_delivery_rows(
    db: AsyncSession, announcement_id: int, publication_version: int
) -> list[NotificationDelivery]:
    return (
        await db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.announcement_id == announcement_id,
                NotificationDelivery.publication_version == publication_version,
            )
        )
    ).scalars().all()


async def _get_publication_snapshot(
    db: AsyncSession, announcement_id: int, publication_version: int
) -> AnnouncementPublicationHistory | None:
    return (
        await db.execute(
            select(AnnouncementPublicationHistory).where(
                AnnouncementPublicationHistory.announcement_id == announcement_id,
                AnnouncementPublicationHistory.publication_version
                == publication_version,
            )
        )
    ).scalar_one_or_none()


async def _ensure_publication_snapshot(
    db: AsyncSession, announcement: Announcement
) -> AnnouncementPublicationHistory | None:
    """Create the immutable content snapshot for the current round."""
    version = _publication_version(announcement)
    history = await _get_publication_snapshot(db, announcement.id, version)
    if history is not None:
        return history
    history = AnnouncementPublicationHistory(
        announcement_id=announcement.id,
        publication_version=version,
        title=str(announcement.title),
        content=str(announcement.content),
        announcement_type=str(
            getattr(announcement, "announcement_type", "")
            or AnnouncementType.GENERAL.value
        ),
        published_at=announcement.published_at,
        delivery_states="[]",
    )
    db.add(history)
    await db.flush()
    return history


def _delivery_state(row: NotificationDelivery) -> dict[str, Any]:
    def iso(value: object) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else None

    return {
        "delivery_id": getattr(row, "id", None),
        "user_id": getattr(row, "user_id", None),
        "channel": str(getattr(row, "channel", "")),
        "status": str(getattr(row, "status", DeliveryStatus.PENDING.value)),
        "error_message": getattr(row, "error_message", None),
        "attempts": int(getattr(row, "attempts", 0) or 0),
        "sent_at": iso(getattr(row, "sent_at", None)),
        "next_retry_at": iso(getattr(row, "next_retry_at", None)),
    }


async def _archive_publication(
    db: AsyncSession,
    announcement: Announcement,
    *,
    archived_at: object | None = None,
) -> AnnouncementPublicationHistory | None:
    """Freeze the current version's delivery state before it is reused."""
    version = _publication_version(announcement)
    history = await _ensure_publication_snapshot(db, announcement)
    if history is None or history.archived_at is not None:
        return history
    rows = await _current_delivery_rows(db, announcement.id, version)
    states = [_delivery_state(row) for row in rows]
    for row in rows:
        db.add(
            AnnouncementDeliveryHistory(
                publication_id=history.id,
                user_id=getattr(row, "user_id", None),
                channel=str(getattr(row, "channel", "")),
                status=str(
                    getattr(row, "status", DeliveryStatus.PENDING.value)
                ),
                error_message=getattr(row, "error_message", None),
                attempts=int(getattr(row, "attempts", 0) or 0),
                sent_at=getattr(row, "sent_at", None),
                next_retry_at=getattr(row, "next_retry_at", None),
                source_delivery_id=getattr(row, "id", None),
            )
        )
    history.delivery_states = json.dumps(
        states, ensure_ascii=False, separators=(",", ":")
    )
    history.archived_at = archived_at or now_utc()
    await db.flush()
    return history


async def _archive_and_reset_publication(
    db: AsyncSession, announcement: Announcement
) -> int:
    """Archive the old round, clear reads, and prepare a new version."""
    old_version = _publication_version(announcement)
    await _archive_publication(db, announcement, archived_at=now_utc())
    await db.execute(
        delete(AnnouncementRead).where(
            AnnouncementRead.announcement_id == announcement.id
        )
    )
    announcement.publication_version = old_version + 1
    await db.flush()
    return old_version


async def _ensure_delivery_rows(
    db: AsyncSession,
    announcement_id: int,
    publication_version: int | None = None,
) -> None:
    users = (
        await db.execute(select(TelegramUser).where(TelegramUser.is_active.is_(True)))
    ).scalars().all()
    active_user_ids = {user.id for user in users}
    endpoints = (
        await db.execute(
            select(NotificationEndpoint)
            .where(NotificationEndpoint.enabled.is_(True))
            .order_by(NotificationEndpoint.id)
        )
    ).scalars().all()
    existing = (
        await db.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.announcement_id == announcement_id
            )
        )
    ).scalars().all()
    existing_by_key = {(row.user_id, row.channel): row for row in existing}
    existing_keys = set(existing_by_key)
    target_version = publication_version or 1

    def reset_for_current_round(row: NotificationDelivery) -> None:
        if _publication_version(row) == target_version:
            return
        row.status = DeliveryStatus.PENDING.value
        row.error_message = None
        row.attempts = 0
        row.sent_at = None
        row.next_retry_at = None
        # A delivery row can be reused by a later publication version.  Any
        # lease left by a worker from the previous round must never block the
        # first worker of the new round (or make it look already claimed).
        if hasattr(row, "claim_token"):
            row.claim_token = None
        if hasattr(row, "claim_until"):
            row.claim_until = None
        row.publication_version = target_version
        row.updated_at = now_utc()

    for user in users:
        key = (user.id, "web")
        if key not in existing_keys:
            row = NotificationDelivery(
                announcement_id=announcement_id,
                user_id=user.id,
                channel="web",
                status=DeliveryStatus.PENDING.value,
                publication_version=target_version,
            )
            db.add(row)
            existing_keys.add(key)
            existing_by_key[key] = row
        else:
            reset_for_current_round(existing_by_key[key])
    for endpoint in endpoints:
        channel = str(endpoint.provider).lower()
        if channel not in {"email", "telegram"} or endpoint.user_id not in active_user_ids:
            continue
        key = (endpoint.user_id, channel)
        if key in existing_keys:
            reset_for_current_round(existing_by_key[key])
            continue
        row = NotificationDelivery(
            announcement_id=announcement_id,
            user_id=endpoint.user_id,
            channel=channel,
            status=DeliveryStatus.PENDING.value,
            publication_version=target_version,
        )
        db.add(row)
        existing_keys.add(key)
        existing_by_key[key] = row


async def _broadcast_in_background(
    announcement_id: int, expected_version: int | None = None
) -> None:
    from backend.models import database as db_module
    from backend.services.notification_service import notification_service

    if db_module.async_session is None:
        logger.warning("公告广播跳过：数据库会话尚未初始化")
        return
    try:
        async with db_module.async_session() as session:
            await notification_service.broadcast_announcement(
                session,
                announcement_id,
                expected_version=expected_version,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Delivery rows retain failure state for the admin statistics/retry API.
        logger.exception("公告异步广播失败: announcement_id={}", announcement_id)


def schedule_announcement_broadcast(
    announcement_id: int, *, expected_version: int | None = None
) -> asyncio.Task | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("公告广播未调度：当前没有运行中的事件循环")
        return None
    del loop
    try:
        return create_registered_background_task(
            _broadcast_in_background(announcement_id, expected_version),
            "announcement.broadcast",
        )
    except RuntimeError as exc:
        # Unit callers outside the application lifespan have no bound runtime
        # supervisor.  Never create an untracked task in that case.
        logger.warning("公告广播未调度：运行时 supervisor 不可用: {}", exc)
        return None


async def publish_announcement(
    db: AsyncSession,
    announcement_id: int,
    *,
    schedule_broadcast: bool = True,
) -> Announcement:
    announcement = await _get_lifecycle_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    transitioned = False
    if announcement.status != AnnouncementStatus.PUBLISHED.value:
        if announcement.status == AnnouncementStatus.WITHDRAWN.value:
            await _archive_and_reset_publication(db, announcement)
        announcement.status = AnnouncementStatus.PUBLISHED.value
        announcement.published_at = now_utc()
        announcement.updated_at = now_utc()
        transitioned = True
        await db.flush()
        await _ensure_publication_snapshot(db, announcement)
        await _ensure_delivery_rows(
            db, announcement.id, _publication_version(announcement)
        )
        await db.commit()
        await db.refresh(announcement)
    if schedule_broadcast and transitioned:
        schedule_announcement_broadcast(
            int(announcement.id),
            expected_version=_publication_version(announcement),
        )
    return announcement


async def withdraw_announcement(
    db: AsyncSession, announcement_id: int
) -> Announcement:
    announcement = await _get_lifecycle_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    if announcement.status != AnnouncementStatus.PUBLISHED.value:
        raise ValueError("仅已发布公告可撤回")
    await _archive_publication(db, announcement, archived_at=now_utc())
    announcement.status = AnnouncementStatus.WITHDRAWN.value
    announcement.updated_at = now_utc()
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def delete_announcement(db: AsyncSession, announcement_id: int) -> bool:
    """Delete only a never-published draft.

    Publication snapshots are the audit record for an announcement.  A
    physical delete must therefore be refused as soon as the row has ever
    entered a publication lifecycle, even if a legacy database has an
    inconsistent ``status`` value (for example ``draft`` with a non-null
    ``published_at`` or a leftover snapshot).  Callers translate this
    ``ValueError`` to the existing HTTP 409 response.
    """
    announcement = await _get_lifecycle_announcement(db, announcement_id)
    if announcement is None:
        return False

    if announcement.status == AnnouncementStatus.PUBLISHED.value:
        raise ValueError("已发布公告不可删除，请先撤回")
    if announcement.status == AnnouncementStatus.WITHDRAWN.value:
        raise ValueError("已撤回公告不可删除，发布历史必须保留")
    if announcement.status != AnnouncementStatus.DRAFT.value:
        raise ValueError("公告状态不允许删除")
    if announcement.published_at is not None:
        raise ValueError("存在发布历史的公告不可删除")

    # Do not infer safety from status alone.  Old installations may have
    # partially migrated rows, and a snapshot is authoritative evidence that
    # this announcement was published at least once.
    has_publication_history = (
        await db.execute(
            select(AnnouncementPublicationHistory.id)
            .where(
                AnnouncementPublicationHistory.announcement_id
                == announcement.id
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None

    if has_publication_history:
        raise ValueError("存在发布历史的公告不可删除")

    await db.delete(announcement)
    await db.commit()
    return True


async def list_announcements(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    include_drafts: bool = False,
    limit: int = 100,
    offset: int = 0,
    page: int | None = None,
    per_page: int | None = None,
    unread_only: bool = False,
) -> list[tuple[Announcement, bool]]:
    """Return announcements in deterministic order.

    The original list-shaped return value is intentionally retained.  Passing
    ``page`` or ``per_page`` opts into offset pagination while preserving that
    return shape; HTTP callers that need metadata should use
    :func:`paginate_announcements`.
    """
    if page is not None or per_page is not None:
        current_page = _normalize_page(page)
        page_size = _normalize_page_size(per_page if per_page is not None else limit)
        result = await paginate_announcements(
            db,
            user_id=user_id,
            include_drafts=include_drafts,
            page=current_page,
            per_page=page_size,
            unread_only=unread_only,
        )
        return result.items

    rows = await _fetch_announcement_rows(
        db,
        user_id=user_id,
        include_drafts=include_drafts,
        limit=_normalize_legacy_limit(limit),
        offset=_normalize_offset(offset),
        unread_only=unread_only,
    )
    return rows


def _normalize_page(value: int | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _normalize_page_size(value: int | None) -> int:
    try:
        return max(1, min(int(value or 100), 100))
    except (TypeError, ValueError):
        return 100


def _normalize_legacy_limit(value: int | None) -> int:
    try:
        return max(1, min(int(value or 100), 500))
    except (TypeError, ValueError):
        return 100


def _normalize_offset(value: int | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _announcement_filters(
    *,
    user_id: int | None,
    include_drafts: bool,
    unread_only: bool,
) -> list[Any]:
    filters: list[Any] = []
    if not include_drafts:
        filters.append(
            Announcement.status == AnnouncementStatus.PUBLISHED.value
        )
    if unread_only and user_id is not None:
        read_exists = select(AnnouncementRead.id).where(
            AnnouncementRead.user_id == user_id,
            AnnouncementRead.announcement_id == Announcement.id,
        ).exists()
        filters.append(~read_exists)
    return filters


def _announcement_ordering() -> tuple[Any, ...]:
    """Keep equal timestamps deterministic across pages and database engines."""
    return (
        Announcement.published_at.desc(),
        Announcement.created_at.desc(),
        Announcement.id.desc(),
    )


async def _fetch_announcement_rows(
    db: AsyncSession,
    *,
    user_id: int | None,
    include_drafts: bool,
    limit: int,
    offset: int,
    unread_only: bool,
) -> list[tuple[Announcement, bool]]:
    query = select(Announcement).where(
        *_announcement_filters(
            user_id=user_id,
            include_drafts=include_drafts,
            unread_only=unread_only,
        )
    ).order_by(*_announcement_ordering())
    rows = (
        await db.execute(query.offset(offset).limit(limit))
    ).scalars().all()
    if user_id is None or not rows:
        return [(row, False) for row in rows]
    read_rows = (
        await db.execute(
            select(AnnouncementRead.announcement_id).where(
                AnnouncementRead.user_id == user_id,
                AnnouncementRead.announcement_id.in_([row.id for row in rows]),
            )
        )
    ).all()
    read_ids = {item[0] for item in read_rows}
    return [(row, row.id in read_ids) for row in rows]


async def paginate_announcements(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    include_drafts: bool = False,
    page: int = 1,
    per_page: int = 100,
    unread_only: bool = False,
) -> AnnouncementListPage:
    """Fetch one announcement page after applying all visibility filters.

    In particular, ``unread_only`` is part of the SQL predicate before
    ``OFFSET``/``LIMIT``.  Filtering a page after fetching it would make an
    older unread announcement disappear whenever newer rows fill the page.
    """
    page_size = _normalize_page_size(per_page)
    requested_page = _normalize_page(page)
    filters = _announcement_filters(
        user_id=user_id,
        include_drafts=include_drafts,
        unread_only=unread_only,
    )
    total_result = await db.execute(
        select(func.count(Announcement.id)).where(*filters)
    )
    total = int(total_result.scalar() or 0)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(requested_page, total_pages)
    rows = await _fetch_announcement_rows(
        db,
        user_id=user_id,
        include_drafts=include_drafts,
        limit=page_size,
        offset=(current_page - 1) * page_size,
        unread_only=unread_only,
    )
    return AnnouncementListPage(
        items=rows,
        total=total,
        page=current_page,
        per_page=page_size,
        total_pages=total_pages,
    )


async def unread_count(db: AsyncSession, user_id: int) -> int:
    read_exists = select(AnnouncementRead.id).where(
        AnnouncementRead.user_id == user_id,
        AnnouncementRead.announcement_id == Announcement.id,
    ).exists()
    result = await db.execute(
        select(func.count(Announcement.id)).where(
            Announcement.status == AnnouncementStatus.PUBLISHED.value,
            ~read_exists,
        )
    )
    return int(result.scalar() or 0)


def _is_announcement_read_conflict(error: IntegrityError) -> bool:
    """Return whether an integrity error is the read-marker unique race.

    ``begin_nested()`` below rolls back only the marker insert savepoint.  It
    is safe to turn the one expected duplicate into an idempotent result, but
    foreign-key, NOT NULL, and every unrelated constraint error must still be
    raised.  The checks cover the native error metadata used by PostgreSQL and
    MySQL as well as the diagnostic text emitted by SQLite.
    """
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(
        original, "pgcode", None
    )
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if str(constraint_name).lower() == "uq_announcement_read":
        return True

    errno = getattr(original, "errno", None)
    if errno is None:
        args = getattr(original, "args", ())
        if args and isinstance(args[0], int):
            errno = args[0]
    text = " ".join(
        part.lower()
        for part in (str(error), str(original))
        if part
    )
    # PostgreSQL's async drivers do not all expose ``diag`` consistently;
    # MySQL commonly reports only ``for key`` in the text.  Require either
    # our exact constraint name or both columns of the known unique key.  A
    # bare SQLSTATE/errno is deliberately insufficient because the savepoint
    # can also flush unrelated pending objects from the caller's session.
    if "uq_announcement_read" in text:
        return True
    if str(sqlstate) == "23505" or errno == 1062:
        return (
            "announcement_reads" in text
            and "announcement_id" in text
            and "user_id" in text
        )
    return (
        "announcement_reads" in text
        and "announcement_id" in text
        and "user_id" in text
        and any(
            marker in text
            for marker in ("unique", "duplicate", "already exists")
        )
    )


async def _insert_announcement_read(
    db: AsyncSession, user_id: int, announcement_id: int
) -> bool:
    """Insert one read marker and report whether this call created it.

    The unique key is the concurrency primitive.  The savepoint is important:
    a losing concurrent insert must not put the caller's outer session into a
    failed-transaction state, and it must not roll back unrelated work in that
    transaction.  Only the expected unique-key race is absorbed.
    """
    try:
        async with db.begin_nested():
            db.add(
                AnnouncementRead(
                    user_id=user_id,
                    announcement_id=announcement_id,
                )
            )
            await db.flush()
    except IntegrityError as exc:
        if not _is_announcement_read_conflict(exc):
            raise
        return False
    return True


async def mark_read(db: AsyncSession, user_id: int, announcement_id: int) -> bool:
    announcement = await get_announcement(db, announcement_id)
    if announcement is None or announcement.status != AnnouncementStatus.PUBLISHED.value:
        return False
    existing = (
        await db.execute(
            select(AnnouncementRead.id).where(
                AnnouncementRead.user_id == user_id,
                AnnouncementRead.announcement_id == announcement_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None and await _insert_announcement_read(
        db, user_id, announcement_id
    ):
        await db.commit()
    return True


async def mark_all_read(db: AsyncSession, user_id: int) -> int:
    announcements = (
        await db.execute(
            select(Announcement.id).where(
                Announcement.status == AnnouncementStatus.PUBLISHED.value
            )
        )
    ).all()
    existing = (
        await db.execute(
            select(AnnouncementRead.announcement_id).where(
                AnnouncementRead.user_id == user_id
            )
        )
    ).all()
    existing_ids = {item[0] for item in existing}
    new_count = 0
    for (announcement_id,) in announcements:
        if announcement_id not in existing_ids and await _insert_announcement_read(
            db, user_id, announcement_id
        ):
            new_count += 1
    if new_count:
        await db.commit()
    return new_count


async def delivery_stats(
    db: AsyncSession, announcement_id: int
) -> AnnouncementDeliveryStats:
    announcement = await get_announcement(db, announcement_id)
    current_version = _publication_version(announcement) if announcement else 1
    rows = (
        await db.execute(
            select(NotificationDelivery.status, func.count(NotificationDelivery.id))
            .where(NotificationDelivery.announcement_id == announcement_id)
            .where(NotificationDelivery.publication_version == current_version)
            .group_by(NotificationDelivery.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    # A withdrawn announcement may be edited as a draft before it is
    # republished.  In that state the old snapshot has the same publication
    # version as the current row and must remain visible; only an active,
    # still-current publication is intentionally omitted from the archive.
    history_filter = AnnouncementPublicationHistory.announcement_id == announcement_id
    history_filter = history_filter & (
        (AnnouncementPublicationHistory.publication_version != current_version)
        | AnnouncementPublicationHistory.archived_at.is_not(None)
    )
    history_rows = (
        await db.execute(
            select(AnnouncementPublicationHistory)
            .where(history_filter)
            .order_by(AnnouncementPublicationHistory.publication_version.desc())
        )
    ).scalars().all()
    history_ids = [row.id for row in history_rows]
    history_counts: dict[tuple[int, str], int] = {}
    if history_ids:
        grouped = (
            await db.execute(
                select(
                    AnnouncementDeliveryHistory.publication_id,
                    AnnouncementDeliveryHistory.status,
                    func.count(AnnouncementDeliveryHistory.id),
                )
                .where(
                    AnnouncementDeliveryHistory.publication_id.in_(history_ids)
                )
                .group_by(
                    AnnouncementDeliveryHistory.publication_id,
                    AnnouncementDeliveryHistory.status,
                )
            )
        ).all()
        history_counts = {
            (int(publication_id), str(status)): int(count)
            for publication_id, status, count in grouped
        }

    history: list[dict[str, Any]] = []
    for snapshot in history_rows:
        history.append(
            {
                "id": snapshot.id,
                "publication_version": snapshot.publication_version,
                "title": snapshot.title,
                "content": snapshot.content,
                "content_html": sanitize_markdown(snapshot.content),
                "type": snapshot.announcement_type,
                "published_at": snapshot.published_at.isoformat()
                if snapshot.published_at
                else None,
                "archived_at": snapshot.archived_at.isoformat()
                if snapshot.archived_at
                else None,
                "pending": history_counts.get(
                    (snapshot.id, DeliveryStatus.PENDING.value), 0
                ),
                "sent": history_counts.get(
                    (snapshot.id, DeliveryStatus.SENT.value), 0
                ),
                "failed": history_counts.get(
                    (snapshot.id, DeliveryStatus.FAILED.value), 0
                ),
            }
        )
    return AnnouncementDeliveryStats(
        pending=counts.get(DeliveryStatus.PENDING.value, 0),
        sent=counts.get(DeliveryStatus.SENT.value, 0),
        failed=counts.get(DeliveryStatus.FAILED.value, 0),
        history=tuple(history),
    )


async def create_release_announcement(
    db: AsyncSession,
    *,
    version: str,
    notes: str,
    created_by: int | None = None,
    publish: bool = False,
) -> Announcement:
    """Create a release announcement using the same lifecycle as manual posts."""
    announcement = await create_announcement(
        db,
        title=f"Sakura AI {version}",
        content=notes,
        announcement_type=AnnouncementType.RELEASE.value,
        created_by=created_by,
    )
    if publish:
        announcement = await publish_announcement(db, announcement.id)
    return announcement


class AnnouncementService:
    """Object-oriented facade retained for integrations that prefer services."""

    create = staticmethod(create_announcement)
    update = staticmethod(update_announcement)
    get = staticmethod(get_announcement)
    publish = staticmethod(publish_announcement)
    withdraw = staticmethod(withdraw_announcement)
    delete = staticmethod(delete_announcement)
    list = staticmethod(list_announcements)
    paginate = staticmethod(paginate_announcements)
    unread_count = staticmethod(unread_count)
    mark_read = staticmethod(mark_read)
    mark_all_read = staticmethod(mark_all_read)
    delivery_stats = staticmethod(delivery_stats)


__all__ = [
    "AnnouncementDeliveryStats",
    "AnnouncementListPage",
    "AnnouncementService",
    "announcement_to_dict",
    "create_announcement",
    "create_release_announcement",
    "delete_announcement",
    "delivery_stats",
    "get_announcement",
    "list_announcements",
    "mark_all_read",
    "mark_read",
    "paginate_announcements",
    "publish_announcement",
    "render_markdown_safe",
    "sanitize_markdown",
    "schedule_announcement_broadcast",
    "unread_count",
    "update_announcement",
    "withdraw_announcement",
]
