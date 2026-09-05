"""Security regressions for GitHub aliases and OAuth email endpoints."""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend.api.v1 import users as users_api
from backend.api.v1.schemas import UserInfoUpdateRequest
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import TelegramUser
from backend.services.identity_service import (
    GitHubAccount,
    GitHubUsernameConflictError,
    _upsert_email_endpoint,
    rename_github_username,
    upsert_github_account,
)
from backend.webui.routes import users as web_users


class _AsyncSQLiteSession:
    """AsyncSession-shaped facade backed by a real SQLite transaction."""

    def __init__(self, session: Session):
        self.sync_session = session
        self.commit_count = 0

    def add(self, instance):
        self.sync_session.add(instance)

    def add_all(self, instances):
        self.sync_session.add_all(instances)

    async def execute(self, statement):
        return self.sync_session.execute(statement)

    async def get(self, model, identity):
        return self.sync_session.get(model, identity)

    async def flush(self):
        self.sync_session.flush()

    async def commit(self):
        self.commit_count += 1
        self.sync_session.commit()

    async def rollback(self):
        self.sync_session.rollback()

    async def refresh(self, instance):
        self.sync_session.refresh(instance)

    async def run_sync(self, callback):
        return callback(self.sync_session)


@pytest.fixture
def sqlite_db():
    metadata = MetaData()
    TelegramUser.__table__.to_metadata(metadata)
    UserIdentity.__table__.to_metadata(metadata)
    NotificationEndpoint.__table__.to_metadata(metadata)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    session = Session(engine)
    facade = _AsyncSQLiteSession(session)
    try:
        yield facade
    finally:
        session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_rename_retires_synthetic_alias_and_old_login_cannot_claim_admin(
    sqlite_db,
):
    user = TelegramUser(
        telegram_id=101,
        github_username="old-name",
        role="admin",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.flush()
    sqlite_db.add(
        UserIdentity(
            user_id=user.id,
            provider="github",
            provider_user_id="legacy:old-name",
            provider_username="old-name",
        )
    )
    await sqlite_db.commit()

    await rename_github_username(sqlite_db, user, "new-name")
    await sqlite_db.commit()

    old_login = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="attacker-id", username="old-name"),
    )
    assert old_login is not None
    assert old_login.id != user.id
    assert user.github_username == "new-name"
    assert user.role == "admin"

    new_login = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="real-new-id", username="new-name"),
    )
    assert new_login is not None
    assert new_login.id == user.id


@pytest.mark.asyncio
async def test_rename_checks_conflicts_before_mutating_or_deleting_aliases(sqlite_db):
    target = TelegramUser(
        telegram_id=102,
        github_username="old-name",
        role="admin",
        is_active=True,
    )
    owner = TelegramUser(
        telegram_id=103,
        github_username="new-name",
        role="user",
        is_active=True,
    )
    sqlite_db.add(target)
    sqlite_db.add(owner)
    await sqlite_db.flush()
    alias = UserIdentity(
        user_id=target.id,
        provider="github",
        provider_user_id="legacy:old-name",
        provider_username="old-name",
    )
    sqlite_db.add(alias)
    await sqlite_db.commit()
    commits_before = sqlite_db.commit_count

    with pytest.raises(GitHubUsernameConflictError):
        await rename_github_username(sqlite_db, target, "new-name")

    assert sqlite_db.commit_count == commits_before
    assert target.github_username == "old-name"
    rows = (await sqlite_db.execute(select(UserIdentity))).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider_user_id == "legacy:old-name"


@pytest.mark.asyncio
async def test_rename_rejects_other_users_real_provider_username(sqlite_db):
    target = TelegramUser(
        telegram_id=109,
        github_username="old-name",
        role="admin",
        is_active=True,
    )
    other = TelegramUser(
        telegram_id=110,
        github_username="other-mirror",
        role="user",
        is_active=True,
    )
    sqlite_db.add_all([target, other])
    await sqlite_db.flush()
    sqlite_db.add(
        UserIdentity(
            user_id=other.id,
            provider="github",
            provider_user_id="stable-other",
            provider_username="new-name",
        )
    )
    await sqlite_db.commit()

    with pytest.raises(GitHubUsernameConflictError):
        await rename_github_username(sqlite_db, target, "new-name")
    assert target.github_username == "old-name"


@pytest.mark.asyncio
async def test_rename_only_rewrites_synthetic_rows_and_collapses_duplicates(sqlite_db):
    user = TelegramUser(
        telegram_id=104,
        github_username="old-name",
        role="admin",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.flush()
    sqlite_db.add_all(
        [
            UserIdentity(
                user_id=user.id,
                provider="github",
                provider_user_id="legacy:old-name",
                provider_username="old-name",
            ),
            UserIdentity(
                user_id=user.id,
                provider="github",
                provider_user_id="legacy:old-name-duplicate",
                provider_username="old-name",
            ),
            UserIdentity(
                user_id=user.id,
                provider="github",
                provider_user_id="real-provider-id",
                provider_username="provider-authoritative",
            ),
        ]
    )
    await sqlite_db.commit()

    await rename_github_username(sqlite_db, user, "new-name")
    await sqlite_db.commit()
    rows = (
        await sqlite_db.execute(
            select(UserIdentity).order_by(UserIdentity.provider_user_id)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert any(
        row.provider_user_id == "legacy:new-name"
        and row.provider_username == "new-name"
        for row in rows
    )
    real = next(row for row in rows if row.provider_user_id == "real-provider-id")
    assert real.provider_username == "provider-authoritative"


@pytest.mark.asyncio
async def test_unverified_email_never_enables_or_disables_verified_primary(sqlite_db):
    user = TelegramUser(
        telegram_id=105,
        github_username="email-user",
        role="user",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.flush()
    old_endpoint = NotificationEndpoint(
        user_id=user.id,
        provider="email",
        address="verified@example.com",
        verified=True,
        enabled=True,
    )
    sqlite_db.add(old_endpoint)
    await sqlite_db.commit()

    await _upsert_email_endpoint(
        sqlite_db, user, "unverified@example.com", verified=False
    )
    await sqlite_db.commit()
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.id)
        )
    ).scalars().all()
    new_endpoint = next(item for item in endpoints if "unverified" in item.address)
    assert new_endpoint.verified is False
    assert new_endpoint.enabled is False
    assert old_endpoint.enabled is True

    await _upsert_email_endpoint(
        sqlite_db, user, "unverified@example.com", verified=True
    )
    await sqlite_db.commit()
    assert new_endpoint.verified is True
    assert new_endpoint.enabled is True
    assert old_endpoint.enabled is False


@pytest.mark.asyncio
async def test_preprovisioned_user_without_identity_can_login_and_unverified_email_is_disabled(
    sqlite_db,
):
    user = TelegramUser(
        telegram_id=106,
        github_username="preprovisioned",
        role="user",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.commit()

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(
            provider_user_id="preprovisioned-id",
            username="preprovisioned",
            email="unverified@example.com",
            email_verified=False,
        ),
    )
    assert result is not None
    assert result.id == user.id
    identity = (
        await sqlite_db.execute(select(UserIdentity))
    ).scalars().one()
    endpoint = (
        await sqlite_db.execute(select(NotificationEndpoint))
    ).scalars().one()
    assert identity.provider_user_id == "preprovisioned-id"
    assert endpoint.verified is False
    assert endpoint.enabled is False


@pytest.mark.asyncio
async def test_api_and_webui_rename_routes_use_the_shared_conflict_guard(
    sqlite_db, monkeypatch
):
    target = TelegramUser(
        telegram_id=107,
        github_username="old-name",
        role="admin",
        is_active=True,
    )
    owner = TelegramUser(
        telegram_id=108,
        github_username="new-name",
        role="user",
        is_active=True,
    )
    sqlite_db.add_all([target, owner])
    await sqlite_db.commit()
    commits_before = sqlite_db.commit_count

    admin = {"sub": "root", "user_id": 999, "role": "super_admin"}
    api_response = await users_api.update_user_info(
        user_id=target.id,
        body=UserInfoUpdateRequest(github_username="new-name"),
        db=sqlite_db,
        user=admin,
    )
    assert api_response.status_code == 400
    assert sqlite_db.commit_count == commits_before
    assert target.github_username == "old-name"

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/users/{target.id}/info",
            "headers": [],
            "query_string": b"",
        }
    )
    web_response = await web_users.update_user_info(
        request=request,
        user_id=target.id,
        db=sqlite_db,
        user=admin,
        csrf_token="test",
        telegram_id=None,
        github_username="new-name",
    )
    assert web_response.status_code == 302
    assert sqlite_db.commit_count == commits_before
    assert target.github_username == "old-name"

    # A non-conflicting API rename commits through the same helper path.
    async def skip_admin_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "log_admin_action", skip_admin_log)
    success = await users_api.update_user_info(
        user_id=target.id,
        body=UserInfoUpdateRequest(github_username="renamed-name"),
        db=sqlite_db,
        user=admin,
    )
    assert success.status_code == 200
    assert target.github_username == "renamed-name"
