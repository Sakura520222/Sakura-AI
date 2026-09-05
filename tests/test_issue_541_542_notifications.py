"""Focused regression coverage for the identity, backup and announcement slices."""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import BigInteger, Column
from sqlalchemy.dialects import mysql, postgresql

import backend.telegram.bot as telegram_bot_module
from backend.api.v1 import setup as setup_api
from backend.api.v1.announcements import router as announcements_api_router
from backend.api.v1.schemas import (
    UserCreateRequest,
    UserInfoUpdateRequest,
    UserResponse,
)
from backend.models import database as database_module
from backend.models.announcement_models import (
    Announcement,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.database import UserConfig, WebUIConfig
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)
from backend.services import (
    announcement_service,
    notification_service,
    telegram_binding_service,
)
from backend.services.auth_service import GitHubOAuthProvider, select_github_email
from backend.services.config_backup_service import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    SYSTEM_SECTION,
    ConfigBackupError,
    parse_config_backup,
)
from backend.services.identity_service import (
    GitHubAccount,
    bind_notification_endpoint,
    migrate_legacy_identity_data,
    upsert_github_account,
)
from backend.services.user_backup_service import (
    USER_BACKUP_FORMAT,
    UserBackupError,
    parse_user_backup,
    restore_user_backup,
)
from backend.telegram import handlers as telegram_handlers
from backend.telegram.notifications import NotificationSender
from backend.webui.deps import require_super_admin
from backend.webui.routes.announcements import router as announcements_webui_router


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("test result unexpectedly contains duplicates")
        return self._rows[0] if self._rows else None


def test_legacy_telegram_column_upgrade_is_mysql_only_and_idempotent(monkeypatch):
    """The compatibility migration changes nullability without dropping rows."""

    class Inspector:
        def __init__(self, nullable):
            self.nullable = nullable

        def has_table(self, name):
            assert name == "telegram_users"
            return True

        def get_columns(self, name):
            assert name == "telegram_users"
            return [{"name": "telegram_id", "nullable": self.nullable}]

    class Connection:
        def __init__(self, dialect):
            self.dialect = dialect
            self.statements = []

        async def run_sync(self, callback):
            return callback(object())

        async def execute(self, statement):
            self.statements.append(str(statement))

    inspector = Inspector(False)
    monkeypatch.setattr("sqlalchemy.inspect", lambda _bind: inspector)

    async def exercise():
        connection = Connection(mysql.dialect())
        await database_module._ensure_legacy_telegram_id_nullable(
            connection, SimpleNamespace(info=lambda *_args: None)
        )
        assert len(connection.statements) == 1
        assert "MODIFY COLUMN `telegram_id` BIGINT NULL" in connection.statements[0]

        # A second run sees the migrated nullable column and emits no DDL.
        inspector.nullable = True
        await database_module._ensure_legacy_telegram_id_nullable(
            connection, SimpleNamespace(info=lambda *_args: None)
        )
        assert len(connection.statements) == 1

        postgres = Connection(postgresql.dialect())
        inspector.nullable = False
        await database_module._ensure_legacy_telegram_id_nullable(
            postgres, SimpleNamespace(info=lambda *_args: None)
        )
        assert postgres.statements == [
            'ALTER TABLE "telegram_users" '
            'ALTER COLUMN "telegram_id" DROP NOT NULL'
        ]

    import asyncio

    asyncio.run(exercise())


def test_add_column_sql_quotes_mysql_and_postgresql_differently():
    column = Column("telegram_id", BigInteger, nullable=True)
    mysql_sql = database_module._build_add_column_sql(
        mysql.dialect(), "telegram_users", column
    )
    postgres_sql = database_module._build_add_column_sql(
        postgresql.dialect(), "telegram_users", column
    )
    assert mysql_sql == (
        "ALTER TABLE telegram_users ADD COLUMN telegram_id BIGINT NULL"
    )
    assert postgres_sql == (
        "ALTER TABLE telegram_users ADD COLUMN telegram_id BIGINT NULL"
    )
    assert "`" not in postgres_sql


class _IdentityMigrationSession:
    def __init__(self):
        self.users = [
            TelegramUser(
                id=1,
                telegram_id=1234,
                github_username="Alice",
                is_active=True,
            )
        ]
        self.identities = []
        self.endpoints = []
        self.commits = 0

    async def execute(self, query):
        entity = query.column_descriptions[0]["entity"]
        params = query.compile().params
        if entity is TelegramUser:
            rows = self.users
        elif entity is UserIdentity:
            rows = self.identities
            if "user_id_1" in params:
                rows = [row for row in rows if row.user_id == params["user_id_1"]]
            elif "provider_user_id_1" in params:
                rows = [
                    row
                    for row in rows
                    if row.provider_user_id == params["provider_user_id_1"]
                ]
        elif entity is NotificationEndpoint:
            rows = [
                row
                for row in self.endpoints
                if row.provider == params.get("provider_1")
                and row.address == params.get("address_1")
            ]
        else:
            raise AssertionError(entity)
        return _Rows(rows)

    def add(self, row):
        if isinstance(row, TelegramUser):
            row.id = max((user.id or 0 for user in self.users), default=0) + 1
            self.users.append(row)
        elif isinstance(row, UserIdentity):
            self.identities.append(row)
        elif isinstance(row, NotificationEndpoint):
            self.endpoints.append(row)
        else:
            raise AssertionError(type(row))

    async def get(self, model, row_id):
        rows = self.users if model is TelegramUser else self.identities
        return next((row for row in rows if row.id == row_id), None)

    async def flush(self):
        return None

    async def refresh(self, _row):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("migration should not roll back")


@pytest.mark.asyncio
async def test_legacy_identity_backfill_is_idempotent_and_preserves_telegram_user():
    session = _IdentityMigrationSession()

    first = await migrate_legacy_identity_data(session)
    second = await migrate_legacy_identity_data(session)

    assert first == {"identities_created": 1, "endpoints_created": 1, "conflicts": 0}
    assert second == {"identities_created": 0, "endpoints_created": 0, "conflicts": 0}
    assert session.users[0].id == 1
    assert session.users[0].telegram_id == 1234
    assert len(session.identities) == 1
    assert len(session.endpoints) == 1
    assert session.endpoints[0].address == "1234"


@pytest.mark.asyncio
async def test_github_provider_id_reuses_user_when_username_changes():
    session = _IdentityMigrationSession()
    session.identities.append(
        UserIdentity(
            id=1,
            user_id=1,
            provider="github",
            provider_user_id="42",
            provider_username="Alice",
        )
    )

    user = await upsert_github_account(
        session,
        GitHubAccount(provider_user_id="42", username="AliceRenamed"),
    )

    assert user is session.users[0]
    assert user.id == 1
    assert user.github_username == "AliceRenamed"
    assert session.identities[0].provider_username == "AliceRenamed"
    assert len(session.users) == 1


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeOAuthClient:
    def __init__(self):
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **_kwargs):
        self.urls.append(url)
        return _FakeResponse(200, {"access_token": "access-token"})

    async def get(self, url, **_kwargs):
        self.urls.append(url)
        if url.endswith("/user"):
            return _FakeResponse(
                200,
                {
                    "id": 42,
                    "login": "RenamedAlice",
                    "email": "private@example.invalid",
                    "avatar_url": "https://avatars.invalid/42",
                },
            )
        return _FakeResponse(
            200,
            [
                {"email": "unverified@example.invalid", "primary": True, "verified": False},
                {"email": "primary@example.invalid", "primary": True, "verified": True},
            ],
        )


@pytest.mark.asyncio
async def test_github_oauth_fetches_verified_primary_email(monkeypatch):
    client = _FakeOAuthClient()
    monkeypatch.setattr(
        "backend.services.auth_service.httpx.AsyncClient", lambda: client
    )
    settings = SimpleNamespace(
        github_oauth_client_id="client",
        github_oauth_client_secret="secret",
        github_oauth_token_url="https://oauth.invalid/token",
        github_oauth_user_url="https://api.invalid/user",
        github_oauth_emails_url="https://api.invalid/user/emails",
    )

    result = await GitHubOAuthProvider(settings).exchange_code("code")

    assert result.account.provider_user_id == "42"
    assert result.account.username == "RenamedAlice"
    assert result.account.email == "primary@example.invalid"
    assert result.account.email_verified is True
    assert client.urls == [
        "https://oauth.invalid/token",
        "https://api.invalid/user",
        "https://api.invalid/user/emails",
    ]


def test_github_email_selection_allows_login_without_an_email():
    assert select_github_email({"email": None}, []) == (None, False)
    email, verified = select_github_email(
        {}, [{"email": "User@Example.com", "verified": False, "primary": False}]
    )
    assert email == "user@example.com"
    assert verified is False


def _user_backup_payload(users, *, version=1):
    return json.dumps(
        {
            "format": USER_BACKUP_FORMAT,
            "version": version,
            "scope": "users",
            "user_count": len(users),
            "users": users,
        }
    ).encode()


def test_user_backup_v1_fields_are_migrated_to_v2_shape():
    parsed = parse_user_backup(
        _user_backup_payload(
            [
                {
                    "telegram_id": 1001,
                    "github_username": "Alice",
                    "profile": {"role": "user", "is_active": True},
                }
            ]
        )
    )
    assert parsed["version"] == 2
    assert parsed["users"][0]["identity"] == {
        "telegram_id": 1001,
        "github_username": "Alice",
        "email": None,
        "email_verified": False,
    }


def test_user_backup_v2_supports_external_only_users_and_detects_endpoint_conflicts():
    users = [
        {
            "identity": {},
            "identities": [
                {
                    "provider": "passkey",
                    "provider_user_id": "credential-a",
                }
            ],
            "notification_endpoints": [
                {"provider": "email", "address": "same@example.invalid"}
            ],
        },
        {
            "identity": {"telegram_id": 1002},
            "notification_endpoints": [
                {"provider": "email", "address": "same@example.invalid"}
            ],
        },
    ]
    with pytest.raises(UserBackupError, match="通知端点 email:same@example.invalid 重复"):
        parse_user_backup(_user_backup_payload(users, version=2))

    parsed = parse_user_backup(
        _user_backup_payload([users[0]], version=2)
    )
    assert parsed["users"][0]["identity"]["telegram_id"] is None
    assert parsed["users"][0]["identities"][0]["provider"] == "passkey"


class _ExternalRestoreSession:
    """Small AsyncSession double that includes the v2 identity tables."""

    def __init__(self):
        self.users = []
        self.identities = []
        self.endpoints = []
        self.configs = []
        self.webuis = []
        self.recoveries = []
        self.passkeys = []
        self.pending = []
        self.next_id = 1

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = {
            TelegramUser: self.users,
            UserIdentity: self.identities,
            NotificationEndpoint: self.endpoints,
            UserConfig: self.configs,
            WebUIConfig: self.webuis,
            UserRecoveryCode: self.recoveries,
            UserWebAuthnCredential: self.passkeys,
        }.get(model)
        if rows is None:
            raise AssertionError(model)
        return _Rows(rows)

    def add(self, row):
        self.pending.append(row)

    async def flush(self):
        for row in self.pending:
            if getattr(row, "id", None) is None:
                row.id = self.next_id
                self.next_id += 1
            rows = {
                TelegramUser: self.users,
                UserIdentity: self.identities,
                NotificationEndpoint: self.endpoints,
                UserConfig: self.configs,
                WebUIConfig: self.webuis,
                UserRecoveryCode: self.recoveries,
                UserWebAuthnCredential: self.passkeys,
            }[type(row)]
            if row not in rows:
                rows.append(row)
        self.pending.clear()

    async def delete(self, row):
        for rows in (
            self.users,
            self.identities,
            self.endpoints,
            self.configs,
            self.webuis,
            self.recoveries,
            self.passkeys,
        ):
            if row in rows:
                rows.remove(row)

    async def commit(self):
        await self.flush()

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_restore_external_identity_only_user_creates_internal_user_and_identity():
    document = _user_backup_payload(
        [
            {
                "identity": {},
                "identities": [
                    {
                        "provider": "passkey",
                        "provider_user_id": "credential-only",
                    }
                ],
            }
        ],
        version=2,
    )
    session = _ExternalRestoreSession()

    result = await restore_user_backup(session, parse_user_backup(document))

    assert result.users_created == 1
    assert len(session.users) == 1
    assert session.users[0].telegram_id is None
    assert len(session.identities) == 1
    assert session.identities[0].user_id == session.users[0].id
    assert session.identities[0].provider == "passkey"


def test_config_backup_maps_legacy_smtp_alias_without_exposing_new_secret_name():
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": "2026-09-03T00:00:00.000000Z",
        "scope": SYSTEM_SECTION,
        "sections": {
            SYSTEM_SECTION: {
                "count": 1,
                "configs": [
                    {"key": "smtp_pass", "value": "secret", "description": None}
                ],
            }
        },
    }
    parsed = parse_config_backup(json.dumps(payload).encode())
    assert parsed[SYSTEM_SECTION][0].key == "smtp_password"
    assert parsed[SYSTEM_SECTION][0].value == "secret"


class _NotificationSession:
    def __init__(self, deliveries, endpoints):
        self.deliveries = deliveries
        self.endpoints = endpoints
        self.commits = 0

    async def execute(self, query):
        # The delivery worker uses conditional UPDATE statements for both
        # claims and terminal writes.  Keep this adapter deliberately small,
        # but evaluate the same row id/token/version predicates so these
        # tests do not create a fail-open path in production code.
        if getattr(query, "is_update", False):
            values = {
                getattr(column, "name", str(column)): getattr(value, "value", value)
                for column, value in query._values.items()
            }
            predicates = {}

            def collect(criteria):
                left = getattr(criteria, "left", None)
                right = getattr(criteria, "right", None)
                left_name = getattr(left, "name", None)
                left_table = getattr(getattr(left, "table", None), "name", None)
                right_value = getattr(right, "value", None)
                if left_table == NotificationDelivery.__tablename__ and left_name:
                    if right_value is not None:
                        predicates[left_name] = right_value
                for child in getattr(criteria, "clauses", ()):
                    collect(child)

            for criteria in query._where_criteria:
                collect(criteria)
            row = next(
                (
                    item
                    for item in self.deliveries
                    if item.id == predicates.get("id", item.id)
                ),
                None,
            )
            if row is None:
                return _UpdateResult(0)
            if row.status not in {
                DeliveryStatus.PENDING.value,
                DeliveryStatus.FAILED.value,
            }:
                return _UpdateResult(0)
            if "publication_version" in predicates and getattr(
                row, "publication_version", 1
            ) not in (None, predicates["publication_version"]):
                return _UpdateResult(0)
            if "claim_token" in predicates and getattr(
                row, "claim_token", None
            ) != predicates["claim_token"]:
                return _UpdateResult(0)
            if "claim_token" in values and values["claim_token"] is not None:
                if getattr(row, "claim_token", None) and getattr(
                    row, "claim_until", None
                ):
                    return _UpdateResult(0)
            for key, value in values.items():
                setattr(row, key, value)
            return _UpdateResult(1)
        entity = query.column_descriptions[0]["entity"]
        if entity is NotificationDelivery:
            return _Rows(self.deliveries)
        if entity is NotificationEndpoint:
            return _Rows(self.endpoints)
        raise AssertionError(entity)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


class _UpdateResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FailingProvider:
    channel = "email"

    def __init__(self):
        self.calls = 0

    async def send(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("smtp-password-secret")


class _WorkingProvider:
    channel = "web"

    def __init__(self):
        self.calls = 0

    async def send(self, **_kwargs):
        self.calls += 1


@pytest.mark.asyncio
async def test_notification_provider_failure_isolated_and_retried(monkeypatch):
    failed_provider = _FailingProvider()
    working_provider = _WorkingProvider()
    registry = notification_service.NotificationProviderRegistry(
        {"email": failed_provider, "web": working_provider}
    )
    service = notification_service.NotificationService(registry)
    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_password="smtp-password-secret",
            telegram_bot_token="telegram-secret",
            notification_retry_max_attempts=3,
            notification_retry_initial_delay_seconds=0,
            notification_retry_backoff_factor=1,
            notification_rate_limit_seconds=0,
            notification_max_concurrency=2,
        ),
    )
    announcement = Announcement(
        id=1, title="Title", content="Body", status="published"
    )
    failed = NotificationDelivery(
        id=1,
        announcement_id=1,
        user_id=1,
        channel="email",
        status=DeliveryStatus.PENDING.value,
        attempts=0,
    )
    working = NotificationDelivery(
        id=2,
        announcement_id=1,
        user_id=1,
        channel="web",
        status=DeliveryStatus.PENDING.value,
        attempts=0,
    )
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="email",
        address="user@example.invalid",
        enabled=True,
    )
    session = _NotificationSession([failed, working], [endpoint])

    result = await service.broadcast_announcement(session, announcement)

    assert result == {"sent": 1, "failed": 1, "skipped": 0}
    assert failed_provider.calls == 3
    assert working_provider.calls == 1
    assert failed.status == DeliveryStatus.FAILED.value
    assert failed.attempts == 3
    assert "smtp-password-secret" not in failed.error_message
    assert "***" in failed.error_message
    # A claim and each lease heartbeat are committed independently from the
    # terminal state; the old fixture counted only the two terminal commits.
    assert session.commits >= 2


@pytest.mark.asyncio
async def test_notification_retry_honors_telegram_retry_after(monkeypatch):
    class _RetryProvider:
        channel = "telegram"

        def __init__(self):
            self.calls = 0

        async def send(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise notification_service.NotificationProviderRetryAfter(
                    "rate limited", 7
                )

    provider = _RetryProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"telegram": provider})
    )
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notification_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_password=None,
            telegram_bot_token=None,
            notification_retry_max_attempts=2,
            notification_retry_initial_delay_seconds=1,
            notification_retry_backoff_factor=2,
            notification_rate_limit_seconds=0,
        ),
    )
    announcement = Announcement(
        id=1, title="Title", content="Body", status="published"
    )
    delivery = NotificationDelivery(
        id=1,
        announcement_id=1,
        user_id=1,
        channel="telegram",
        status=DeliveryStatus.PENDING.value,
        attempts=0,
    )
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="telegram",
        address="123",
        enabled=True,
    )
    session = _NotificationSession([delivery], [endpoint])

    sent = await service._deliver_one(
        session, delivery, announcement, endpoint, __import__("asyncio").Lock()
    )

    assert sent is True
    assert provider.calls == 2
    assert sleeps == [7]
    assert delivery.status == DeliveryStatus.SENT.value


@pytest.mark.asyncio
async def test_notification_sender_maps_legacy_mirror_to_rebound_endpoint(monkeypatch):
    class _Result:
        def all(self):
            return [("222", 111)]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return _Result()

    class _Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    monkeypatch.setattr(database_module, "async_session", _Session)
    bot = _Bot()
    sender = NotificationSender(bot)

    await sender.send_to_targets("hello", [111])

    assert [item["chat_id"] for item in bot.sent] == [222]


@pytest.mark.asyncio
async def test_telegram_binding_token_memory_fallback_is_single_use(monkeypatch):
    async def unavailable_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        telegram_binding_service,
        "get_async_redis",
        unavailable_redis,
    )
    monkeypatch.setattr(
        telegram_binding_service,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_bind_token_expire_seconds=300,
            telegram_bot_username="sakura_test_bot",
        ),
    )
    telegram_binding_service._binding_fallback.clear()

    binding = await telegram_binding_service.create_telegram_binding_token(42)

    assert binding.deep_link.startswith("https://t.me/sakura_test_bot?start=")
    assert await telegram_binding_service.consume_telegram_binding_token(binding.token) == 42
    assert await telegram_binding_service.consume_telegram_binding_token(binding.token) is None
    telegram_binding_service._binding_fallback.clear()


@pytest.mark.asyncio
async def test_telegram_bind_rejects_group_before_consuming_token(monkeypatch):
    consumed = []

    async def fake_consume(token):
        consumed.append(token)
        return 42

    monkeypatch.setattr(telegram_handlers, "consume_telegram_binding_token", fake_consume)

    class _Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text):
            self.replies.append(text)

    message = _Message()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(type="group", id=111),
        effective_user=SimpleNamespace(id=111),
        message=message,
    )
    context = SimpleNamespace(args=["tg_bind_" + "A" * 32])

    await telegram_handlers.cmd_bind(update, context)

    assert consumed == []
    assert message.replies
    assert "私聊" in message.replies[0]


@pytest.mark.asyncio
async def test_telegram_binding_replaces_previous_endpoint_deterministically():
    class _EndpointSession:
        def __init__(self):
            self.user = TelegramUser(id=1, telegram_id=111, is_active=True)
            self.endpoints = [
                NotificationEndpoint(
                    id=1,
                    user_id=1,
                    provider="telegram",
                    address="111",
                    enabled=True,
                )
            ]

        async def get(self, model, row_id):
            if model is TelegramUser and row_id == 1:
                return self.user
            if model is NotificationEndpoint:
                return next((row for row in self.endpoints if row.id == row_id), None)
            return None

        async def execute(self, query):
            model = query.column_descriptions[0]["entity"]
            if model is not NotificationEndpoint:
                raise AssertionError(model)
            params = query.compile().params
            rows = self.endpoints
            if "address_1" in params:
                rows = [
                    row
                    for row in rows
                    if row.provider == params.get("provider_1")
                    and row.address == params.get("address_1")
                ]
            else:
                rows = [
                    row
                    for row in rows
                    if row.user_id == params.get("user_id_1")
                    and row.provider == params.get("provider_1")
                    and row.enabled
                    and row.id != params.get("id_1")
                ]
            return _Rows(rows)

        def add(self, row):
            row.id = max((item.id for item in self.endpoints), default=0) + 1
            self.endpoints.append(row)

        async def commit(self):
            return None

        async def refresh(self, _row):
            return None

        async def rollback(self):
            return None

    session = _EndpointSession()

    await bind_notification_endpoint(session, 1, "telegram", "222", verified=True)
    await bind_notification_endpoint(session, 1, "telegram", "333", verified=True)

    assert [(row.address, row.enabled) for row in session.endpoints] == [
        ("111", False),
        ("222", False),
        ("333", True),
    ]


@pytest.mark.asyncio
async def test_announcement_scheduler_uses_runtime_supervisor_helper(monkeypatch):
    captured = {}

    def fake_scheduler(awaitable, source):
        captured["source"] = source
        awaitable.close()
        return "registered-task"

    monkeypatch.setattr(
        announcement_service,
        "create_registered_background_task",
        fake_scheduler,
    )

    result = announcement_service.schedule_announcement_broadcast(7)

    assert result == "registered-task"
    assert captured == {"source": "announcement.broadcast"}


def test_webui_announcement_admin_route_precedes_dynamic_detail_route():
    paths = [route.path for route in announcements_webui_router.routes]
    assert paths.index("/announcements/admin") < paths.index(
        "/announcements/{announcement_id}"
    )


def test_nullable_telegram_api_models_accept_github_only_users():
    user = TelegramUser(id=9, telegram_id=None, role="user")
    response = UserResponse.model_validate(user, from_attributes=True)

    assert response.telegram_id is None
    assert UserCreateRequest(github_username="alice").telegram_id is None
    assert UserInfoUpdateRequest(github_username="alice").telegram_id is None


def test_system_config_notification_keys_have_bilingual_dynamic_labels():
    from backend.services.system_config_service import SYSTEM_CONFIG_GROUPS

    root = Path(__file__).parents[1] / "backend/webui/translations"
    for locale in ("zh-CN.yaml", "en.yaml"):
        catalog = yaml.safe_load((root / locale).read_text(encoding="utf-8"))[
            "system_config"
        ]
        for group in SYSTEM_CONFIG_GROUPS:
            assert f"group_{group['id']}" in catalog
            assert f"group_{group['id']}_desc" in catalog
            for key in group["keys"]:
                assert f"key_{key}" in catalog
                assert f"key_{key}_desc" in catalog


@pytest.mark.asyncio
async def test_announcement_lifecycle_rejects_mutation_after_publish(monkeypatch):
    announcement = Announcement(
        id=1,
        title="Draft",
        content="Body",
        status="draft",
    )
    scheduled = []
    session = SimpleNamespace(
        add=lambda _row: None,
        flush=lambda: None,
        commit=lambda: None,
        refresh=lambda _row: None,
    )

    async def fake_get(_db, _announcement_id):
        return announcement

    async def fake_commit():
        return None

    async def fake_refresh(_row):
        return None

    async def fake_flush(*_args):
        return None

    session.flush = fake_flush
    session.commit = fake_commit
    session.refresh = fake_refresh
    monkeypatch.setattr(announcement_service, "get_announcement", fake_get)
    monkeypatch.setattr(
        announcement_service, "_get_lifecycle_announcement", fake_get
    )
    monkeypatch.setattr(announcement_service, "_ensure_delivery_rows", fake_flush)
    monkeypatch.setattr(
        announcement_service,
        "_ensure_publication_snapshot",
        fake_flush,
    )
    monkeypatch.setattr(
        announcement_service,
        "schedule_announcement_broadcast",
        lambda announcement_id, **_kwargs: scheduled.append(announcement_id),
    )

    await announcement_service.publish_announcement(session, 1)
    assert announcement.status == "published"
    assert scheduled == [1]

    with pytest.raises(ValueError, match="已发布公告不可直接修改"):
        await announcement_service.update_announcement(session, 1, title="Changed")
    with pytest.raises(ValueError, match="已发布公告不可删除"):
        await announcement_service.delete_announcement(session, 1)


@pytest.mark.asyncio
async def test_announcement_withdraw_edit_and_republish(monkeypatch):
    announcement = Announcement(
        id=1,
        title="Original",
        content="Body",
        status="published",
    )
    session = SimpleNamespace()
    session.commit = lambda: None
    session.refresh = lambda _row: None

    async def fake_commit():
        return None

    async def fake_refresh(_row):
        return None

    async def fake_flush(*_args, **_kwargs):
        return None

    session.commit = fake_commit
    session.refresh = fake_refresh
    session.flush = fake_flush

    async def fake_get(_db, _announcement_id):
        return announcement

    monkeypatch.setattr(announcement_service, "get_announcement", fake_get)
    monkeypatch.setattr(
        announcement_service, "_get_lifecycle_announcement", fake_get
    )
    monkeypatch.setattr(
        announcement_service,
        "_archive_publication",
        fake_flush,
    )
    monkeypatch.setattr(
        announcement_service,
        "_archive_and_reset_publication",
        lambda *_args: fake_flush(),
    )
    monkeypatch.setattr(
        announcement_service,
        "_ensure_publication_snapshot",
        fake_flush,
    )
    monkeypatch.setattr(
        announcement_service,
        "_ensure_delivery_rows",
        lambda *_args: fake_flush(),
    )
    scheduled = []
    monkeypatch.setattr(
        announcement_service,
        "schedule_announcement_broadcast",
        lambda announcement_id, **_kwargs: scheduled.append(announcement_id),
    )

    await announcement_service.withdraw_announcement(session, 1)
    assert announcement.status == "withdrawn"
    await announcement_service.update_announcement(session, 1, title="Updated")
    assert announcement.title == "Updated"
    await announcement_service.publish_announcement(
        session, 1, schedule_broadcast=True
    )

    assert announcement.status == "published"
    assert scheduled == [1]


def test_announcement_admin_routes_require_super_admin():
    admin_routes = [
        route
        for route in announcements_api_router.routes
        if "/admin" in route.path
    ]
    assert admin_routes
    for route in admin_routes:
        assert any(
            dependency.call is not None
            and dependency.call.__name__ == require_super_admin.__name__
            for dependency in route.dependant.dependencies
        ) or any(
            dependency.call is not None
            and dependency.call.__name__ == "require_api_super_admin"
            for dependency in route.dependant.dependencies
        )


@pytest.mark.asyncio
async def test_setup_state_uses_shared_required_fields_without_telegram_requirement(
    monkeypatch,
):
    async def missing_fields():
        return ["GITHUB_APP_ID"]

    async def current_step():
        return 1

    monkeypatch.setattr(setup_api, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(setup_api, "get_missing_fields", missing_fields)
    monkeypatch.setattr(setup_api, "get_current_step", current_step)

    response = await setup_api.get_setup_state()
    payload = json.loads(response.body)

    assert payload["data"]["current_step"] == 1
    assert payload["data"]["missing_fields"] == ["GITHUB_APP_ID"]
    assert "TELEGRAM_BOT_TOKEN" not in payload["data"]["missing_fields"]


def test_config_backup_maps_legacy_smtp_tls_alias_to_security_mode():
    def _payload(value):
        return {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "exported_at": "2026-09-03T00:00:00.000000Z",
            "scope": SYSTEM_SECTION,
            "sections": {
                SYSTEM_SECTION: {
                    "count": 1,
                    "configs": [
                        {"key": "smtp_tls", "value": value, "description": None}
                    ],
                }
            },
        }

    parsed = parse_config_backup(json.dumps(_payload("True")).encode())
    assert parsed[SYSTEM_SECTION][0].key == "smtp_security"
    assert parsed[SYSTEM_SECTION][0].value == "starttls"

    parsed = parse_config_backup(json.dumps(_payload("false")).encode())
    assert parsed[SYSTEM_SECTION][0].value == "none"

    parsed = parse_config_backup(json.dumps(_payload("SSL")).encode())
    assert parsed[SYSTEM_SECTION][0].value == "ssl"

    with pytest.raises(ConfigBackupError):
        parse_config_backup(json.dumps(_payload("maybe")).encode())


_STARTTLS_NOT_CALLED = object()


class _FakeSMTPSession:
    """Records how EmailNotificationProvider drives the smtplib API."""

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.constructor_context = context
        self.starttls_context = _STARTTLS_NOT_CALLED
        self.logins = []
        self.sent_messages = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def starttls(self, *, context=None):
        self.starttls_context = context
        return "220 ready"

    def login(self, username, password):
        self.logins.append((username, password))

    def send_message(self, message):
        self.sent_messages.append(message)


def _patch_fake_smtp(monkeypatch) -> dict[str, list[_FakeSMTPSession]]:
    created: dict[str, list[_FakeSMTPSession]] = {"smtp": [], "smtp_ssl": []}

    def _factory(bucket):
        def create(*args, **kwargs):
            session = _FakeSMTPSession(*args, **kwargs)
            created[bucket].append(session)
            return session

        return create

    monkeypatch.setattr(notification_service.smtplib, "SMTP", _factory("smtp"))
    monkeypatch.setattr(
        notification_service.smtplib, "SMTP_SSL", _factory("smtp_ssl")
    )
    return created


_EMAIL_MARKDOWN_CONTENT = "# 发布说明\n\n**加粗要点** 与 `inline-code`\n\n- 列表项"


async def _send_email(monkeypatch, **smtp_overrides) -> dict[str, list]:
    created = _patch_fake_smtp(monkeypatch)
    values = {
        "email_enabled": True,
        "smtp_host": "smtp.example.invalid",
        "smtp_from": "noreply@example.invalid",
        "smtp_username": "user",
        "smtp_password": "secret",
        "smtp_port": 465,
        "smtp_security": "starttls",
    }
    values.update(smtp_overrides)
    monkeypatch.setattr(
        notification_service, "get_settings", lambda: SimpleNamespace(**values)
    )
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="email",
        address="dest@example.invalid",
        enabled=True,
    )
    await notification_service.EmailNotificationProvider().send(
        endpoint=endpoint,
        title="标题",
        content=_EMAIL_MARKDOWN_CONTENT,
        content_html=announcement_service.sanitize_markdown(
            _EMAIL_MARKDOWN_CONTENT
        ),
        announcement_type="release",
    )
    return created


@pytest.mark.asyncio
async def test_email_provider_implicit_tls_uses_smtp_ssl(monkeypatch):
    created = await _send_email(monkeypatch, smtp_security="ssl")

    assert len(created["smtp_ssl"]) == 1
    assert created["smtp"] == []
    session = created["smtp_ssl"][0]
    assert session.host == "smtp.example.invalid"
    assert session.port == 465
    assert isinstance(session.constructor_context, ssl.SSLContext)
    assert session.starttls_context is _STARTTLS_NOT_CALLED
    assert session.logins == [("user", "secret")]
    assert len(session.sent_messages) == 1


@pytest.mark.asyncio
async def test_email_provider_starttls_upgrades_plain_connection(monkeypatch):
    created = await _send_email(monkeypatch, smtp_security="starttls", smtp_port=587)

    assert len(created["smtp"]) == 1
    assert created["smtp_ssl"] == []
    session = created["smtp"][0]
    assert session.port == 587
    assert isinstance(session.starttls_context, ssl.SSLContext)
    assert session.logins == [("user", "secret")]
    assert len(session.sent_messages) == 1


@pytest.mark.asyncio
async def test_email_provider_none_mode_stays_plaintext(monkeypatch):
    created = await _send_email(monkeypatch, smtp_security="none")

    assert len(created["smtp"]) == 1
    assert created["smtp_ssl"] == []
    assert created["smtp"][0].starttls_context is _STARTTLS_NOT_CALLED
    assert created["smtp"][0].logins == [("user", "secret")]
    assert len(created["smtp"][0].sent_messages) == 1


@pytest.mark.asyncio
async def test_email_provider_invalid_security_mode_falls_back_to_starttls(
    monkeypatch,
):
    created = await _send_email(monkeypatch, smtp_security="bogus-mode")

    assert created["smtp_ssl"] == []
    assert len(created["smtp"]) == 1
    assert created["smtp"][0].starttls_context is not _STARTTLS_NOT_CALLED


@pytest.mark.asyncio
async def test_email_provider_renders_markdown_type_and_bold_title(monkeypatch):
    created = await _send_email(monkeypatch, smtp_security="ssl")

    message = created["smtp_ssl"][0].sent_messages[0]
    # 默认发件昵称 Sakura-AI
    assert str(message["From"]) == "Sakura-AI <noreply@example.invalid>"
    assert str(message["Subject"]) == "【版本发布】标题"
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert "<strong>标题</strong>" in html_part
    assert ">版本发布</span>" in html_part
    # Markdown 正文已渲染（标题/加粗/列表），而不是转义后的纯文本
    assert "<h1>发布说明</h1>" in html_part
    assert "<strong>加粗要点</strong>" in html_part
    assert "<li>列表项</li>" in html_part
    # 页脚的 Sakura-AI 可点击跳转（未配置域名时回退开源仓库）
    assert (
        html_part.count('href="https://github.com/Sakura520222/Sakura-AI"') == 2
    )
    assert "此邮件由 <a href=" in html_part


@pytest.mark.asyncio
async def test_email_provider_footer_links_to_deployment_domain(monkeypatch):
    created = await _send_email(
        monkeypatch, sanitized_app_domain="deploy.example.invalid"
    )

    message = created["smtp"][0].sent_messages[0]
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert html_part.count('href="https://deploy.example.invalid"') == 2
    assert "ai.firefly520.top" not in html_part


@pytest.mark.asyncio
async def test_email_provider_footer_falls_back_when_domain_is_default(monkeypatch):
    created = await _send_email(monkeypatch, sanitized_app_domain="localhost")

    message = created["smtp"][0].sent_messages[0]
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert (
        html_part.count('href="https://github.com/Sakura520222/Sakura-AI"') == 2
    )
    plain_part = message.get_body(preferencelist=("plain",)).get_content()
    assert "【版本发布】标题" in plain_part
    assert "# 发布说明" in plain_part


@pytest.mark.asyncio
async def test_email_provider_honors_custom_from_name(monkeypatch):
    created = await _send_email(monkeypatch, smtp_from_name="樱组通知")

    message = created["smtp"][0].sent_messages[0]
    assert str(message["From"]) == "樱组通知 <noreply@example.invalid>"


class _FakeTelegramBot:
    def __init__(self):
        self.sent = []
        self.base_url = "https://api.telegram.org/bot123:abc"

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class _FakeHttpxResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, payload=None, exc=None):
        self._payload = payload if payload is not None else {"ok": False}
        self._exc = exc
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json=None):
        self.requests.append((url, json))
        if self._exc is not None:
            raise self._exc
        return _FakeHttpxResponse(self._payload)


async def _send_telegram(monkeypatch, *, http_payload=None, http_exc=None):
    bot = _FakeTelegramBot()
    client = _FakeHttpxClient(payload=http_payload, exc=http_exc)
    monkeypatch.setattr(telegram_bot_module, "get_telegram_bot", lambda: bot)
    monkeypatch.setattr(
        notification_service.httpx, "AsyncClient", lambda timeout=None: client
    )
    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(telegram_enabled=True, telegram_bot_token="123:abc"),
    )
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="telegram",
        address="4242",
        enabled=True,
    )
    await notification_service.TelegramNotificationProvider().send(
        endpoint=endpoint,
        title="维护 <通知>",
        content="# 维护计划\n\n**数据库升级**",
        content_html="<h1>维护计划</h1><p><strong>数据库升级</strong></p>",
        announcement_type="maintenance",
    )
    return bot, client


@pytest.mark.asyncio
async def test_telegram_provider_prefers_rich_markdown(monkeypatch):
    bot, client = await _send_telegram(monkeypatch, http_payload={"ok": True})

    assert bot.sent == []
    assert len(client.requests) == 1
    url, payload = client.requests[0]
    assert url == "https://api.telegram.org/bot123:abc/sendRichMessage"
    assert payload["chat_id"] == 4242
    markdown = payload["rich_message"]["markdown"]
    assert markdown.startswith("[维护通知]\n\n<b>维护 &lt;通知&gt;</b>\n\n")
    assert "📢" not in markdown
    assert "# 维护计划" in markdown
    assert "**数据库升级**" in markdown


@pytest.mark.asyncio
async def test_telegram_provider_falls_back_to_legacy_html(monkeypatch):
    bot, client = await _send_telegram(
        monkeypatch, http_payload={"ok": False, "error_code": 404}
    )

    assert len(client.requests) == 1
    assert len(bot.sent) == 1
    kwargs = bot.sent[0]
    assert kwargs["parse_mode"] == "HTML"
    text = kwargs["text"]
    assert text.startswith("[维护通知]\n\n<b>维护 &lt;通知&gt;</b>\n\n")
    assert "📢" not in text
    assert "<b>维护计划</b>" in text
    assert "<b>数据库升级</b>" in text


@pytest.mark.asyncio
async def test_telegram_provider_rich_rate_limit_propagates(monkeypatch):
    payload = {
        "ok": False,
        "error_code": 429,
        "parameters": {"retry_after": 9},
    }
    with pytest.raises(notification_service.NotificationProviderRetryAfter) as exc_info:
        await _send_telegram(monkeypatch, http_payload=payload)

    assert exc_info.value.retry_after == 9


def test_markdown_to_telegram_html_subset_and_escaping():
    source = (
        "# 标题\n"
        "段落 **加粗** *斜体* `code` <script>alert(1)</script>\n"
        "- 项目一\n"
        "- 项目二\n"
        "1. 第一\n"
        "2. 第二\n"
        "[链接](https://example.com/a?b=1) 与 [拒绝](javascript:alert(1))"
    )
    rendered = announcement_service.markdown_to_telegram_html(source)

    assert "<b>标题</b>" in rendered
    assert "<b>加粗</b>" in rendered
    assert "<i>斜体</i>" in rendered
    assert "<code>code</code>" in rendered
    assert "&lt;script&gt;" in rendered
    assert "• 项目一" in rendered
    assert "• 项目二" in rendered
    assert "1. 第一" in rendered
    assert "2. 第二" in rendered
    assert '<a href="https://example.com/a?b=1">链接</a>' in rendered
    assert "<a href" not in rendered.split("与 ", 1)[1]


def test_announcement_type_label_defaults_for_unknown_values():
    assert announcement_service.announcement_type_label("release") == "版本发布"
    assert announcement_service.announcement_type_label("IMPORTANT") == "重要公告"
    assert announcement_service.announcement_type_label(None) == "公告"
    assert announcement_service.announcement_type_label("bogus") == "公告"
