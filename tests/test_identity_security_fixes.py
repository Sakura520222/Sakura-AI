"""Security regressions for GitHub aliases and OAuth email endpoints."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend.api.v1 import auth as api_auth
from backend.api.v1 import users as users_api
from backend.api.v1.schemas import UserCreateRequest, UserInfoUpdateRequest
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import TelegramUser
from backend.services.identity_service import (
    GitHubAccount,
    GitHubUsernameConflictError,
    LegacyIdentityAmbiguityError,
    _upsert_email_endpoint,
    rename_github_username,
    upsert_github_account,
)
from backend.services.telegram_service import TelegramService
from backend.webui.routes import auth as web_auth
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
async def test_verified_disabled_email_stays_disabled_on_later_oauth(sqlite_db):
    user = TelegramUser(
        telegram_id=115,
        github_username="email-opt-out",
        role="user",
        is_active=True,
    )
    sqlite_db.add(user)
    await sqlite_db.flush()
    endpoint = NotificationEndpoint(
        user_id=user.id,
        provider="email",
        address="optout@example.com",
        verified=True,
        enabled=False,
    )
    sqlite_db.add(endpoint)
    await sqlite_db.commit()

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(
            provider_user_id="email-opt-out-id",
            username="email-opt-out",
            email="optout@example.com",
            email_verified=True,
        ),
    )

    assert result is not None
    assert endpoint.verified is True
    assert endpoint.enabled is False


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
async def test_oauth_new_user_stores_canonical_casefold_mirror(sqlite_db):
    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="new-canonical-id", username="Alice"),
    )

    assert result is not None
    assert result.github_username == "alice"
    identity = (await sqlite_db.execute(select(UserIdentity))).scalars().one()
    assert identity.provider_username == "Alice"


@pytest.mark.asyncio
async def test_legacy_bridge_rejects_two_casefold_mirrors_with_one_alias(sqlite_db):
    first = TelegramUser(
        telegram_id=111,
        github_username="Alice",
        role="user",
        is_active=True,
    )
    second = TelegramUser(
        telegram_id=112,
        github_username="alice",
        role="user",
        is_active=True,
    )
    sqlite_db.add_all([first, second])
    await sqlite_db.flush()
    sqlite_db.add(
        UserIdentity(
            user_id=first.id,
            provider="github",
            provider_user_id="legacy:alice",
            provider_username="Alice",
        )
    )
    await sqlite_db.commit()

    with pytest.raises(LegacyIdentityAmbiguityError):
        await upsert_github_account(
            sqlite_db,
            GitHubAccount(provider_user_id="new-provider", username="ALICE"),
        )

    assert [row.id for row in (await sqlite_db.execute(select(TelegramUser))).scalars().all()] == [
        first.id,
        second.id,
    ]
    assert len((await sqlite_db.execute(select(UserIdentity))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_legacy_bridge_rechecks_mirror_before_upgrading_alias(sqlite_db, monkeypatch):
    owner = TelegramUser(
        telegram_id=118,
        github_username="Alice",
        role="user",
        is_active=True,
    )
    sqlite_db.add(owner)
    await sqlite_db.flush()
    alias = UserIdentity(
        user_id=owner.id,
        provider="github",
        provider_user_id="legacy:alice",
        provider_username="Alice",
    )
    sqlite_db.add(alias)
    await sqlite_db.commit()

    async def lookup_then_inject(db, _account):
        # Simulate another application writer completing a case-insensitive
        # mirror insert after the initial lookup but before the bridge guard.
        db.add(
            TelegramUser(
                telegram_id=119,
                github_username="alice",
                role="user",
                is_active=True,
            )
        )
        await db.flush()
        return alias

    monkeypatch.setattr(
        "backend.services.identity_service._find_github_identity",
        lookup_then_inject,
    )

    with pytest.raises(LegacyIdentityAmbiguityError):
        await upsert_github_account(
            sqlite_db,
            GitHubAccount(provider_user_id="new-alice-id", username="ALICE"),
        )

    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    assert [(row.id, row.github_username) for row in users] == [
        (owner.id, "Alice")
    ]
    assert alias.provider_user_id == "legacy:alice"


@pytest.mark.asyncio
async def test_exact_stable_provider_id_bypasses_legacy_mirror_ambiguity(sqlite_db):
    first = TelegramUser(
        telegram_id=113,
        github_username="Alice",
        role="user",
        is_active=True,
    )
    second = TelegramUser(
        telegram_id=114,
        github_username="alice",
        role="user",
        is_active=True,
    )
    sqlite_db.add_all([first, second])
    await sqlite_db.flush()
    identity = UserIdentity(
        user_id=first.id,
        provider="github",
        provider_user_id="stable-alice",
        provider_username="Alice",
    )
    sqlite_db.add(identity)
    await sqlite_db.commit()

    result = await upsert_github_account(
        sqlite_db,
        GitHubAccount(provider_user_id="stable-alice", username="ALICE"),
    )

    assert result is not None
    assert result.id == first.id
    assert result.github_username == "Alice"
    assert identity.provider_username == "ALICE"


@pytest.mark.asyncio
async def test_username_derived_login_provider_id_obeys_ambiguity_guard(sqlite_db):
    first = TelegramUser(
        telegram_id=116,
        github_username="Alice",
        role="user",
        is_active=True,
    )
    second = TelegramUser(
        telegram_id=117,
        github_username="alice",
        role="user",
        is_active=True,
    )
    sqlite_db.add_all([first, second])
    await sqlite_db.flush()
    sqlite_db.add(
        UserIdentity(
            user_id=first.id,
            provider="github",
            provider_user_id="login:alice",
            provider_username="Alice",
        )
    )
    await sqlite_db.commit()

    with pytest.raises(LegacyIdentityAmbiguityError):
        await upsert_github_account(
            sqlite_db,
            GitHubAccount(provider_user_id="login:alice", username="ALICE"),
        )


@pytest.mark.asyncio
async def test_telegram_registration_rejects_casefold_duplicate_username(sqlite_db):
    service = TelegramService(sqlite_db)

    first_ok, _ = await service.register_user(telegram_id=120, github_username="Alice")
    second_ok, second_message = await service.register_user(
        telegram_id=121, github_username="alice"
    )

    assert first_ok is True
    assert second_ok is False
    assert "已被其他账号绑定" in second_message
    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    assert [(row.telegram_id, row.github_username) for row in users] == [
        (120, "alice")
    ]


@pytest.mark.asyncio
async def test_api_user_creation_stages_telegram_endpoint_atomically(
    sqlite_db, monkeypatch
):
    async def skip_admin_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "log_admin_action", skip_admin_log)
    response = await users_api.create_user(
        body=UserCreateRequest(github_username="new-user", telegram_id=7001),
        db=sqlite_db,
        user={"sub": "root", "user_id": 999, "role": "super_admin"},
    )

    assert response.status_code == 200
    created = (await sqlite_db.execute(select(TelegramUser))).scalars().one()
    endpoint = (await sqlite_db.execute(select(NotificationEndpoint))).scalars().one()
    assert created.telegram_id == 7001
    assert endpoint.user_id == created.id
    assert endpoint.address == "7001"
    assert endpoint.enabled is True


@pytest.mark.asyncio
async def test_api_user_creation_rolls_back_when_endpoint_is_owned(
    sqlite_db, monkeypatch
):
    owner = TelegramUser(telegram_id=None, github_username="owner", is_active=True)
    sqlite_db.add(owner)
    await sqlite_db.flush()
    sqlite_db.add(
        NotificationEndpoint(
            user_id=owner.id,
            provider="telegram",
            address="7002",
            verified=True,
            enabled=True,
        )
    )
    await sqlite_db.commit()

    async def skip_admin_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_api, "log_admin_action", skip_admin_log)
    response = await users_api.create_user(
        body=UserCreateRequest(github_username="must-rollback", telegram_id=7002),
        db=sqlite_db,
        user={"sub": "root", "user_id": 999, "role": "super_admin"},
    )

    assert response.status_code == 400
    users = (await sqlite_db.execute(select(TelegramUser))).scalars().all()
    assert [item.github_username for item in users] == ["owner"]


@pytest.mark.asyncio
async def test_oauth_provider_identity_commit_race_reloads_exact_winner(monkeypatch):
    winner = TelegramUser(
        id=200,
        telegram_id=1200,
        github_username="winner",
        is_active=True,
    )
    winner_identity = UserIdentity(
        id=300,
        user_id=winner.id,
        provider="github",
        provider_user_id="race-provider",
        provider_username="winner",
    )

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self.rows)

    class _RaceSession:
        def __init__(self):
            self.rollback_count = 0

        async def execute(self, query):
            entity = query.column_descriptions[0]["entity"]
            if entity is UserIdentity:
                if any(
                    "provider_user_id" in key for key in query.compile().params
                ):
                    return _Rows([winner_identity])
                return _Rows([])
            if entity is TelegramUser:
                return _Rows([])
            raise AssertionError(entity)

        def add(self, _row):
            return None

        async def commit(self):
            raise IntegrityError(
                "insert",
                {},
                sqlite3.IntegrityError(
                    "UNIQUE constraint failed: "
                    "user_identities.provider, user_identities.provider_user_id"
                ),
            )

        async def rollback(self):
            self.rollback_count += 1

        async def get(self, model, row_id):
            assert model is TelegramUser
            return winner if row_id == winner.id else None

        async def refresh(self, _row):
            return None

    session = _RaceSession()
    monkeypatch.setattr(
        "backend.services.identity_service.create_user_and_flush",
        AsyncMock(return_value=TelegramUser(id=999, github_username="winner")),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_github_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_user_by_explicit_github_username",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._disable_unverified_email_endpoints",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._upsert_email_endpoint",
        AsyncMock(),
    )

    # The first lookup is intentionally empty; the exact winner is only
    # visible to the query performed after rollback in the commit handler.
    result = await upsert_github_account(
        session,
        GitHubAccount(provider_user_id="race-provider", username="winner"),
    )

    assert result is winner
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_oauth_username_flush_race_reloads_exact_winner(monkeypatch):
    winner = TelegramUser(
        id=201,
        telegram_id=1201,
        github_username="winner",
        is_active=True,
    )
    winner_identity = UserIdentity(
        id=301,
        user_id=winner.id,
        provider="github",
        provider_user_id="flush-race-provider",
        provider_username="winner",
    )

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self.rows)

    class _FlushRaceSession:
        def __init__(self):
            self.rollback_count = 0

        async def execute(self, query):
            entity = query.column_descriptions[0]["entity"]
            if entity is UserIdentity:
                return _Rows([winner_identity])
            if entity is TelegramUser:
                return _Rows([])
            raise AssertionError(entity)

        async def rollback(self):
            self.rollback_count += 1

        async def get(self, model, row_id):
            assert model is TelegramUser
            return winner if row_id == winner.id else None

        async def refresh(self, _row):
            return None

    session = _FlushRaceSession()
    monkeypatch.setattr(
        "backend.services.identity_service.create_user_and_flush",
        AsyncMock(
            side_effect=IntegrityError(
                "insert",
                {},
                sqlite3.IntegrityError(
                    "UNIQUE constraint failed: telegram_users.github_username"
                ),
            )
        ),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_github_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_user_by_explicit_github_username",
        AsyncMock(return_value=None),
    )

    result = await upsert_github_account(
        session,
        GitHubAccount(
            provider_user_id="flush-race-provider", username="winner"
        ),
    )

    assert result is winner
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_oauth_username_flush_race_does_not_swallow_other_integrity_error(
    monkeypatch,
):
    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    class _FlushFailureSession:
        async def execute(self, _query):
            return _Rows()

        async def rollback(self):
            return None

    session = _FlushFailureSession()
    unrelated = IntegrityError(
        "insert",
        {},
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: telegram_users.email"
        ),
    )
    monkeypatch.setattr(
        "backend.services.identity_service.create_user_and_flush",
        AsyncMock(side_effect=unrelated),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_github_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "backend.services.identity_service._find_user_by_explicit_github_username",
        AsyncMock(return_value=None),
    )

    with pytest.raises(IntegrityError) as caught:
        await upsert_github_account(
            session,
            GitHubAccount(provider_user_id="unrelated-race", username="winner"),
        )

    assert caught.value is unrelated


@pytest.mark.asyncio
async def test_api_oauth_ambiguity_consumes_state_without_account_details(monkeypatch):
    deleted_states: list[str] = []

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_authenticate(*_args, **_kwargs):
        raise LegacyIdentityAmbiguityError("candidate-one/candidate-two")

    monkeypatch.setattr(
        api_auth,
        "_get_oauth_state",
        AsyncMock(return_value={"redirect": "/"}),
    )
    monkeypatch.setattr(
        api_auth,
        "_delete_oauth_state",
        AsyncMock(side_effect=lambda state: deleted_states.append(state)),
    )
    monkeypatch.setattr(
        api_auth,
        "get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        api_auth,
        "GitHubOAuthProvider",
        lambda _settings: SimpleNamespace(
            exchange_code=AsyncMock(
                return_value=SimpleNamespace(
                    account=GitHubAccount(
                        provider_user_id="ambiguous",
                        username="alice",
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(
        api_auth.auth_service,
        "authenticate_github",
        fake_authenticate,
    )
    monkeypatch.setattr(api_auth.db_module, "async_session", _SessionContext)

    endpoint = getattr(api_auth.github_callback, "__wrapped__", api_auth.github_callback)
    response = await endpoint(None, api_auth.OAuthCallbackRequest(code="c", state="s"))

    assert response.status_code == 409
    assert deleted_states == ["s"]
    assert b"candidate-one" not in response.body
    assert b"candidate-two" not in response.body


@pytest.mark.asyncio
async def test_webui_oauth_ambiguity_consumes_state_without_account_details(monkeypatch):
    deleted_states: list[str] = []

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_authenticate(*_args, **_kwargs):
        raise LegacyIdentityAmbiguityError("candidate-one/candidate-two")

    monkeypatch.setattr(web_auth, "_get_oauth_state", AsyncMock(return_value={"redirect": "/"}))
    monkeypatch.setattr(
        web_auth,
        "_delete_oauth_state",
        AsyncMock(side_effect=lambda state: deleted_states.append(state)),
    )
    monkeypatch.setattr(web_auth, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        web_auth,
        "GitHubOAuthProvider",
        lambda _settings: SimpleNamespace(
            exchange_code=AsyncMock(
                return_value=SimpleNamespace(
                    account=GitHubAccount(
                        provider_user_id="ambiguous",
                        username="alice",
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(
        web_auth.auth_service,
        "authenticate_github",
        fake_authenticate,
    )
    monkeypatch.setattr(web_auth.db_module, "async_session", _SessionContext)
    monkeypatch.setattr(
        web_auth,
        "_oauth_error",
        lambda _request, message, **kwargs: SimpleNamespace(
            status_code=kwargs.get("status_code"),
            body=message.encode(),
        ),
    )

    response = await web_auth.github_callback(
        request=None,
        code="c",
        state="s",
        error=None,
        error_description=None,
    )

    assert response.status_code == 409
    assert deleted_states == ["s"]
    assert b"candidate-one" not in response.body


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
