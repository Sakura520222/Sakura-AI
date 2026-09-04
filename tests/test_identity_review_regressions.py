"""Regression coverage for legacy user storage and GitHub identity linking."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api.v1 import users as users_api
from backend.api.v1.schemas import UserCreateRequest
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import TelegramUser
from backend.services.identity_service import (
    GitHubAccount,
    upsert_github_account,
)


class _AsyncSessionFacade:
    """Expose the AsyncSession methods under test over a real SQLite Session."""

    def __init__(self, session: Session):
        self.sync_session = session
        self.commit_count = 0

    def add(self, instance) -> None:
        self.sync_session.add(instance)

    async def execute(self, statement):
        return self.sync_session.execute(statement)

    async def get(self, model, identity):
        return self.sync_session.get(model, identity)

    async def flush(self) -> None:
        self.sync_session.flush()

    async def commit(self) -> None:
        self.commit_count += 1
        self.sync_session.commit()

    async def rollback(self) -> None:
        self.sync_session.rollback()

    async def refresh(self, instance) -> None:
        self.sync_session.refresh(instance)

    async def run_sync(self, callback):
        return callback(self.sync_session)


@pytest.fixture
def sqlite_db():
    """Build a real in-memory SQLite schema with the pre-v2 NOT NULL column."""
    metadata = MetaData()
    users_table = TelegramUser.__table__.to_metadata(metadata)
    users_table.c.telegram_id.nullable = False
    UserIdentity.__table__.to_metadata(metadata)
    NotificationEndpoint.__table__.to_metadata(metadata)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    session = Session(engine)
    facade = _AsyncSessionFacade(session)
    try:
        yield facade
    finally:
        session.close()
        engine.dispose()


def _response_payload(response) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_api_create_user_uses_unique_legacy_placeholders_without_endpoints(
    monkeypatch, sqlite_db
):
    async def skip_admin_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "log_admin_action", skip_admin_log)

    first_response = await users_api.create_user(
        body=UserCreateRequest(github_username="alice", role="admin"),
        db=sqlite_db,
        user={"sub": "root", "user_id": 99},
    )
    second_response = await users_api.create_user(
        body=UserCreateRequest(github_username="bob", role="super_admin"),
        db=sqlite_db,
        user={"sub": "root", "user_id": 99},
    )

    first = _response_payload(first_response)
    second = _response_payload(second_response)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first["success"] is True
    assert second["success"] is True
    assert first["data"]["telegram_id"] == 0
    assert second["data"]["telegram_id"] == -1
    assert first["data"]["role"] == "admin"
    assert second["data"]["role"] == "super_admin"

    users = (
        (await sqlite_db.execute(select(TelegramUser).order_by(TelegramUser.id)))
        .scalars()
        .all()
    )
    assert [(item.telegram_id, item.role) for item in users] == [
        (0, "admin"),
        (-1, "super_admin"),
    ]
    assert (await sqlite_db.execute(select(NotificationEndpoint))).scalars().all() == []
    assert (await sqlite_db.execute(select(UserIdentity))).scalars().all() == []


@pytest.mark.asyncio
async def test_github_login_rejects_inactive_username_only_user_without_mutation(
    sqlite_db,
):
    disabled = TelegramUser(
        telegram_id=100,
        github_username="disabled-user",
        role="admin",
        is_active=False,
    )
    sqlite_db.add(disabled)
    await sqlite_db.commit()
    commits_before_login = sqlite_db.commit_count

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="github-1", username="disabled-user"),
    )

    assert result is None
    assert sqlite_db.commit_count == commits_before_login
    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    identities = (await sqlite_db.execute(select(UserIdentity))).scalars().all()
    endpoints = (await sqlite_db.execute(select(NotificationEndpoint))).scalars().all()
    assert len(users) == 1
    assert users[0].id == disabled.id
    assert users[0].github_username == "disabled-user"
    assert users[0].is_active is False
    assert identities == []
    assert endpoints == []


@pytest.mark.asyncio
async def test_active_legacy_and_stable_github_identity_logins_reuse_user(
    sqlite_db,
):
    legacy_user = TelegramUser(
        telegram_id=200,
        github_username="legacy-user",
        role="admin",
        is_active=True,
    )
    sqlite_db.add(legacy_user)
    await sqlite_db.commit()

    linked = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="github-2", username="legacy-user"),
    )
    renamed = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="github-2", username="renamed-user"),
    )

    assert linked is not None
    assert renamed is not None
    assert linked.id == legacy_user.id
    assert renamed.id == legacy_user.id
    assert renamed.github_username == "renamed-user"
    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    identities = (await sqlite_db.execute(select(UserIdentity))).scalars().all()
    assert len(users) == 1
    assert len(identities) == 1
    assert identities[0].provider_user_id == "github-2"
    assert identities[0].provider_username == "renamed-user"


@pytest.mark.asyncio
async def test_github_provider_mismatch_is_rejected_without_creating_user(
    sqlite_db,
):
    user = TelegramUser(
        telegram_id=300,
        github_username="bound-user",
        role="user",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.commit()
    identity = UserIdentity(
        user_id=user.id,
        provider="github",
        provider_user_id="github-stable",
        provider_username="bound-user",
    )
    sqlite_db.add(identity)
    await sqlite_db.commit()
    commits_before_login = sqlite_db.commit_count

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="github-other", username="bound-user"),
    )

    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    identities = (await sqlite_db.execute(select(UserIdentity))).scalars().all()
    assert result is None
    assert sqlite_db.commit_count == commits_before_login
    assert len(users) == 1
    assert len(identities) == 1
    assert identities[0].provider_user_id == "github-stable"


@pytest.mark.asyncio
async def test_inactive_real_identity_mismatch_is_rejected_without_reactivation(
    sqlite_db,
):
    user = TelegramUser(
        telegram_id=400,
        github_username="disabled-bound-user",
        role="user",
        is_active=False,
    )
    sqlite_db.add(user)
    await sqlite_db.commit()
    identity = UserIdentity(
        user_id=user.id,
        provider="github",
        provider_user_id="github-disabled-stable",
        provider_username="disabled-bound-user",
    )
    sqlite_db.add(identity)
    await sqlite_db.commit()
    commits_before_login = sqlite_db.commit_count

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(
            provider_user_id="github-disabled-other",
            username="disabled-bound-user",
        ),
    )

    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    identities = (await sqlite_db.execute(select(UserIdentity))).scalars().all()
    assert result is None
    assert sqlite_db.commit_count == commits_before_login
    assert len(users) == 1
    assert users[0].is_active is False
    assert len(identities) == 1
    assert identities[0].provider_user_id == "github-disabled-stable"
