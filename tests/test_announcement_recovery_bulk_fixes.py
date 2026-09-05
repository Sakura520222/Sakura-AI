"""Regression coverage for durable announcement recovery and bulk admin data."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.time_service import now_utc
from backend.models import Base
from backend.models import database as database_module
from backend.models.announcement_models import (
    Announcement,
    AnnouncementDeliveryHistory,
    AnnouncementPublicationHistory,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.telegram_models import TelegramUser
from backend.services import announcement_service, notification_service


class _AsyncSQLiteSession:
    """Async-shaped adapter backed by a synchronous SQLite session."""

    def __init__(self, session: Session):
        self._session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self._session.close()

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def get(self, model, row_id, **kwargs):
        return self._session.get(model, row_id, **kwargs)

    async def commit(self):
        self._session.commit()

    async def flush(self):
        self._session.flush()

    async def rollback(self):
        self._session.rollback()


class _CountingAsyncSQLiteSession(_AsyncSQLiteSession):
    def __init__(self, session: Session):
        super().__init__(session)
        self.execute_count = 0

    async def execute(self, statement, *args, **kwargs):
        self.execute_count += 1
        return await super().execute(statement, *args, **kwargs)


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_stats(engine, count: int) -> None:
    timestamp = now_utc()
    with Session(engine) as session:
        for announcement_id in range(1, count + 1):
            is_draft = announcement_id == 1
            announcement = Announcement(
                id=announcement_id,
                title=f"Announcement {announcement_id}",
                content=f"Body {announcement_id}",
                status="draft" if is_draft else "published",
                published_at=None if is_draft else timestamp,
                publication_version=1,
            )
            session.add(announcement)
            session.add_all(
                [
                    NotificationDelivery(
                        announcement_id=announcement_id,
                        user_id=announcement_id,
                        channel="web",
                        status=DeliveryStatus.PENDING.value,
                        publication_version=1,
                    ),
                    NotificationDelivery(
                        announcement_id=announcement_id,
                        user_id=count + announcement_id,
                        channel="web",
                        status=DeliveryStatus.SENT.value,
                        publication_version=1,
                    ),
                ]
            )
            if not is_draft:
                snapshot = AnnouncementPublicationHistory(
                    announcement_id=announcement_id,
                    publication_version=1,
                    title=announcement.title,
                    content=announcement.content,
                    announcement_type="general",
                    published_at=timestamp,
                    archived_at=timestamp,
                )
                session.add(snapshot)
                session.flush()
                session.add(
                    AnnouncementDeliveryHistory(
                        publication_id=snapshot.id,
                        user_id=announcement_id,
                        channel="web",
                        status=DeliveryStatus.SENT.value,
                    )
                )
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 10, 100])
async def test_delivery_stats_many_is_constant_query_count_and_matches_scalar(
    sqlite_engine, count
):
    _seed_stats(sqlite_engine, count)
    with Session(sqlite_engine) as sync_session:
        announcements = sync_session.scalars(
            select(Announcement).order_by(Announcement.id)
        ).all()
        db = _CountingAsyncSQLiteSession(sync_session)
        stats = await announcement_service.delivery_stats_many(db, announcements)
        assert len(stats) == count
        # Current counts, snapshots, and archived counts are the only bulk
        # statistics queries regardless of page size.
        assert db.execute_count <= 3
        assert stats.deletable_ids == frozenset({1})

        expected = await announcement_service.delivery_stats(db, count)
        assert stats[count].as_dict() == expected.as_dict()


@pytest.mark.asyncio
async def test_recovery_drains_only_current_pending_rows(sqlite_engine, monkeypatch):
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add_all(
            [
                Announcement(
                    id=1,
                    title="Current",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=1,
                ),
                Announcement(
                    id=2,
                    title="Failed",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=1,
                ),
                Announcement(
                    id=3,
                    title="Withdrawn",
                    content="Body",
                    status="withdrawn",
                    published_at=timestamp,
                    publication_version=1,
                ),
                Announcement(
                    id=4,
                    title="Draft",
                    content="Body",
                    status="draft",
                    publication_version=1,
                ),
                Announcement(
                    id=5,
                    title="Republished",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=2,
                ),
                Announcement(
                    id=6,
                    title="Sent",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=1,
                ),
            ]
        )
        session.add_all(
            [
                NotificationDelivery(
                    id=1,
                    announcement_id=1,
                    user_id=1,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=2,
                    announcement_id=2,
                    user_id=2,
                    channel="web",
                    status="failed",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=3,
                    announcement_id=3,
                    user_id=3,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=4,
                    announcement_id=4,
                    user_id=4,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=5,
                    announcement_id=5,
                    user_id=5,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=6,
                    announcement_id=6,
                    user_id=6,
                    channel="web",
                    status="sent",
                    publication_version=1,
                ),
            ]
        )
        session.commit()

    def factory():
        return _AsyncSQLiteSession(Session(sqlite_engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    calls: list[tuple[int, int | None, bool]] = []

    async def fake_broadcast(
        db,
        announcement_id,
        *,
        expected_version=None,
        pending_only=False,
    ):
        calls.append((announcement_id, expected_version, pending_only))
        result = await db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.announcement_id == announcement_id,
                NotificationDelivery.publication_version == expected_version,
                NotificationDelivery.status == DeliveryStatus.PENDING.value,
            )
            .values(status=DeliveryStatus.SENT.value)
        )
        await db.commit()
        return {"sent": result.rowcount, "failed": 0, "skipped": 0}

    monkeypatch.setattr(
        notification_service.notification_service,
        "broadcast_announcement",
        fake_broadcast,
    )
    await announcement_service.recover_pending_announcement_deliveries()

    assert calls == [(1, 1, True)]
    with Session(sqlite_engine) as session:
        assert session.get(NotificationDelivery, 1).status == "sent"
        assert session.get(NotificationDelivery, 2).status == "failed"
        assert session.get(NotificationDelivery, 3).status == "pending"
        assert session.get(NotificationDelivery, 4).status == "pending"
        assert session.get(NotificationDelivery, 5).status == "pending"
        assert session.get(NotificationDelivery, 6).status == "sent"


@pytest.mark.asyncio
async def test_pending_only_broadcast_does_not_retry_failed_rows(
    sqlite_engine, monkeypatch
):
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add(TelegramUser(id=1, is_active=True))
        session.add(
            Announcement(
                id=1,
                title="Current",
                content="Body",
                status="published",
                published_at=timestamp,
                publication_version=1,
            )
        )
        session.add_all(
            [
                NotificationDelivery(
                    id=1,
                    announcement_id=1,
                    user_id=1,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
                NotificationDelivery(
                    id=2,
                    announcement_id=1,
                    user_id=1,
                    channel="email",
                    status="failed",
                    publication_version=1,
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(database_module, "async_session", None)
    provider = type(
        "_WebProvider",
        (),
        {"channel": "web", "send": lambda self, **_kwargs: asyncio.sleep(0)},
    )()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    with Session(sqlite_engine) as sync_session:
        db = _AsyncSQLiteSession(sync_session)
        result = await service.broadcast_announcement(
            db,
            1,
            expected_version=1,
            pending_only=True,
        )
        assert result == {"sent": 1, "failed": 0, "skipped": 0}

    with Session(sqlite_engine) as session:
        assert session.get(NotificationDelivery, 1).status == "sent"
        assert session.get(NotificationDelivery, 2).status == "failed"


@pytest.mark.asyncio
async def test_pending_only_claim_rejects_pending_to_failed_race(
    sqlite_engine, monkeypatch
):
    """A stale recovery snapshot must not send a row changed to FAILED."""
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add(TelegramUser(id=1, is_active=True))
        session.add(
            Announcement(
                id=1,
                title="Current",
                content="Body",
                status="published",
                published_at=timestamp,
                publication_version=1,
            )
        )
        session.add(
            NotificationDelivery(
                id=1,
                announcement_id=1,
                user_id=1,
                channel="web",
                status=DeliveryStatus.PENDING.value,
                publication_version=1,
            )
        )
        session.commit()
        stale_announcement = session.get(Announcement, 1)
        stale_delivery = session.get(NotificationDelivery, 1)
        session.expunge(stale_announcement)
        session.expunge(stale_delivery)

    # Simulate a competing worker finishing the same row after recovery's
    # initial SELECT but before recovery performs its claim CAS.
    with Session(sqlite_engine) as competing:
        competing.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == 1)
            .values(status=DeliveryStatus.FAILED.value)
        )
        competing.commit()

    monkeypatch.setattr(database_module, "async_session", None)
    send_calls = 0

    class _Provider:
        channel = "web"

        async def send(self, **_kwargs):
            nonlocal send_calls
            send_calls += 1

    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": _Provider()})
    )
    with Session(sqlite_engine) as sync_session:
        db = _AsyncSQLiteSession(sync_session)
        result = await service._deliver_one(
            db,
            stale_delivery,
            stale_announcement,
            None,
            asyncio.Lock(),
            expected_version=1,
            allowed_statuses=(DeliveryStatus.PENDING.value,),
        )

    assert result is None
    assert send_calls == 0
    # The ordinary/manual path intentionally retains FAILED claiming; only
    # the recovery marker narrows the compare-and-set status predicate.
    with Session(sqlite_engine) as sync_session:
        db = _AsyncSQLiteSession(sync_session)
        manual_token = await service._claim_delivery(
            db,
            stale_delivery,
            stale_announcement,
            expected_version=1,
        )
        assert manual_token
    with Session(sqlite_engine) as session:
        assert session.get(NotificationDelivery, 1).status == DeliveryStatus.FAILED.value


@pytest.mark.asyncio
async def test_two_recovery_tasks_share_the_delivery_claim(sqlite_engine, monkeypatch):
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add(TelegramUser(id=1, is_active=True))
        session.add(
            Announcement(
                id=1,
                title="Current",
                content="Body",
                status="published",
                published_at=timestamp,
                publication_version=1,
            )
        )
        session.add(
            NotificationDelivery(
                id=1,
                announcement_id=1,
                user_id=1,
                channel="web",
                status="pending",
                publication_version=1,
            )
        )
        session.commit()

    def factory():
        return _AsyncSQLiteSession(Session(sqlite_engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    send_lock = asyncio.Lock()
    send_count = 0

    async def fake_broadcast(db, announcement_id, **_kwargs):
        nonlocal send_count
        async with send_lock:
            row = await db.get(NotificationDelivery, 1)
            if row.status != DeliveryStatus.PENDING.value:
                return {"sent": 0, "failed": 0, "skipped": 1}
            send_count += 1
            await db.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.id == 1,
                    NotificationDelivery.status == DeliveryStatus.PENDING.value,
                )
                .values(status=DeliveryStatus.SENT.value)
            )
            await db.commit()
            return {"sent": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(
        notification_service.notification_service,
        "broadcast_announcement",
        fake_broadcast,
    )
    await asyncio.gather(
        announcement_service.recover_pending_announcement_deliveries(),
        announcement_service.recover_pending_announcement_deliveries(),
    )

    assert send_count == 1
    with Session(sqlite_engine) as session:
        assert session.get(NotificationDelivery, 1).status == "sent"


@pytest.mark.asyncio
async def test_recovery_waits_for_unexpired_claim_and_can_be_cancelled(
    sqlite_engine, monkeypatch
):
    timestamp = now_utc()
    claim_until = timestamp + timedelta(seconds=0.05)
    with Session(sqlite_engine) as session:
        session.add(
            Announcement(
                id=1,
                title="Current",
                content="Body",
                status="published",
                published_at=timestamp,
                publication_version=1,
            )
        )
        session.add(
            NotificationDelivery(
                id=1,
                announcement_id=1,
                user_id=1,
                channel="web",
                status="pending",
                publication_version=1,
                claim_token="live-worker",
                claim_until=claim_until,
            )
        )
        session.commit()

    def factory():
        return _AsyncSQLiteSession(Session(sqlite_engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    monkeypatch.setattr(
        announcement_service,
        "_ANNOUNCEMENT_RECOVERY_POLL_SECONDS",
        0.01,
    )
    calls: list[int] = []

    async def fake_broadcast(db, announcement_id, **_kwargs):
        calls.append(announcement_id)
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == 1)
            .values(status="sent", claim_token=None, claim_until=None)
        )
        await db.commit()
        return {"sent": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(
        notification_service.notification_service,
        "broadcast_announcement",
        fake_broadcast,
    )
    started = time.monotonic()
    await announcement_service.recover_pending_announcement_deliveries()
    assert calls == [1]
    assert time.monotonic() - started >= 0.03

    # A long-lived heartbeat lease keeps the recovery task cancellable during
    # shutdown instead of allowing it to bypass ownership.
    with Session(sqlite_engine) as session:
        session.execute(
            update(NotificationDelivery).values(
                status="pending",
                claim_token="heartbeat-worker",
                claim_until=now_utc() + timedelta(hours=1),
            )
        )
        session.commit()
    task = asyncio.create_task(announcement_service.recover_pending_announcement_deliveries())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with Session(sqlite_engine) as session:
        row = session.get(NotificationDelivery, 1)
        assert row.status == "pending"
        assert row.claim_token == "heartbeat-worker"


@pytest.mark.asyncio
async def test_long_lease_does_not_block_other_recovery_work(
    sqlite_engine, monkeypatch
):
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add_all(
            [
                Announcement(
                    id=1,
                    title="Leased",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=1,
                ),
                Announcement(
                    id=2,
                    title="Ready",
                    content="Body",
                    status="published",
                    published_at=timestamp,
                    publication_version=1,
                ),
            ]
        )
        session.add_all(
            [
                NotificationDelivery(
                    id=1,
                    announcement_id=1,
                    user_id=1,
                    channel="web",
                    status="pending",
                    publication_version=1,
                    claim_token="long-worker",
                    claim_until=timestamp + timedelta(hours=1),
                ),
                NotificationDelivery(
                    id=2,
                    announcement_id=2,
                    user_id=2,
                    channel="web",
                    status="pending",
                    publication_version=1,
                ),
            ]
        )
        session.commit()

    def factory():
        return _AsyncSQLiteSession(Session(sqlite_engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    monkeypatch.setattr(
        announcement_service, "_ANNOUNCEMENT_RECOVERY_WORKERS", 1
    )
    ready = asyncio.Event()
    calls: list[int] = []

    async def fake_broadcast(db, announcement_id, **_kwargs):
        calls.append(announcement_id)
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.announcement_id == announcement_id)
            .values(status=DeliveryStatus.SENT.value)
        )
        await db.commit()
        ready.set()
        return {"sent": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(
        notification_service.notification_service,
        "broadcast_announcement",
        fake_broadcast,
    )
    recovery = asyncio.create_task(
        announcement_service.recover_pending_announcement_deliveries()
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=0.5)
        assert calls == [2]
        with Session(sqlite_engine) as session:
            assert session.get(NotificationDelivery, 2).status == "sent"
            assert session.get(NotificationDelivery, 1).status == "pending"
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery


@pytest.mark.asyncio
async def test_mixed_leased_and_unleased_rows_recover_without_waiting_for_lease(
    sqlite_engine, monkeypatch
):
    """An active lease must not block other rows in the same publication."""
    timestamp = now_utc()
    with Session(sqlite_engine) as session:
        session.add(
            Announcement(
                id=1,
                title="Current",
                content="Body",
                status="published",
                published_at=timestamp,
                publication_version=1,
            )
        )
        session.add_all(
            [
                NotificationDelivery(
                    id=1,
                    announcement_id=1,
                    user_id=1,
                    channel="web",
                    status=DeliveryStatus.PENDING.value,
                    publication_version=1,
                    claim_token="live-worker",
                    claim_until=timestamp + timedelta(hours=1),
                ),
                NotificationDelivery(
                    id=2,
                    announcement_id=1,
                    user_id=2,
                    channel="web",
                    status=DeliveryStatus.PENDING.value,
                    publication_version=1,
                ),
            ]
        )
        session.commit()

    def factory():
        return _AsyncSQLiteSession(Session(sqlite_engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    called = asyncio.Event()
    calls: list[int] = []

    async def fake_broadcast(db, announcement_id, **kwargs):
        calls.append(announcement_id)
        assert kwargs["pending_only"] is True
        await db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == 2,
                NotificationDelivery.status == DeliveryStatus.PENDING.value,
            )
            .values(status=DeliveryStatus.SENT.value)
        )
        await db.commit()
        called.set()
        return {"sent": 1, "failed": 0, "skipped": 1}

    monkeypatch.setattr(
        notification_service.notification_service,
        "broadcast_announcement",
        fake_broadcast,
    )
    recovery = asyncio.create_task(
        announcement_service._recover_one_announcement(1, 1, factory)
    )
    try:
        await asyncio.wait_for(called.wait(), timeout=0.5)
        assert calls == [1]
        with Session(sqlite_engine) as session:
            assert session.get(NotificationDelivery, 1).status == DeliveryStatus.PENDING.value
            assert session.get(NotificationDelivery, 2).status == DeliveryStatus.SENT.value
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery


@pytest.mark.asyncio
async def test_broadcast_uses_bounded_worker_pool_for_large_backlog(monkeypatch):
    """A backlog creates workers, not one coroutine per delivery row."""

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    class _DB:
        async def execute(self, statement, *_args, **_kwargs):
            if "notification_endpoints" in str(statement):
                return _Result([])
            return _Result(deliveries)

    announcement = Announcement(
        id=1,
        title="Backlog",
        content="Body",
        status="published",
        publication_version=1,
    )
    deliveries = [
        NotificationDelivery(
            id=index,
            announcement_id=1,
            user_id=index,
            channel="web",
            status=DeliveryStatus.PENDING.value,
            publication_version=1,
        )
        for index in range(1, 101)
    ]
    monkeypatch.setattr(database_module, "async_session", None)
    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(notification_max_concurrency=4),
    )
    active = 0
    max_active = 0

    async def fake_deliver(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return True

    service = notification_service.NotificationService()
    monkeypatch.setattr(service, "_deliver_one", fake_deliver)
    real_create_task = asyncio.create_task
    created_workers = 0

    def counting_create_task(coro, *args, **kwargs):
        nonlocal created_workers
        created_workers += 1
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(notification_service.asyncio, "create_task", counting_create_task)
    result = await service.broadcast_announcement(_DB(), announcement, expected_version=1)

    assert result == {"sent": 100, "failed": 0, "skipped": 0}
    assert created_workers == 4
    assert max_active <= 4


def test_admin_template_delete_is_limited_to_batch_deletable_ids():
    from pathlib import Path

    template = (
        Path(__file__).parents[1]
        / "backend/webui/templates/announcements_admin.html"
    ).read_text(encoding="utf-8")
    assert "item.id in (deletable_ids | default([]))" in template
    assert (
        "{% if item.status != 'published' %}<form method=\"post\" "
        "action=\"/announcements/admin/{{ item.id }}/delete\""
    ) not in template
