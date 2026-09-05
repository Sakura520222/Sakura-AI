"""Regression coverage for reachable announcement pages and archived rounds."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api.v1 import announcements as announcement_routes
from backend.models import Base
from backend.models.announcement_models import (
    Announcement,
    AnnouncementPublicationHistory,
    AnnouncementRead,
    DeliveryStatus,
)
from backend.models.telegram_models import TelegramUser
from backend.services import announcement_service


class _AsyncSQLiteSession:
    """Small async-shaped facade backed by real SQLite for service tests."""

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    def add(self, value):
        self._session.add(value)

    async def commit(self):
        self._session.commit()

    async def flush(self):
        self._session.flush()

    async def refresh(self, value):
        self._session.refresh(value)


@pytest.fixture
def sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield _AsyncSQLiteSession(session)
    finally:
        session.close()
        engine.dispose()


def _published(
    announcement_id: int,
    *,
    published_at,
    content: str | None = None,
) -> Announcement:
    return Announcement(
        id=announcement_id,
        title=f"Announcement {announcement_id}",
        content=content or f"Body {announcement_id}",
        status="published",
        published_at=published_at,
        created_at=published_at,
        updated_at=published_at,
    )


@pytest.mark.asyncio
async def test_unread_filter_is_applied_before_pagination_and_ids_break_ties(
    sqlite_session,
):
    db = sqlite_session
    user = TelegramUser(id=1, telegram_id=1001, is_active=True)
    db.add(user)
    published_at = announcement_service.now_utc()
    for announcement_id in range(1, 6):
        db.add(_published(announcement_id, published_at=published_at))
    # The two newest rows are read.  The remaining three must form the
    # unread result set before OFFSET/LIMIT are applied.
    db.add(AnnouncementRead(user_id=1, announcement_id=5))
    db.add(AnnouncementRead(user_id=1, announcement_id=4))
    await db.commit()

    first = await announcement_service.paginate_announcements(
        db, user_id=1, page=1, per_page=2, unread_only=True
    )
    second = await announcement_service.paginate_announcements(
        db, user_id=1, page=2, per_page=2, unread_only=True
    )

    assert [row.id for row, _read in first.items] == [3, 2]
    assert [row.id for row, _read in second.items] == [1]
    assert first.total == 3
    assert first.total_pages == 2
    assert all(not read for _row, read in first.items + second.items)


@pytest.mark.asyncio
async def test_page_bounds_are_clamped_and_legacy_list_limit_remains_compatible(
    sqlite_session,
):
    db = sqlite_session
    published_at = announcement_service.now_utc()
    for announcement_id in range(1, 4):
        db.add(_published(announcement_id, published_at=published_at))
    await db.commit()

    page = await announcement_service.paginate_announcements(
        db, page=999, per_page=2
    )
    assert page.page == 2
    assert page.total_pages == 2
    assert [row.id for row, _read in page.items] == [1]

    legacy_rows = await announcement_service.list_announcements(
        db, limit=2, offset=1
    )
    assert [row.id for row, _read in legacy_rows] == [2, 1]


@pytest.mark.asyncio
async def test_archived_current_round_is_returned_with_safe_history_body(sqlite_session):
    db = sqlite_session
    published_at = announcement_service.now_utc()
    announcement = _published(
        1,
        published_at=published_at,
        content="Old **body** <script>alert(1)</script>",
    )
    db.add(announcement)
    await db.flush()
    db.add(
        AnnouncementPublicationHistory(
            announcement_id=1,
            publication_version=1,
            title="Old title",
            content=announcement.content,
            announcement_type="general",
            published_at=published_at,
            archived_at=published_at + timedelta(seconds=1),
            delivery_states="[]",
        )
    )
    await db.commit()

    stats = await announcement_service.delivery_stats(db, 1)
    history = stats.as_dict()["history"]
    assert len(history) == 1
    assert history[0]["publication_version"] == 1
    assert history[0]["content"] == announcement.content
    assert "<script>" not in history[0]["content_html"]
    assert "<strong>body</strong>" in history[0]["content_html"]


def test_announcement_pages_and_guide_expose_the_new_contract():
    root = Path(__file__).resolve().parents[1]
    user_page = (root / "backend/webui/templates/announcements.html").read_text()
    admin_page = (root / "backend/webui/templates/announcements_admin.html").read_text()
    guide = (root / "docs/TELEGRAM_SETUP.md").read_text()

    assert "page={{ page + 1 }}" in user_page
    assert "page={{ page + 1 }}" in admin_page
    assert "round.content_html | safe" in admin_page
    assert "/start" in guide and "/bind" in guide
    assert "/status" not in guide
    assert "/user_add" not in guide
    assert "TELEGRAM_ADMIN_USER_IDS" not in guide


@pytest.mark.asyncio
async def test_admin_retry_loads_an_old_announcement_directly(monkeypatch):
    target = _published(100, published_at=announcement_service.now_utc())
    called = []

    async def fake_get(_db, announcement_id):
        called.append(announcement_id)
        return target if announcement_id == target.id else None

    def fake_schedule(announcement_id, **kwargs):
        called.append((announcement_id, kwargs["expected_version"]))

    async def fail_list(*_args, **_kwargs):
        raise AssertionError("retry must not be limited by the first list page")

    monkeypatch.setattr(announcement_routes, "get_announcement", fake_get)
    monkeypatch.setattr(
        announcement_routes, "schedule_announcement_broadcast", fake_schedule
    )
    monkeypatch.setattr(
        announcement_routes, "list_announcements", fail_list, raising=False
    )

    result = await announcement_routes.admin_retry_announcement(
        target.id,
        user={"user_id": 1},
        db=object(),
    )
    assert result == {"ok": True, "scheduled": True}
    assert called == [100, (100, 1)]


def test_delivery_status_enum_is_used_by_history_contract():
    assert DeliveryStatus.PENDING.value == "pending"
