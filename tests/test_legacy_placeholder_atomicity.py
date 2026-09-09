"""Concurrency and error-boundary tests for legacy SQLite user placeholders."""

from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.models.identity_models import NotificationEndpoint
from backend.models.telegram_models import TelegramUser
from backend.services import identity_service
from backend.services.identity_service import create_user_and_flush


class _AsyncNestedTransaction:
    """Adapt a synchronous SQLAlchemy savepoint to an async-shaped facade."""

    def __init__(self, transaction):
        self._transaction = transaction

    async def __aenter__(self):
        self._transaction.__enter__()
        return self

    async def __aexit__(self, *args):
        return self._transaction.__exit__(*args)


class _AsyncSQLiteSession:
    """Expose one independent file-backed SQLite Session as async methods."""

    def __init__(self, session: Session, candidate_barrier: threading.Barrier | None = None):
        self._session = session
        self._candidate_barrier = candidate_barrier
        self._candidate_waited = False

    def add(self, value):
        self._session.add(value)

    async def execute(self, statement):
        result = self._session.execute(statement)
        if (
            self._candidate_barrier is not None
            and not self._candidate_waited
            and "select telegram_users.telegram_id" in str(statement).casefold()
        ):
            self._candidate_waited = True
            self._candidate_barrier.wait(timeout=10)
        return result

    async def run_sync(self, callback):
        return callback(self._session)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    def begin_nested(self):
        return _AsyncNestedTransaction(self._session.begin_nested())


@pytest.fixture
def legacy_sqlite_engine(tmp_path):
    metadata = MetaData()
    users_table = TelegramUser.__table__.to_metadata(metadata)
    # Model metadata describes the current nullable schema.  Recreate the
    # physical pre-v2 table explicitly to exercise the compatibility branch.
    users_table.c.telegram_id.nullable = False
    NotificationEndpoint.__table__.to_metadata(metadata)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-users.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _register(engine, username: str, barrier: threading.Barrier) -> int:
    session = Session(engine, expire_on_commit=False)
    db = _AsyncSQLiteSession(session, barrier)

    async def _run() -> int:
        user = await create_user_and_flush(
            db,
            lambda resolved_telegram_id: TelegramUser(
                telegram_id=resolved_telegram_id,
                github_username=username,
            ),
        )
        await db.commit()
        return int(user.telegram_id)

    try:
        return asyncio.run(_run())
    finally:
        session.close()


@pytest.mark.asyncio
async def test_file_sqlite_concurrent_legacy_registrations_claim_distinct_sentinels(
    legacy_sqlite_engine,
):
    """Two sessions racing on the same first candidate both eventually succeed."""

    barrier = threading.Barrier(2)
    first, second = await asyncio.gather(
        asyncio.to_thread(_register, legacy_sqlite_engine, "alice", barrier),
        asyncio.to_thread(_register, legacy_sqlite_engine, "bob", barrier),
    )

    assert {first, second} == {0, -1}
    with Session(legacy_sqlite_engine) as session:
        users = session.execute(select(TelegramUser).order_by(TelegramUser.id)).scalars().all()
        endpoints = session.execute(select(NotificationEndpoint)).scalars().all()
    assert len(users) == 2
    assert {user.telegram_id for user in users} == {0, -1}
    assert all(user.telegram_id <= 0 for user in users)
    assert endpoints == []


@pytest.mark.asyncio
async def test_real_async_sessions_retry_the_same_legacy_candidate(tmp_path, monkeypatch):
    """Exercise AsyncSession.begin_nested() against a real file SQLite DB."""

    pytest.importorskip("aiosqlite")
    metadata = MetaData()
    users_table = TelegramUser.__table__.to_metadata(metadata)
    users_table.c.telegram_id.nullable = False
    NotificationEndpoint.__table__.to_metadata(metadata)
    database_path = tmp_path / "legacy-users-async.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 10},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        first_selection_ready = asyncio.Event()
        selection_count = 0
        selection_lock = asyncio.Lock()
        original_next_placeholder = identity_service._next_legacy_placeholder

        async def coordinated_next_placeholder(db):
            nonlocal selection_count
            async with selection_lock:
                selection_count += 1
                is_first_round = selection_count <= 2
                if selection_count == 2:
                    first_selection_ready.set()
            if is_first_round:
                await first_selection_ready.wait()
                return 0
            return await original_next_placeholder(db)

        monkeypatch.setattr(
            identity_service,
            "_next_legacy_placeholder",
            coordinated_next_placeholder,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def register(username: str) -> int:
            async with sessions() as db:
                user = await create_user_and_flush(
                    db,
                    lambda resolved_telegram_id: TelegramUser(
                        telegram_id=resolved_telegram_id,
                        github_username=username,
                    ),
                )
                await db.commit()
                return int(user.telegram_id)

        first, second = await asyncio.wait_for(
            asyncio.gather(register("async-alice"), register("async-bob")),
            timeout=30,
        )
        assert selection_count >= 3
        assert {first, second} == {0, -1}

        async with sessions() as db:
            users = (
                await db.execute(select(TelegramUser).order_by(TelegramUser.id))
            ).scalars().all()
            endpoints = (await db.execute(select(NotificationEndpoint))).scalars().all()
        assert {user.telegram_id for user in users} == {0, -1}
        assert endpoints == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_telegram_unique_conflict_is_not_retried_or_swallowed(
    legacy_sqlite_engine,
):
    session = Session(legacy_sqlite_engine, expire_on_commit=False)
    db = _AsyncSQLiteSession(session)
    session.add(TelegramUser(telegram_id=123, github_username="already-used"))
    session.commit()

    async def _run() -> None:
        with pytest.raises(IntegrityError):
            await create_user_and_flush(
                db,
                lambda resolved_telegram_id: TelegramUser(
                    telegram_id=resolved_telegram_id,
                    github_username="already-used",
                ),
            )

        # The target savepoint was rolled back, leaving the outer transaction
        # usable for the caller.  No broad IntegrityError swallowing occurred.
        users = (await db.execute(select(TelegramUser))).scalars().all()
        assert len(users) == 1

    try:
        await _run()
    finally:
        session.close()
