"""Regression tests for Setup validation and identity/backup compatibility."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.setup_service import SetupService
from backend.models.database import UserConfig, WebUIConfig
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserRepoSubscription,
    UserWebAuthnCredential,
)
from backend.services.config_backup_service import SYSTEM_SECTION, BackupRecord
from backend.services.identity_service import (
    NotificationEndpointConflictError,
    migrate_legacy_identity_data,
)
from backend.services.system_config_service import SystemConfigValidationError
from backend.services.user_backup_service import parse_user_backup, restore_user_backup
from backend.telegram.notifications import NotificationSender


class _AsyncSQLiteSession:
    """Small async facade over a real synchronous SQLite session."""

    def __init__(self, session: Session):
        self.sync_session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def add(self, instance):
        self.sync_session.add(instance)

    async def execute(self, statement):
        return self.sync_session.execute(statement)

    async def get(self, model, identity):
        return self.sync_session.get(model, identity)

    async def flush(self):
        self.sync_session.flush()

    async def refresh(self, instance):
        self.sync_session.refresh(instance)

    async def commit(self):
        self.sync_session.commit()

    async def rollback(self):
        self.sync_session.rollback()

    async def delete(self, instance):
        self.sync_session.delete(instance)

    async def close(self):
        self.sync_session.close()


@pytest.fixture
def sqlite_db():
    metadata = MetaData()
    for model in (
        TelegramUser,
        UserIdentity,
        NotificationEndpoint,
        UserRepoSubscription,
        UserConfig,
        WebUIConfig,
        UserRecoveryCode,
        UserWebAuthnCredential,
    ):
        model.__table__.to_metadata(metadata)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    session = Session(engine)
    facade = _AsyncSQLiteSession(session)
    try:
        yield facade
    finally:
        session.close()
        engine.dispose()


def _backup_document(
    *,
    telegram_id: int,
    endpoints: list[dict] | None,
    email: str | None = None,
    email_verified: bool = False,
):
    identity = {
        "telegram_id": telegram_id,
        "github_username": "alice",
    }
    if email is not None:
        identity.update(email=email, email_verified=email_verified)
    return parse_user_backup(
        json.dumps(
            {
                "format": "sakura-ai-user-backup",
                "version": 2,
                "scope": "users",
                "user_count": 1,
                "users": [
                    {
                        "identity": identity,
                        "identities": [
                            {
                                "provider": "github",
                                "provider_user_id": "github-alice",
                                "provider_username": "alice",
                            }
                        ],
                        "notification_endpoints": endpoints,
                        "profile": {"is_active": True},
                        "personal_config": {
                            "dynamic_overrides": [],
                            "webui": None,
                        },
                        "two_factor": {
                            "mfa_required": False,
                            "totp_enabled": False,
                            "totp_secret": None,
                            "totp_enabled_at": None,
                            "totp_last_used_step": None,
                            "recovery_codes": [],
                        },
                        "passkeys": [],
                    }
                ],
            }
    ).encode()
    )


@pytest.mark.asyncio
async def test_restore_legacy_unverified_email_mirror_is_kept_disabled(sqlite_db):
    document = _backup_document(
        telegram_id=1201,
        endpoints=None,
        email="legacy@example.com",
        email_verified=False,
    )

    await restore_user_backup(sqlite_db, document)

    endpoint = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "email"
            )
        )
    ).scalars().one()
    assert endpoint.address == "legacy@example.com"
    assert endpoint.verified is False
    assert endpoint.enabled is False


@pytest.mark.asyncio
async def test_restore_explicit_unverified_enabled_email_is_normalized_disabled(
    sqlite_db,
):
    document = _backup_document(
        telegram_id=1202,
        endpoints=[
            {
                "provider": "email",
                "address": "unverified@example.com",
                "verified": False,
                "enabled": True,
            }
        ],
    )
    # Exercise the restore boundary independently of the parser normalization;
    # this mirrors callers that construct a validated backup document in code.
    document["users"][0]["notification_endpoints"][0]["enabled"] = True

    await restore_user_backup(sqlite_db, document)

    endpoint = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "email"
            )
        )
    ).scalars().one()
    assert endpoint.verified is False
    assert endpoint.enabled is False


@pytest.mark.asyncio
async def test_restore_verified_disabled_email_preserves_user_choice(sqlite_db):
    document = _backup_document(
        telegram_id=1203,
        endpoints=[
            {
                "provider": "email",
                "address": "verified@example.com",
                "verified": True,
                "enabled": False,
            }
        ],
    )

    await restore_user_backup(sqlite_db, document)

    endpoint = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "email"
            )
        )
    ).scalars().one()
    assert endpoint.verified is True
    assert endpoint.enabled is False


@pytest.mark.asyncio
async def test_setup_rejects_invalid_notification_batch_before_initialization(monkeypatch):
    service = SetupService()
    init_database = AsyncMock()
    monkeypatch.setattr(service, "init_database", init_database)

    result = await service.complete_setup(
        {
            "DATABASE_URL": "mysql+asyncmy://user:pass@db/sakura",
            "ADMIN_GITHUB_USERNAME": "admin",
            "NOTIFICATION_RETRY_MAX_ATTEMPTS": "999999",
        }
    )

    assert result["success"] is False
    assert "notification_retry_max_attempts" in result["message"]
    init_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_revalidates_backup_notification_values_before_initialization(
    monkeypatch,
):
    service = SetupService()
    init_database = AsyncMock()
    monkeypatch.setattr(service, "init_database", init_database)

    result = await service.complete_setup(
        {
            "DATABASE_URL": "mysql+asyncmy://user:pass@db/sakura",
            "ADMIN_GITHUB_USERNAME": "admin",
        },
        backup_sections={
            SYSTEM_SECTION: [
                BackupRecord(
                    "notification_rate_limit_seconds",
                    "inf",
                    "notification_rate_limit_seconds",
                )
            ]
        },
    )

    assert result["success"] is False
    assert "notification_rate_limit_seconds" in result["message"]
    init_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_rejects_casefold_duplicate_legacy_admin_users(
    sqlite_db, monkeypatch
):
    sqlite_db.sync_session.add_all(
        [
            TelegramUser(id=1, telegram_id=101, github_username="Alice"),
            TelegramUser(id=2, telegram_id=102, github_username="alice"),
        ]
    )
    await sqlite_db.commit()

    monkeypatch.setattr("backend.models.database.async_engine", object())
    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    monkeypatch.setattr(
        "backend.models.database.migrate_schema_async", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.models.database.insert_default_configs_async", AsyncMock()
    )

    with pytest.raises(ValueError, match="大小写冲突"):
        await SetupService().create_admin_user(
            "ALICE", database_url="mysql+asyncmy://user:pass@db/sakura"
        )

    assert [row.role for row in sqlite_db.sync_session.query(TelegramUser).all()] == [
        "user",
        "user",
    ]


@pytest.mark.asyncio
async def test_setup_admin_positive_telegram_id_stages_verified_endpoint(
    sqlite_db, monkeypatch
):
    monkeypatch.setattr("backend.models.database.async_engine", object())
    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    monkeypatch.setattr(
        "backend.models.database.migrate_schema_async", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.models.database.insert_default_configs_async", AsyncMock()
    )

    await SetupService().create_admin_user(
        "setup-admin", 1204, "mysql+asyncmy://user:pass@db/sakura"
    )

    user = (
        await sqlite_db.execute(
            select(TelegramUser).where(
                TelegramUser.github_username == "setup-admin"
            )
        )
    ).scalars().one()
    endpoint = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "telegram"
            )
        )
    ).scalars().one()
    assert endpoint.user_id == user.id
    assert endpoint.address == "1204"
    assert endpoint.enabled is True
    assert endpoint.verified is True


@pytest.mark.asyncio
async def test_setup_admin_reuses_and_reenables_telegram_endpoint(
    sqlite_db, monkeypatch
):
    sqlite_db.add(
        TelegramUser(
            id=1,
            telegram_id=1204,
            github_username="setup-admin",
            role="user",
            is_active=True,
        )
    )
    sqlite_db.add(
        NotificationEndpoint(
            user_id=1,
            provider="telegram",
            address="1204",
            verified=False,
            enabled=False,
        )
    )
    await sqlite_db.commit()

    monkeypatch.setattr("backend.models.database.async_engine", object())
    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    monkeypatch.setattr(
        "backend.models.database.migrate_schema_async", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.models.database.insert_default_configs_async", AsyncMock()
    )

    await SetupService().create_admin_user(
        "setup-admin", 1204, "mysql+asyncmy://user:pass@db/sakura"
    )

    endpoint = sqlite_db.sync_session.query(NotificationEndpoint).one()
    assert endpoint.enabled is True
    assert endpoint.verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize("telegram_id", [None, 0, -1204])
async def test_setup_admin_nonpositive_telegram_id_does_not_stage_endpoint(
    sqlite_db, monkeypatch, telegram_id
):
    monkeypatch.setattr("backend.models.database.async_engine", object())
    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    monkeypatch.setattr(
        "backend.models.database.migrate_schema_async", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.models.database.insert_default_configs_async", AsyncMock()
    )

    await SetupService().create_admin_user(
        "setup-admin", telegram_id, "mysql+asyncmy://user:pass@db/sakura"
    )

    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "telegram"
            )
        )
    ).scalars().all()
    assert endpoints == []


@pytest.mark.asyncio
async def test_setup_telegram_endpoint_conflict_rolls_back_admin_promotion(
    sqlite_db, monkeypatch
):
    sqlite_db.add(
        TelegramUser(
            id=1,
            telegram_id=111,
            github_username="setup-admin",
            role="user",
            is_active=True,
        )
    )
    sqlite_db.add(
        TelegramUser(
            id=2,
            telegram_id=222,
            github_username="owner",
            role="user",
            is_active=True,
        )
    )
    sqlite_db.add(
        NotificationEndpoint(
            user_id=2,
            provider="telegram",
            address="1205",
            verified=True,
            enabled=True,
        )
    )
    await sqlite_db.commit()

    monkeypatch.setattr("backend.models.database.async_engine", object())
    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    monkeypatch.setattr(
        "backend.models.database.migrate_schema_async", AsyncMock()
    )
    monkeypatch.setattr(
        "backend.models.database.insert_default_configs_async", AsyncMock()
    )

    with pytest.raises(NotificationEndpointConflictError):
        await SetupService().create_admin_user(
            "setup-admin", 1205, "mysql+asyncmy://user:pass@db/sakura"
        )

    target = sqlite_db.sync_session.get(TelegramUser, 1)
    owner = sqlite_db.sync_session.get(TelegramUser, 2)
    endpoint = sqlite_db.sync_session.query(NotificationEndpoint).one()
    assert target.role == "user"
    assert target.telegram_id == 111
    assert owner.role == "user"
    assert endpoint.user_id == 2


@pytest.mark.asyncio
async def test_save_configs_validates_before_opening_session(monkeypatch):
    service = SetupService()
    monkeypatch.setattr(
        "backend.models.database.async_session",
        lambda: pytest.fail("invalid Setup values must not open a DB session"),
    )

    with pytest.raises(SystemConfigValidationError):
        await service.save_configs_to_db(
            {"NOTIFICATION_RATE_LIMIT_SECONDS": "nan"}
        )


@pytest.mark.asyncio
async def test_identity_migration_tolerates_multiple_github_identities(sqlite_db):
    user = TelegramUser(id=1, telegram_id=1001, github_username="alice")
    sqlite_db.add(user)
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="github-alice-1",
            provider_username="alice",
        )
    )
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="github-alice-2",
            provider_username="alice-work",
        )
    )
    await sqlite_db.commit()

    first = await migrate_legacy_identity_data(sqlite_db)
    second = await migrate_legacy_identity_data(sqlite_db)

    identities = (
        (await sqlite_db.execute(select(UserIdentity).order_by(UserIdentity.id)))
        .scalars()
        .all()
    )
    assert first["identities_created"] == 0
    assert second["identities_created"] == 0
    assert [row.provider_user_id for row in identities] == [
        "github-alice-1",
        "github-alice-2",
    ]


@pytest.mark.asyncio
async def test_identity_migration_skips_ambiguous_alias_but_migrates_unrelated_rows(
    sqlite_db,
):
    for user in (
        TelegramUser(id=1, telegram_id=1001, github_username="Alice"),
        TelegramUser(id=2, telegram_id=1002, github_username="alice"),
        TelegramUser(id=3, telegram_id=1003, github_username="bob"),
    ):
        sqlite_db.add(user)
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="legacy:alice",
            provider_username="Alice",
        )
    )
    await sqlite_db.commit()

    result = await migrate_legacy_identity_data(sqlite_db)

    assert result["ambiguous_github_usernames"] == ["alice"]
    identities = (
        await sqlite_db.execute(select(UserIdentity).order_by(UserIdentity.id))
    ).scalars().all()
    assert [row.provider_user_id for row in identities] == [
        "legacy:alice",
        "legacy:bob",
    ]
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "telegram"
            )
        )
    ).scalars().all()
    assert {row.address for row in endpoints} == {"1001", "1002", "1003"}


@pytest.mark.asyncio
async def test_identity_migration_adds_telegram_when_email_endpoint_exists(sqlite_db):
    user = TelegramUser(
        id=1,
        telegram_id=1101,
        github_username="alice",
        email="alice@example.invalid",
        email_verified=True,
    )
    sqlite_db.add(user)
    await sqlite_db.flush()
    sqlite_db.add(
        NotificationEndpoint(
            user_id=1,
            provider="email",
            address="alice@example.invalid",
            verified=True,
            enabled=True,
        )
    )
    await sqlite_db.commit()

    await migrate_legacy_identity_data(sqlite_db)

    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.id)
        )
    ).scalars().all()
    assert [(row.provider, row.address, row.enabled) for row in endpoints] == [
        ("email", "alice@example.invalid", True),
        ("telegram", "1101", True),
    ]


@pytest.mark.asyncio
async def test_restore_preserves_legacy_fk_and_routes_new_telegram_endpoint(sqlite_db, monkeypatch):
    user = TelegramUser(id=1, telegram_id=111, github_username="alice")
    sqlite_db.add(user)
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="github-alice",
            provider_username="alice",
        )
    )
    old_endpoint = NotificationEndpoint(
        user_id=1,
        provider="telegram",
        address="111",
        verified=True,
        enabled=True,
    )
    sqlite_db.add(old_endpoint)
    sqlite_db.add(UserRepoSubscription(telegram_id=111, repo_name="owner/repo"))
    await sqlite_db.commit()

    document = _backup_document(
        telegram_id=222,
        endpoints=[
            {
                "provider": "telegram",
                "address": "222",
                "verified": True,
                "enabled": True,
            }
        ],
    )
    result = await restore_user_backup(sqlite_db, document)
    assert result.users_updated == 0 or result.users_updated == 1

    restored_user = await sqlite_db.get(TelegramUser, 1)
    assert restored_user.telegram_id == 111
    subscription = (
        await sqlite_db.execute(select(UserRepoSubscription))
    ).scalars().one()
    assert subscription.telegram_id == 111
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.address)
        )
    ).scalars().all()
    assert [(row.address, row.enabled) for row in endpoints] == [
        ("111", False),
        ("222", True),
    ]

    monkeypatch.setattr(
        "backend.models.database.async_session", lambda: sqlite_db
    )
    sender = NotificationSender(object())
    assert await sender._enabled_telegram_targets([111]) == [222]

    # Startup migration sees the retained disabled mirror and must not
    # reactivate it or create another endpoint for the old address.
    await migrate_legacy_identity_data(sqlite_db)
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.address)
        )
    ).scalars().all()
    assert [(row.address, row.enabled) for row in endpoints] == [
        ("111", False),
        ("222", True),
    ]


@pytest.mark.asyncio
async def test_restore_identity_only_telegram_change_disables_old_endpoint(sqlite_db):
    user = TelegramUser(id=1, telegram_id=111, github_username="alice")
    sqlite_db.add(user)
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="github-alice",
            provider_username="alice",
        )
    )
    sqlite_db.add(
        NotificationEndpoint(
            user_id=1,
            provider="telegram",
            address="111",
            verified=True,
            enabled=True,
        )
    )
    await sqlite_db.commit()

    await restore_user_backup(
        sqlite_db,
        _backup_document(telegram_id=222, endpoints=None),
    )
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.address)
        )
    ).scalars().all()
    assert [(row.address, row.enabled) for row in endpoints] == [
        ("111", False),
        ("222", True),
    ]


@pytest.mark.asyncio
async def test_restore_respects_explicit_disabled_telegram_endpoint(sqlite_db):
    user = TelegramUser(id=1, telegram_id=111, github_username="alice")
    sqlite_db.add(user)
    sqlite_db.add(
        UserIdentity(
            user_id=1,
            provider="github",
            provider_user_id="github-alice",
            provider_username="alice",
        )
    )
    sqlite_db.add(
        NotificationEndpoint(
            user_id=1,
            provider="telegram",
            address="111",
            verified=True,
            enabled=True,
        )
    )
    await sqlite_db.commit()

    await restore_user_backup(
        sqlite_db,
        _backup_document(
            telegram_id=222,
            endpoints=[
                {
                    "provider": "telegram",
                    "address": "222",
                    "verified": True,
                    "enabled": False,
                }
            ],
        ),
    )
    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).order_by(NotificationEndpoint.address)
        )
    ).scalars().all()
    assert [(row.address, row.enabled) for row in endpoints] == [
        ("111", False),
        ("222", False),
    ]


@pytest.mark.asyncio
async def test_restore_multiple_enabled_telegram_endpoints_keeps_one_deterministically(
    sqlite_db,
):
    document = _backup_document(
        telegram_id=222,
        endpoints=[
            {
                "provider": "telegram",
                "address": "333",
                "verified": True,
                "enabled": True,
            },
            {
                "provider": "telegram",
                "address": "222",
                "verified": True,
                "enabled": True,
            },
        ],
    )

    await restore_user_backup(sqlite_db, document)

    endpoints = (
        await sqlite_db.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.provider == "telegram"
            )
        )
    ).scalars().all()
    assert [(row.address, row.enabled) for row in endpoints] == [
        ("333", False),
        ("222", True),
    ]
