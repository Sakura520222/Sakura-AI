"""Historical import suppression and bounded, scope-correct SQL fanout."""
import asyncio
import math
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.orm import Session

from backend.models.announcement_models import Announcement, NotificationDelivery
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
    UserNotification,
)
from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential
from backend.services import user_backup_service
from backend.services.notification_events import (
    candidates,
    emit_event,
    routing_rules,
    rule_matches,
)
from backend.services.webauthn_service import credential_id_hash
from tests.test_notification_events_646 import AsyncDB, Provider, make_event, service
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


async def changed_backup(db):
    document = await user_backup_service.export_user_backup(db)
    user = next(row for row in document["users"] if row["identity"]["github_username"] == "alice")
    user["profile"].update(role="admin")
    user["passkeys"] = [{"credential_id": "restored-credential", "public_key": "public-key",
        "sign_count": 0, "device_name": "Restored key", "transports": "internal", "backed_up": False,
        "created_at": None, "last_used_at": None}]
    return user_backup_service.parse_user_backup(user_backup_service.serialize_user_backup(document))


@pytest.mark.asyncio
async def test_real_backup_restore_suppresses_security_and_passkey_events_only_during_import(event_db):
    engine, _ = event_db
    async with AsyncDB(engine, outbox=True) as db:
        endpoint = await db.get(NotificationEndpoint, 1)
        endpoint.config_version = 3
        endpoint.verification_token_hash = "c" * 64
        await db.commit()
        document = await changed_backup(db)
        # A failed earlier flush can leave its before_flush collection in info.
        db.info["notification_changes"] = [("security", await db.get(TelegramUser, 1), "Old change", "old-run")]
        result = await user_backup_service.restore_user_backup(db, document)
        assert result.passkeys_created == 1 and db.info["notification_outbox"] is True
        endpoint = await db.get(NotificationEndpoint, 1)
        assert endpoint.config_version == 4 and endpoint.verification_token_hash is None
        # The application's own export stays importable after toggles/rotation
        # and after a restore; generations are configuration fences, not schema versions.
        exported = await user_backup_service.export_user_backup(db)
        parsed = user_backup_service.parse_user_backup(user_backup_service.serialize_user_backup(exported))
        await user_backup_service.restore_user_backup(db, parsed)
        assert (await db.get(NotificationEndpoint, 1)).config_version == 5
        assert (await db.get(TelegramUser, 1)).role == "admin"
        with Session(engine) as check:
            assert check.query(NotificationEvent).count() == 0
            assert check.query(NotificationDelivery).count() == check.query(UserNotification).count() == 0
            credential = check.query(UserWebAuthnCredential).one()
            assert credential.credential_id_hash == credential_id_hash("restored-credential")
        user = await db.get(TelegramUser, 1)
        user.role = "user"
        await db.commit()
    with Session(engine) as check:
        assert check.query(NotificationEvent).one().event_type == "security.changed"


@pytest.mark.asyncio
async def test_failed_backup_commit_restores_outbox_and_rolls_back_historical_state(event_db, monkeypatch):
    engine, _ = event_db
    async with AsyncDB(engine, outbox=True) as db:
        document = await changed_backup(db)
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("commit unavailable")))
        with pytest.raises(user_backup_service.UserBackupError):
            await user_backup_service.restore_user_backup(db, document)
        assert db.info["notification_outbox"] is True
    with Session(engine) as check:
        assert check.get(TelegramUser, 1).role == "user"
        assert check.query(UserWebAuthnCredential).count() == check.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [True, False, None])
async def test_restore_reinstates_exact_session_flag_on_validation_failure(event_db, previous):
    async with AsyncDB(event_db[0]) as db:
        if previous is None:
            db.info.pop("notification_outbox")
        else:
            db.info["notification_outbox"] = previous
        with pytest.raises(user_backup_service.UserBackupError):
            await user_backup_service.restore_user_backup(db, {})
        assert db.info == ({} if previous is None else {"notification_outbox": previous})


@pytest.mark.asyncio
async def test_cancelled_restore_rolls_back_before_reenabling_outbox(event_db, monkeypatch):
    async def cancel(db, _document):
        user = await db.get(TelegramUser, 1)
        user.role = "admin"
        await db.flush()
        raise asyncio.CancelledError

    monkeypatch.setattr(user_backup_service, "_restore_user_backup", cancel)
    async with AsyncDB(event_db[0], outbox=True) as db:
        with pytest.raises(asyncio.CancelledError):
            await user_backup_service.restore_user_backup(db, {})
        assert db.info["notification_outbox"] is True
    with Session(event_db[0]) as check:
        assert check.get(TelegramUser, 1).role == "user" and check.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
async def test_grouped_sql_rules_preserve_disabled_scope_and_severity_defaults(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add_all([
            NotificationSubscription(user_id=1, event_type="review.*", repo_name="ALICE/REPO", endpoint_id=1),
            NotificationSubscription(user_id=1, event_type="review.*", repo_name="alice/other", endpoint_id=2),
            NotificationSubscription(user_id=2, event_type="review.*", endpoint_id=None, enabled=False),
            NotificationSubscription(user_id=2, event_type="review.completed", min_severity="critical", endpoint_id=None),
            NotificationSubscription(user_id=2, event_type="review.*", resource_type="issue", endpoint_id=None),
            NotificationSubscription(user_id=2, event_type="review.*", resource_id="other", endpoint_id=None),
            NotificationSubscription(user_id=2, event_type="security.*", endpoint_id=None),
        ])
        db.commit()
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        grouped = await routing_rules(db, item)
        scoped = await routing_rules(db, item, user_ids=[2])
        ids, candidate_rules = await candidates(db, item)
        with Session(engine) as check:
            expected = {}
            for row in check.query(NotificationSubscription):
                if row.event_type in {"*", item.event_type, "review.*"}:
                    targets = expected.setdefault(row.user_id, set())
                    if rule_matches(row, item):
                        targets.add(row.endpoint_id)
        assert grouped == candidate_rules == expected
        assert scoped == {2: set()} and ids == [1]


@pytest.mark.asyncio
async def test_announcement_fanout_batches_queries_and_shares_transaction_resource_snapshot(event_db):
    engine, settings = event_db
    total = 202
    with Session(engine) as db:
        db.add_all(TelegramUser(id=index, github_username=f"user{index}", is_active=True)
            for index in range(3, total + 1))
        db.flush()
        db.add(Announcement(id=646, title="Announcement", content="Body", status="published", publication_version=1))
        for user_id in range(1, total + 1):
            endpoint_id = 1 if user_id == 1 else 1000 + user_id
            if user_id != 1:
                db.add(NotificationEndpoint(id=endpoint_id, user_id=user_id, provider="email",
                    address=f"user{user_id}@example.org", verified=True, enabled=True))
            db.add_all([NotificationSubscription(user_id=user_id, event_type="announcement.*", endpoint_id=None),
                NotificationSubscription(user_id=user_id, event_type="announcement.*", endpoint_id=endpoint_id),
                NotificationSubscription(user_id=user_id, event_type="scan.*", endpoint_id=None)])
        db.commit()
    async with AsyncDB(engine) as db:
        item = await emit_event(db, event_type="announcement.published", resource_type="announcement",
            resource_id=646, run_key="1", title="Announcement", content="Body", audience="all")
        await db.commit()
        queries = []

        def capture(_conn, _cursor, sql, _parameters, _context, _many):
            if sql.lstrip().startswith("SELECT"):
                queries.append(sql)

        sql_event.listen(engine, "before_cursor_execute", capture)
        try:
            assert await service(Provider()).materialize_event(db, item.id)
        finally:
            sql_event.remove(engine, "before_cursor_execute", capture)
    rule_queries = [sql for sql in queries if "FROM notification_subscriptions" in sql]
    endpoint_queries = [sql for sql in queries if "FROM notification_endpoints" in sql]
    assert len(rule_queries) <= 1 + math.ceil(total / settings.notification_batch_size)
    assert all("event_type IN" in sql for sql in rule_queries)
    assert len(endpoint_queries) == math.ceil(total / settings.notification_batch_size)
    assert sum("FROM announcements" in sql for sql in queries) == 1
    with Session(engine) as check:
        assert check.query(UserNotification).count() == total
        assert check.query(NotificationDelivery).count() == total * 2
