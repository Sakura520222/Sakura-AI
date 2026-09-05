"""Regression tests for announcement deletion and read-marker races."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models import Base
from backend.models.announcement_models import (
    Announcement,
    AnnouncementPublicationHistory,
    AnnouncementRead,
)
from backend.models.telegram_models import TelegramUser
from backend.services import announcement_service


class _AsyncNestedTransaction:
    """Adapt a synchronous SQLAlchemy savepoint to an async-shaped session."""

    def __init__(self, transaction):
        self._transaction = transaction

    async def __aenter__(self):
        self._transaction.__enter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return self._transaction.__exit__(exc_type, exc_value, traceback)


class _AsyncSQLiteSession:
    """Use real SQLite while exercising the service's async session contract."""

    def __init__(self, session: Session, *, barrier: threading.Barrier | None = None):
        self._session = session
        self._barrier = barrier

    def add(self, value):
        self._session.add(value)

    async def execute(self, statement, *args, **kwargs):
        result = self._session.execute(statement, *args, **kwargs)
        if (
            self._barrier is not None
            and "announcement_reads" in str(statement).lower()
        ):
            self._barrier.wait(timeout=10)
        return result

    async def commit(self):
        self._session.commit()

    async def flush(self):
        self._session.flush()

    async def refresh(self, value):
        self._session.refresh(value)

    async def delete(self, value):
        self._session.delete(value)

    def begin_nested(self):
        return _AsyncNestedTransaction(self._session.begin_nested())


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'announcement-integrity.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_published(engine, *, announcement_count: int = 1) -> None:
    with Session(engine) as session:
        session.add(TelegramUser(id=1, telegram_id=1001, is_active=True))
        for announcement_id in range(1, announcement_count + 1):
            session.add(
                Announcement(
                    id=announcement_id,
                    title=f"Announcement {announcement_id}",
                    content="Body",
                    status="published",
                    published_at=now_utc(),
                )
            )
        session.commit()


def test_delete_only_allows_never_published_draft_and_preserves_history(
    sqlite_engine,
):
    with Session(sqlite_engine) as session:
        draft = Announcement(id=1, title="Draft", content="Body", status="draft")
        withdrawn = Announcement(
            id=2,
            title="Withdrawn",
            content="Old body",
            status="withdrawn",
            published_at=now_utc(),
        )
        inconsistent = Announcement(
            id=3,
            title="Inconsistent",
            content="Body",
            status="draft",
        )
        session.add_all([draft, withdrawn, inconsistent])
        session.flush()
        snapshot = AnnouncementPublicationHistory(
            announcement_id=withdrawn.id,
            publication_version=1,
            title=withdrawn.title,
            content=withdrawn.content,
            announcement_type="general",
            published_at=withdrawn.published_at,
            archived_at=now_utc(),
        )
        inconsistent_snapshot = AnnouncementPublicationHistory(
            announcement_id=inconsistent.id,
            publication_version=1,
            title=inconsistent.title,
            content=inconsistent.content,
            announcement_type="general",
            published_at=now_utc(),
        )
        session.add_all([snapshot, inconsistent_snapshot])
        session.commit()

    async def run():
        with Session(sqlite_engine) as sync_session:
            db = _AsyncSQLiteSession(sync_session)
            assert await announcement_service.delete_announcement(db, 1) is True
            with pytest.raises(ValueError, match="已撤回公告不可删除"):
                await announcement_service.delete_announcement(db, 2)
            with pytest.raises(ValueError, match="存在发布历史"):
                await announcement_service.delete_announcement(db, 3)

    asyncio.run(run())

    with Session(sqlite_engine) as session:
        assert session.get(Announcement, 1) is None
        assert session.get(Announcement, 2) is not None
        assert session.get(Announcement, 3) is not None
        assert session.scalar(
            select(AnnouncementPublicationHistory.id).where(
                AnnouncementPublicationHistory.announcement_id == 2
            )
        ) is not None


def _mark_read_in_thread(engine, barrier: threading.Barrier) -> bool:
    with Session(engine) as session:
        db = _AsyncSQLiteSession(session, barrier=barrier)
        result = asyncio.run(announcement_service.mark_read(db, 1, 1))
        # The losing transaction must remain usable after its savepoint was
        # rolled back; this query is intentionally made before closing it.
        session.scalar(select(AnnouncementRead.id))
        return result


def _mark_all_read_in_thread(engine, barrier: threading.Barrier) -> int:
    with Session(engine) as session:
        db = _AsyncSQLiteSession(session, barrier=barrier)
        result = asyncio.run(announcement_service.mark_all_read(db, 1))
        session.scalar(select(AnnouncementRead.id))
        return result


def test_mark_read_real_sqlite_concurrent_unique_race_is_idempotent(sqlite_engine):
    _seed_published(sqlite_engine)
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _value: _mark_read_in_thread(sqlite_engine, barrier),
                (1, 2),
            )
        )

    assert results == [True, True]
    with Session(sqlite_engine) as session:
        assert session.scalar(select(AnnouncementRead.id)) is not None
        assert session.query(AnnouncementRead).count() == 1


def test_mark_all_read_reports_only_rows_created_by_this_call(sqlite_engine):
    _seed_published(sqlite_engine, announcement_count=3)
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _value: _mark_all_read_in_thread(sqlite_engine, barrier),
                (1, 2),
            )
        )

    # SQLite may let either concurrent transaction insert a non-empty subset;
    # the service contract is that the per-call counts account for every row,
    # not that one particular thread wins the entire batch.
    assert all(result >= 0 for result in results)
    assert sum(results) == 3
    with Session(sqlite_engine) as session:
        assert session.query(AnnouncementRead).count() == 3


def test_unrelated_integrity_error_is_not_swallowed_and_session_recovers(
    sqlite_engine,
):
    _seed_published(sqlite_engine)

    async def run():
        with Session(sqlite_engine) as sync_session:
            db = _AsyncSQLiteSession(sync_session)
            with pytest.raises(IntegrityError):
                await announcement_service.mark_read(db, 999, 1)
            assert sync_session.scalar(select(Announcement.id)) == 1

    asyncio.run(run())


def test_non_target_postgres_unique_constraint_is_not_swallowed():
    original = SimpleNamespace(
        sqlstate="23505",
        diag=SimpleNamespace(constraint_name="uq_unrelated"),
    )
    error = IntegrityError("INSERT", {}, original)
    assert announcement_service._is_announcement_read_conflict(error) is False
