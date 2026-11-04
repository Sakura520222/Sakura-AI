"""Real MySQL/PostgreSQL transaction tests; skipped without an isolated test DB."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models import Base
from backend.models import database as db_module
from backend.models.admin_action_log import AdminActionLog
from backend.models.agent_team_models import AgentTeamTask
from backend.models.announcement_models import (
    Announcement,
    AnnouncementDeliveryHistory,
    AnnouncementPublicationHistory,
    AnnouncementRead,
    NotificationDelivery,
)
from backend.models.billing_models import (
    BillingCreditDebt,
    BillingCreditHold,
    BillingCreditLot,
    BillingDebtFunding,
    BillingTransaction,
    BillingWallet,
)
from backend.models.database import AppConfig, IssueAnalysis, UserConfig, WebUIConfig
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    LegacyEntitlementEvent,
    RateLimitAdmission,
)
from backend.models.notification_models import (
    NotificationAudit,
    NotificationEvent,
    NotificationLockoutRound,
    NotificationSubscription,
)
from backend.models.payment_models import Order, PaymentLog, Plan, RedeemCode
from backend.models.scan_models import RepoScan
from backend.models.telegram_models import (
    QuotaUsageLog,
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)
from backend.services import notification_events, telegram_service
from backend.services.announcement_service import paginate_announcements, unread_count
from backend.services.issue_service import IssueService
from backend.services.notification_business import install_notification_outbox
from backend.services.notification_events import emit_event
from backend.services.notification_migration import upgrade_notification_schema
from backend.services.notification_preferences import (
    cancel_endpoint_deliveries,
    owned_endpoint,
    save_subscription,
    verify_endpoint,
)
from backend.services.notification_service import (
    NotificationProviderRegistry,
    NotificationService,
)
from backend.services.telegram_service import TelegramService


@pytest_asyncio.fixture
async def live_db(monkeypatch):
    url = os.environ.get("NOTIFICATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("No isolated MySQL/PostgreSQL test database configured")
    parsed = make_url(url)
    if parsed.database != "sakura_notification_test" or parsed.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("Notification integration tests require an isolated localhost test database")
    engine = create_async_engine(url)
    table_names = {
        "telegram_users", "notification_endpoints", "announcements", "notification_events",
        "notification_deliveries", "user_notifications", "notification_subscriptions",
        "notification_provider_budgets", "user_repo_subscriptions",
        "issue_analyses",
        "agent_team_tasks",
        "repo_scans",
    }
    table_names.update(model.__tablename__ for model in (
        UserConfig, WebUIConfig, UserIdentity, UserRecoveryCode, UserWebAuthnCredential, NotificationAudit,
        NotificationLockoutRound, AnnouncementPublicationHistory, AnnouncementDeliveryHistory, AnnouncementRead,
        Order, PaymentLog, Plan, RedeemCode,
        BillingWallet, LegacyEntitlement, LegacyEntitlementEvent, RateLimitAdmission,
        QuotaUsageLog, AppConfig,
        BillingTransaction, BillingCreditLot, BillingCreditHold, BillingCreditDebt,
        BillingDebtFunding,
        AdminActionLog,
    ))
    tables = [table for table in Base.metadata.sorted_tables if table.name in table_names]
    async with engine.begin() as conn:
        await conn.run_sync(lambda db: Base.metadata.drop_all(db, tables=tables))
        await conn.run_sync(lambda db: Base.metadata.create_all(db, tables=tables))
    install_notification_outbox()
    factory = async_sessionmaker(engine, expire_on_commit=False, info={"notification_outbox": True})
    monkeypatch.setattr(db_module, "async_session", factory)
    monkeypatch.setattr(get_settings(), "notification_rate_limit_seconds", 0)
    async with factory() as db:
        db.add(AppConfig(key_name="billing_enabled", key_value="false"))
        db.add(TelegramUser(id=1, github_username="alice", is_active=True))
        await db.flush()
        db.add(NotificationEndpoint(id=1, user_id=1, provider="email", address="a@example.org", verified=True, enabled=True))
        await db.flush()
        db.add(NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=1))
        await db.commit()
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda db: Base.metadata.drop_all(db, tables=tables))
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column_collation", "connection_collation"),
    [("utf8mb4_general_ci", "utf8mb4_0900_ai_ci"), ("utf8mb4_0900_ai_ci", "utf8mb4_general_ci")],
)
async def test_live_unread_announcement_scopes_with_mixed_collations(
    live_db, column_collation, connection_collation,
):
    async with live_db(info={"notification_outbox": False}) as db:
        if db.bind.dialect.name in {"mysql", "mariadb"}:
            # Reproduce a legacy database whose column and connection defaults differ.
            await db.execute(text(
                "ALTER TABLE notification_subscriptions MODIFY COLUMN "
                f"resource_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE {column_collation} NULL"
            ))
            await db.execute(text(f"SET NAMES utf8mb4 COLLATE {connection_collation}"))
        for announcement_id in (1, 12, 13):
            db.add(Announcement(
                id=announcement_id, title=f"Announcement {announcement_id}",
                content="Body", status="published", published_at=now_utc(),
            ))
        await db.flush()
        db.add(AnnouncementRead(user_id=1, announcement_id=13, publication_version=1))
        await db.flush()

        # Even the default (no announcement rules) must execute the whole predicate.
        assert await unread_count(db, 1) == 2
        rule = NotificationSubscription(
            user_id=1, event_type="announcement.*", enabled=True,
            resource_type="announcement", resource_id=None,
        )
        db.add(rule)
        for resource_id, expected in (
            (None, [12, 1]), ("1", [1]), ("12", [12]),
            ("01", []), ("1junk", []), ("1.0", []), ("13", []),
        ):
            rule.resource_id = resource_id
            await db.flush()
            page = await paginate_announcements(
                db, user_id=1, per_page=1, unread_only=True,
            )
            assert page.total == len(expected)
            assert [row.id for row, _read in page.items] == expected[:1]
            assert await unread_count(db, 1) == len(expected)
            if len(expected) > 1:
                second = await paginate_announcements(
                    db, user_id=1, page=2, per_page=1, unread_only=True,
                )
                assert [row.id for row, _read in second.items] == expected[1:]
        rule.resource_id = None
        rule.enabled = False
        await db.flush()
        assert await unread_count(db, 1) == 0
        assert (await paginate_announcements(db, user_id=1, unread_only=True)).total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["commit", "rollback", "event_failure"])
async def test_live_low_balance_event_and_ledger_share_the_transaction(live_db, monkeypatch, outcome):
    from decimal import Decimal

    from backend.services.billing_service import BillingService

    async with live_db() as db:
        billing = BillingService(db)
        await billing.set_low_balance_threshold(1, "5")
        await billing.grant(1, "10", "live-balance:fund")
        await db.commit()
        if outcome == "event_failure":
            monkeypatch.setattr(notification_events, "emit_event", AsyncMock(side_effect=RuntimeError("TEST outbox unavailable")))
            with pytest.raises(RuntimeError, match="outbox unavailable"):
                await billing.adjust(1, Decimal(-7), idempotency_key="live-balance:debit", actor_id=1, reason="TEST")
            await db.commit()
        else:
            await billing.adjust(1, Decimal(-7), idempotency_key="live-balance:debit", actor_id=1, reason="TEST")
            if outcome == "commit":
                await db.commit()
            else:
                await db.rollback()
    async with live_db() as db:
        wallet = await db.get(BillingWallet, 1)
        events = (await db.execute(select(NotificationEvent))).scalars().all()
        transactions = (await db.execute(select(BillingTransaction))).scalars().all()
        assert wallet.balance_units == (3_000_000 if outcome == "commit" else 10_000_000)
        assert wallet.low_balance_notified is (outcome == "commit")
        assert len(events) == (1 if outcome == "commit" else 0)
        assert len(transactions) == (2 if outcome == "commit" else 1)
        if events:
            assert events[0].event_type == "billing.low_balance" and events[0].sensitive


@pytest.mark.asyncio
async def test_live_concurrent_debits_emit_one_low_balance_episode(live_db):
    from decimal import Decimal

    from backend.services.billing_service import BillingService

    async with live_db() as db:
        billing = BillingService(db)
        await billing.set_low_balance_threshold(1, "5")
        await billing.grant(1, "10", "concurrent-balance:fund")
        await db.commit()

    async def debit(number):
        async with live_db() as db:
            await BillingService(db).adjust(1, Decimal(-3), idempotency_key=f"concurrent-balance:{number}", actor_id=1, reason="TEST")
            await db.commit()

    await asyncio.gather(*(debit(number) for number in range(3)))
    async with live_db() as db:
        assert (await db.get(BillingWallet, 1)).balance_units == 1_000_000
        assert len((await db.execute(select(NotificationEvent))).scalars().all()) == 1
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 4


@pytest.mark.asyncio
async def test_live_gateway_expiration_emits_once_and_rolls_back_with_its_event(live_db, monkeypatch):
    from backend.services.payment_service import PaymentService

    async with live_db(info={"notification_outbox": False}) as db:
        db.add(Plan(id=1, name="Plan", plan_type="one_time", price_cents=100))
        await db.flush()
        db.add_all([Order(id=number, order_no=f"EXPIRE_{number}", user_id=1, plan_id=1,
            amount_cents=100, currency="USD", status="pending", payment_provider="stripe") for number in (646, 647)])
        await db.commit()

    async def expire():
        async with live_db() as db:
            await PaymentService(db).cancel_and_commit_if_needed("EXPIRE_646")

    await asyncio.gather(expire(), expire())
    async with live_db() as db:
        event = (await db.execute(select(NotificationEvent))).scalar_one()
        assert event.event_type == "payment.failed" and event.recipients_json == "[1]"
        assert (await db.get(Order, 646)).status == "cancelled"

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("Injected outbox failure")

    monkeypatch.setattr(notification_events, "emit_event", unavailable)
    async with live_db() as db:
        with pytest.raises(RuntimeError, match="outbox failure"):
            await PaymentService(db).cancel_expired_order("EXPIRE_647")
        await db.commit()  # Later caller cleanup cannot commit a state-only transition.
        assert (await db.get(Order, 647)).status == "pending"
        assert (await db.execute(select(PaymentLog).where(PaymentLog.order_id == 647))).scalars().all() == []


@pytest.mark.asyncio
async def test_live_webhook_challenge_cannot_verify_a_replaced_credential(live_db, monkeypatch):
    from backend.services.notification_service import notification_service
    from backend.services.secret_crypto_service import encrypt_secret

    async with live_db() as db:
        db.add(NotificationEndpoint(id=2, user_id=1, provider="webhook", address="https://example.org/hook",
            secret_encrypted=encrypt_secret("a" * 32), enabled=True, verified=False))
        await db.commit()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def challenge(_endpoint):
        entered.set()
        await resume.wait()

    monkeypatch.setattr(notification_service.registry.get("webhook"), "verify_endpoint", challenge)

    async def verify():
        async with live_db() as db:
            return await verify_endpoint(db, 1, 2)

    task = asyncio.create_task(verify())
    await asyncio.wait_for(entered.wait(), 10)
    async with live_db() as db:
        endpoint = await owned_endpoint(db, 1, 2, for_update=True)
        endpoint.secret_encrypted, endpoint.verified = encrypt_secret("b" * 32), False
        endpoint.config_version += 1
        await db.commit()
    resume.set()
    with pytest.raises(ValueError, match="configuration changed"):
        await asyncio.wait_for(task, 10)
    async with live_db() as db:
        endpoint = await db.get(NotificationEndpoint, 2)
        assert endpoint.verified is False and endpoint.config_version == 2


@pytest.mark.asyncio
async def test_live_database_concurrent_producers_share_event(live_db):
    async def produce():
        async with live_db() as db:
            item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
                run_key="1", title="Completed", content="Ready", repo_name="ALICE/repo", recipient_ids=[])
            await db.commit()
            return item.id
    ids = await asyncio.gather(*(produce() for _ in range(4)))
    assert len(set(ids)) == 1
    async with live_db() as db:
        from backend.services.notification_events import candidates

        item = await db.get(NotificationEvent, ids[0])
        assert (await candidates(db, item))[0] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "issue", "agent"])
@pytest.mark.parametrize("failure", ["insert", "commit"])
async def test_live_quota_denial_survives_alert_failure_without_expired_attribute_reads(
    live_db, monkeypatch, kind, failure
):
    async with live_db() as db:
        user = await db.get(TelegramUser, 1)
        for prefix in ("", "issue_", "agent_"):
            for period in ("daily", "weekly", "monthly"):
                setattr(
                    user, f"{prefix}{period}_quota", 10 if period == "daily" else 20
                )
                setattr(user, f"{prefix}{period}_used", 10 if period == "daily" else 0)
                from backend.core.time_service import now_utc

                setattr(user, f"last_reset_{prefix}{period}", now_utc())
        await db.commit()
    monkeypatch.setattr(
        telegram_service, "is_payment_enabled", AsyncMock(return_value=False)
    )
    original_emit = notification_events.emit_event

    async def fail_after_insert(*args, **kwargs):
        await original_emit(*args, **kwargs)
        raise RuntimeError("alert persistence unavailable")

    if failure == "insert":
        monkeypatch.setattr(notification_events, "emit_event", fail_after_insert)
    async with live_db() as db:
        user = await db.get(TelegramUser, 1)
        if failure == "commit":
            monkeypatch.setattr(
                db,
                "commit",
                AsyncMock(side_effect=RuntimeError("alert commit unavailable")),
            )
        service = TelegramService(db)
        if kind == "review":
            result = await service.check_and_consume_quota("alice", "Acme/repo", 646)
        elif kind == "issue":
            result = await service.check_and_consume_issue_quota(
                "alice", "Acme/repo", 646
            )
        else:
            result = await service.check_and_consume_agent_quota(
                "alice", "Acme/repo", 646
            )
        assert result[0] is False
        assert "每日次数限流已达到 (10/10)" in result[1]
        assert inspect(user).expired
    async with live_db() as db:
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []
        user = await db.get(TelegramUser, 1)
        assert (user.daily_used, user.issue_daily_used, user.agent_daily_used) == (
            10,
            10,
            10,
        )


@pytest.mark.asyncio
async def test_live_issue_completion_and_close_race_emit_only_committed_transition(live_db):
    async with live_db() as db:
        db.add(IssueAnalysis(id=646, issue_number=646, repo_name="repo", repo_owner="alice",
            author="alice", title="Issue", status="analyzing", analysis_version=1))
        await db.commit()

    async def complete():
        async with live_db() as db:
            await IssueService().save_analysis_result({"summary": "Done", "priority": "high"},
                {"repo_owner": "alice", "repo_name": "repo", "issue_number": 646}, db, analysis_id=646)

    async def close():
        async with live_db() as db:
            await IssueService().mark_issue_closed("alice", "repo", 646, db)

    await asyncio.gather(complete(), close(), close())
    async with live_db() as db:
        record = await db.get(IssueAnalysis, 646)
        events = (await db.execute(select(NotificationEvent).where(NotificationEvent.resource_type == "issue"))).scalars().all()
        assert record.issue_state == "closed"
        assert record.status in {"completed", "cancelled"}
        # Seeding an active task through an application session legitimately
        # emits started. The concurrent writers must add exactly one terminal.
        assert len(events) == 2
        by_type = {event.event_type: event for event in events}
        assert set(by_type) == {"issue.started", f"issue.{record.status}"}
        if record.status == "completed":
            assert by_type["issue.completed"].severity == "warning"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "scan"])
async def test_live_agent_duplicate_admissions_and_early_retry_failures(live_db, kind):
    model = AgentTeamTask if kind == "agent" else RepoScan
    values = {"id": 646, "repo_owner": "Acme", "repo_name": "repo", "status": "failed"}
    values.update({"repo_full_name": "Acme/repo", "source_type": "manual_issue", "title": "Task", "started_by": "alice"}
        if kind == "agent" else {"trigger_type": "manual"})
    async with live_db(info={"notification_outbox": False}) as db:
        db.add(model(**values))
        await db.commit()

    async def queue():
        async with live_db() as db:
            task = await db.get(model, 646)
            task.status = "queued" if kind == "agent" else "pending"
            await db.commit()

    for expected_round in (1, 2):
        await asyncio.gather(queue(), queue())
        async with live_db() as db:
            task = await db.get(model, 646)
            assert task.execution_round == expected_round
            task.status = "failed"  # before skills/cloning/iteration begins
            await db.commit()
    async with live_db() as db:
        events = (await db.execute(select(NotificationEvent).where(NotificationEvent.resource_type == kind))).scalars().all()
        assert {event.run_key for event in events} == ({"1:0:0", "2:0:0"} if kind == "agent" else {"1:646", "2:646"})
        assert len(events) == 2


@pytest.mark.asyncio
async def test_live_agent_round_migration_preserves_existing_tasks(live_db):
    async with live_db(info={"notification_outbox": False}) as db:
        db.add(AgentTeamTask(id=646, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title="Task", status="completed", started_by="alice"))
        db.add(RepoScan(id=646, repo_name="repo", repo_owner="Acme", trigger_type="manual", status="failed"))
        await db.commit()
        for name in ("agent_team_tasks", "repo_scans"):
            await db.execute(text(f"ALTER TABLE {name} DROP COLUMN execution_round"))
        conn = await db.connection()
        assert await conn.run_sync(upgrade_notification_schema) is True
        assert await conn.run_sync(upgrade_notification_schema) is False
        await db.commit()

    async with live_db() as db:
        task = await db.get(AgentTeamTask, 646)
        assert (task.status, task.execution_round) == ("completed", 0)
        scan = await db.get(RepoScan, 646)
        assert (scan.status, scan.execution_round) == ("failed", 0)
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_live_database_competing_claims_send_once(live_db):
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    class Provider:
        channel = "email"
        async def send(self, **_):
            entered.set()
            await release.wait()
            sent.append(1)
    dispatcher = NotificationService(NotificationProviderRegistry({"email": Provider()}))
    async with live_db() as db:
        item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
            run_key="1", title="Completed", content="Ready", repo_name="alice/repo", recipient_ids=[1])
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        row_id = (await db.execute(select(NotificationDelivery.id))).scalar_one()
    async def deliver():
        async with live_db() as db:
            return await dispatcher.deliver_event_row(db, row_id)
    first = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await deliver() is False
    finally:
        release.set()
        await first
    assert sent == [1]


@pytest.mark.asyncio
async def test_live_legacy_unique_key_and_nullability_upgrade(live_db):
    async with live_db() as db:
        conn = await db.connection()
        dialect = conn.dialect.name
        keyword = "INDEX" if dialect in {"mysql", "mariadb"} else "CONSTRAINT"
        for name in ("uq_notification_event_target", "uq_notification_delivery_target"):
            await db.execute(text(f"ALTER TABLE notification_deliveries DROP {keyword} {name}"))
        statement = ("ALTER TABLE notification_deliveries MODIFY COLUMN announcement_id INTEGER NOT NULL"
                     if dialect in {"mysql", "mariadb"} else
                     "ALTER TABLE notification_deliveries ALTER COLUMN announcement_id SET NOT NULL")
        await db.execute(text(statement))
        await db.execute(text("ALTER TABLE notification_deliveries ADD CONSTRAINT uq_notification_delivery UNIQUE (announcement_id, user_id, channel)"))
        assert await conn.run_sync(upgrade_notification_schema) is True
        assert await conn.run_sync(upgrade_notification_schema) is False
        await db.commit()

        # A later fresh schema must still have both uniqueness contracts after
        # this process has executed ALTER TABLE against the legacy schema.
        conn = await db.connection()
        await conn.run_sync(AnnouncementDeliveryHistory.__table__.drop)
        await conn.run_sync(NotificationDelivery.__table__.drop)
        await conn.run_sync(NotificationDelivery.__table__.create)
        await conn.run_sync(AnnouncementDeliveryHistory.__table__.create)
        constraints = await conn.run_sync(lambda connection: inspect(connection).get_unique_constraints("notification_deliveries"))
        assert {"uq_notification_delivery_target", "uq_notification_event_target"} <= {row["name"] for row in constraints}
        await db.commit()


@pytest.mark.asyncio
async def test_live_backup_restore_suppresses_historical_security_events(live_db):
    from backend.services.user_backup_service import (
        export_user_backup,
        parse_user_backup,
        restore_user_backup,
        serialize_user_backup,
    )
    from tests.test_notification_import_routing_646 import changed_backup

    async with live_db() as db:
        endpoint = await db.get(NotificationEndpoint, 1)
        endpoint.config_version = 3
        endpoint.verification_token_hash = "c" * 64
        await db.commit()
        result = await restore_user_backup(db, await changed_backup(db))
        assert result.passkeys_created == 1 and db.info["notification_outbox"] is True
        endpoint = await db.get(NotificationEndpoint, 1)
        assert endpoint.config_version == 4 and endpoint.verification_token_hash is None
        exported = await export_user_backup(db)
        await restore_user_backup(db, parse_user_backup(serialize_user_backup(exported)))
        assert (await db.get(NotificationEndpoint, 1)).config_version == 5
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []
        user = await db.get(TelegramUser, 1)
        assert user.role == "admin"
        user.role = "user"
        await db.commit()
    async with live_db() as db:
        assert (await db.execute(select(NotificationEvent))).scalar_one().event_type == "security.changed"


@pytest.mark.asyncio
async def test_live_concurrent_subscription_upserts_share_one_rule(live_db):
    async def save():
        async with live_db() as db:
            row = await save_subscription(db, 1, {"event_type": "review.*", "repo_name": "ALICE/Repo",
                "resource_type": None, "resource_id": None, "endpoint_id": 1, "enabled": True, "min_severity": "info"})
            return row.id

    assert len(set(await asyncio.gather(*(save() for _ in range(6))))) == 1
    async with live_db() as db:
        row = (await db.execute(select(NotificationSubscription).where(
            NotificationSubscription.repo_name == "ALICE/Repo"
        ))).scalar_one()
        assert row.enabled is True


@pytest.mark.asyncio
async def test_live_endpoint_disable_serializes_with_fanout_and_fences_old_rows(live_db, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Provider:
        channel = "email"

        async def send(self, **_):
            sent.append(1)

    dispatcher = NotificationService(NotificationProviderRegistry({"email": Provider()}))
    original = dispatcher._materialize_recipient

    async def pause(*args):
        entered.set()  # Fanout has locked its exact endpoints before this call.
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(dispatcher, "_materialize_recipient", pause)
    async with live_db() as db:
        item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
            run_key="disable-race", title="Completed", content="Ready", repo_name="alice/repo", recipient_ids=[1])
        await db.commit()
        event_id = item.id

    async def materialize():
        async with live_db() as db:
            return await dispatcher.materialize_event(db, event_id)

    async def disable():
        async with live_db() as db:
            endpoint = await owned_endpoint(db, 1, 1, for_update=True)
            endpoint.enabled = False
            await cancel_endpoint_deliveries(db, 1)
            await db.commit()

    fanout = asyncio.create_task(materialize())
    disabler = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        disabler = asyncio.create_task(disable())
        await asyncio.sleep(0.1)
        assert not disabler.done()
    finally:
        release.set()
        await fanout
        if disabler is not None:
            await disabler
    async with live_db() as db:
        row = (await db.execute(select(NotificationDelivery))).scalar_one()
        assert row.status == "cancelled" and row.claim_token is row.next_retry_at is None
        row_id = row.id
        endpoint = await db.get(NotificationEndpoint, 1)
        endpoint.enabled = True
        await db.commit()
        assert await dispatcher.deliver_event_row(db, row_id) is False
    assert sent == []


@pytest.mark.asyncio
async def test_live_subscription_write_serializes_with_fanout_without_lock_inversion(live_db, monkeypatch):
    from backend.services import notification_dispatch

    entered, release = asyncio.Event(), asyncio.Event()
    dispatcher = NotificationService(NotificationProviderRegistry({}))
    original = dispatcher._materialize_recipient
    original_candidates = notification_dispatch.candidates

    async def change_before_user_lock(*args, **kwargs):
        result = await original_candidates(*args, **kwargs)
        async with live_db() as db:
            await save_subscription(db, 1, {"event_type": "review.*", "endpoint_id": None,
                "enabled": True, "min_severity": "info"})
        return result

    monkeypatch.setattr(notification_dispatch, "candidates", change_before_user_lock)

    async def pause(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(dispatcher, "_materialize_recipient", pause)
    async with live_db() as db:
        item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
            run_key="subscribe-race", title="Completed", content="Ready", repo_name="alice/repo", recipient_ids=[1])
        await db.commit()
        event_id = item.id

    async def materialize():
        async with live_db() as db:
            return await dispatcher.materialize_event(db, event_id)

    async def subscribe():
        async with live_db() as db:
            return await save_subscription(db, 1, {"event_type": "review.completed", "repo_name": "alice/repo",
                "resource_type": None, "resource_id": None, "endpoint_id": 1, "enabled": True, "min_severity": "info"})

    fanout = asyncio.create_task(materialize())
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        writer = asyncio.create_task(subscribe())
        await asyncio.sleep(0.1)
        assert not writer.done()
    finally:
        release.set()
        await asyncio.wait_for(fanout, 10)
        if writer is not None:
            await asyncio.wait_for(writer, 10)
    async with live_db() as db:
        from backend.models.notification_models import UserNotification

        assert (await db.execute(select(UserNotification.user_id))).scalar_one() == 1


@pytest.mark.asyncio
async def test_live_lockout_rounds_and_withdrawal_have_durable_boundaries(live_db, monkeypatch):
    from datetime import timedelta

    from backend.core.time_service import now_utc
    from backend.services.announcement_service import withdraw_announcement
    from backend.services.notification_business import (
        emit_mfa_lockout_event,
        reset_mfa_lockout_round,
    )

    started = now_utc()
    async def lock():
        async with live_db() as db:
            item = await emit_mfa_lockout_event(db, 1, started, 60)
            await db.commit()
            return item.id
    assert len(set(await asyncio.gather(*(lock() for _ in range(3))))) == 1
    async with live_db() as db:
        await reset_mfa_lockout_round(db, 1, started + timedelta(seconds=1))
        assert await emit_mfa_lockout_event(db, 1, started, 60) is None
        await emit_mfa_lockout_event(db, 1, started + timedelta(seconds=2), 60)
        db.add(Announcement(id=646, title="Publication", content="Body", status="published", publication_version=1))
        await db.commit()
        item = await emit_event(db, event_type="announcement.published", resource_type="announcement",
            resource_id=646, run_key="1", title="Publication", content="Body", audience="all")
        await db.commit()
        event_id = item.id
    entered, release = asyncio.Event(), asyncio.Event()
    dispatcher = NotificationService(NotificationProviderRegistry({}))
    original = dispatcher._materialize_recipient
    async def pause(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(dispatcher, "_materialize_recipient", pause)
    async def fanout():
        async with live_db() as db:
            await dispatcher.materialize_event(db, event_id)
    async def withdraw():
        async with live_db() as db:
            await withdraw_announcement(db, 646)
    worker = asyncio.create_task(fanout())
    withdrawal = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        withdrawal = asyncio.create_task(withdraw())
        await asyncio.sleep(0.1)
        assert not withdrawal.done()
    finally:
        release.set()
        await worker
        if withdrawal is not None:
            await withdrawal
    async with live_db() as db:
        assert len((await db.execute(select(NotificationEvent).where(NotificationEvent.event_type == "security.lockout"))).scalars().all()) == 2
        assert (await db.execute(select(AnnouncementDeliveryHistory).where(AnnouncementDeliveryHistory.event_id == event_id))).scalar_one().source_delivery_id is not None
