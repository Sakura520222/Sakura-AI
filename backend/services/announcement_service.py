"""Announcement lifecycle, safe Markdown rendering and read-state helpers."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import now_utc
from backend.models.announcement_models import (
    Announcement,
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

    def inline(value: str) -> str:
        escaped = html.escape(value, quote=False)

        def link(match: re.Match[str]) -> str:
            label = match.group(1)
            href = _safe_href(match.group(2))
            return (
                f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
                f"{label}</a>"
            )

        escaped = _MARKDOWN_LINK_RE.sub(link, escaped)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
        return escaped

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
            output.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif unordered:
            if in_ol:
                output.append("</ol>")
                in_ol = False
            if not in_ul:
                output.append("<ul>")
                in_ul = True
            output.append(f"<li>{inline(unordered.group(1))}</li>")
        elif ordered:
            if in_ul:
                output.append("</ul>")
                in_ul = False
            if not in_ol:
                output.append("<ol>")
                in_ol = True
            output.append(f"<li>{inline(ordered.group(1))}</li>")
        elif not line.strip():
            close_lists()
            output.append("<br>")
        else:
            close_lists()
            output.append(f"<p>{inline(line)}</p>")
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

    def inline(value: str) -> str:
        escaped = html.escape(value, quote=False)

        def link(match: re.Match[str]) -> str:
            label = match.group(1)
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
            return f'<a href="{html.escape(candidate, quote=True)}">{label}</a>'

        escaped = _MARKDOWN_LINK_RE.sub(link, escaped)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(
            r"\*\*([^*\n]+)\*\*|__([^_\n]+)__",
            lambda m: f"<b>{m.group(1) or m.group(2)}</b>",
            escaped,
        )
        escaped = re.sub(
            r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)",
            lambda m: f"<i>{m.group(1) or m.group(2)}</i>",
            escaped,
        )
        return escaped

    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if heading:
            ordered_counter = 0
            lines.append(f"<b>{inline(heading.group(2))}</b>")
        elif unordered:
            ordered_counter = 0
            lines.append(f"• {inline(unordered.group(1))}")
        elif ordered:
            ordered_counter += 1
            lines.append(f"{ordered_counter}. {inline(ordered.group(1))}")
        elif not line.strip():
            lines.append("")
        else:
            ordered_counter = 0
            lines.append(inline(line))
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class AnnouncementDeliveryStats:
    pending: int
    sent: int
    failed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "pending": self.pending,
            "sent": self.sent,
            "failed": self.failed,
        }


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
    db.add(announcement)
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def update_announcement(
    db: AsyncSession,
    announcement_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    announcement_type: str | None = None,
) -> Announcement:
    announcement = await get_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    if announcement.status == AnnouncementStatus.PUBLISHED.value:
        raise ValueError("已发布公告不可直接修改，请先撤回")
    if title is not None:
        title = str(title).strip()
        if not title or len(title) > 500:
            raise ValueError("公告标题不能为空且不得超过 500 个字符")
        announcement.title = title
    if content is not None:
        content = str(content).strip()
        if not content:
            raise ValueError("公告内容不能为空")
        announcement.content = content
    if announcement_type is not None:
        announcement.announcement_type = _validate_type(announcement_type)
    announcement.updated_at = now_utc()
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def get_announcement(
    db: AsyncSession, announcement_id: int
) -> Announcement | None:
    return (
        await db.execute(select(Announcement).where(Announcement.id == announcement_id))
    ).scalar_one_or_none()


async def _ensure_delivery_rows(db: AsyncSession, announcement_id: int) -> None:
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
    existing_keys = {(row.user_id, row.channel) for row in existing}
    for user in users:
        key = (user.id, "web")
        if key not in existing_keys:
            db.add(
                NotificationDelivery(
                    announcement_id=announcement_id,
                    user_id=user.id,
                    channel="web",
                    status=DeliveryStatus.PENDING.value,
                )
            )
            existing_keys.add(key)
    for endpoint in endpoints:
        channel = str(endpoint.provider).lower()
        if channel not in {"email", "telegram"} or endpoint.user_id not in active_user_ids:
            continue
        key = (endpoint.user_id, channel)
        if key in existing_keys:
            continue
        db.add(
            NotificationDelivery(
                announcement_id=announcement_id,
                user_id=endpoint.user_id,
                channel=channel,
                status=DeliveryStatus.PENDING.value,
            )
        )
        existing_keys.add(key)


async def _broadcast_in_background(announcement_id: int) -> None:
    from backend.models import database as db_module
    from backend.services.notification_service import notification_service

    if db_module.async_session is None:
        logger.warning("公告广播跳过：数据库会话尚未初始化")
        return
    try:
        async with db_module.async_session() as session:
            await notification_service.broadcast_announcement(session, announcement_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Delivery rows retain failure state for the admin statistics/retry API.
        logger.exception("公告异步广播失败: announcement_id={}", announcement_id)


def schedule_announcement_broadcast(announcement_id: int) -> asyncio.Task | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("公告广播未调度：当前没有运行中的事件循环")
        return None
    del loop
    try:
        return create_registered_background_task(
            _broadcast_in_background(announcement_id), "announcement.broadcast"
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
    announcement = await get_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    transitioned = False
    if announcement.status != AnnouncementStatus.PUBLISHED.value:
        announcement.status = AnnouncementStatus.PUBLISHED.value
        announcement.published_at = now_utc()
        announcement.updated_at = now_utc()
        transitioned = True
        await db.flush()
        await _ensure_delivery_rows(db, announcement.id)
        await db.commit()
        await db.refresh(announcement)
    if schedule_broadcast and transitioned:
        schedule_announcement_broadcast(int(announcement.id))
    return announcement


async def withdraw_announcement(
    db: AsyncSession, announcement_id: int
) -> Announcement:
    announcement = await get_announcement(db, announcement_id)
    if announcement is None:
        raise LookupError("公告不存在")
    if announcement.status != AnnouncementStatus.PUBLISHED.value:
        raise ValueError("仅已发布公告可撤回")
    announcement.status = AnnouncementStatus.WITHDRAWN.value
    announcement.updated_at = now_utc()
    await db.commit()
    await db.refresh(announcement)
    return announcement


async def delete_announcement(db: AsyncSession, announcement_id: int) -> bool:
    """Delete a draft; published history is retained and must be withdrawn."""
    announcement = await get_announcement(db, announcement_id)
    if announcement is None:
        return False
    if announcement.status == AnnouncementStatus.PUBLISHED.value:
        raise ValueError("已发布公告不可删除，请先撤回")
    await db.delete(announcement)
    await db.commit()
    return True


async def list_announcements(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    include_drafts: bool = False,
    limit: int = 100,
) -> list[tuple[Announcement, bool]]:
    query = select(Announcement).order_by(
        Announcement.published_at.desc(), Announcement.created_at.desc()
    )
    if not include_drafts:
        query = query.where(Announcement.status == AnnouncementStatus.PUBLISHED.value)
    rows = (await db.execute(query.limit(max(1, min(limit, 500))))).scalars().all()
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


async def unread_count(db: AsyncSession, user_id: int) -> int:
    read = select(AnnouncementRead.announcement_id).where(
        AnnouncementRead.user_id == user_id
    )
    result = await db.execute(
        select(func.count(Announcement.id)).where(
            Announcement.status == AnnouncementStatus.PUBLISHED.value,
            ~Announcement.id.in_(read),
        )
    )
    return int(result.scalar() or 0)


async def mark_read(db: AsyncSession, user_id: int, announcement_id: int) -> bool:
    announcement = await get_announcement(db, announcement_id)
    if announcement is None or announcement.status != AnnouncementStatus.PUBLISHED.value:
        return False
    existing = (
        await db.execute(
            select(AnnouncementRead).where(
                AnnouncementRead.user_id == user_id,
                AnnouncementRead.announcement_id == announcement_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(AnnouncementRead(user_id=user_id, announcement_id=announcement_id))
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
        if announcement_id not in existing_ids:
            db.add(AnnouncementRead(user_id=user_id, announcement_id=announcement_id))
            new_count += 1
    if new_count:
        await db.commit()
    return new_count


async def delivery_stats(
    db: AsyncSession, announcement_id: int
) -> AnnouncementDeliveryStats:
    rows = (
        await db.execute(
            select(NotificationDelivery.status, func.count(NotificationDelivery.id))
            .where(NotificationDelivery.announcement_id == announcement_id)
            .group_by(NotificationDelivery.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    return AnnouncementDeliveryStats(
        pending=counts.get(DeliveryStatus.PENDING.value, 0),
        sent=counts.get(DeliveryStatus.SENT.value, 0),
        failed=counts.get(DeliveryStatus.FAILED.value, 0),
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
    unread_count = staticmethod(unread_count)
    mark_read = staticmethod(mark_read)
    mark_all_read = staticmethod(mark_all_read)
    delivery_stats = staticmethod(delivery_stats)


__all__ = [
    "AnnouncementDeliveryStats",
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
    "publish_announcement",
    "render_markdown_safe",
    "sanitize_markdown",
    "schedule_announcement_broadcast",
    "unread_count",
    "update_announcement",
    "withdraw_announcement",
]
