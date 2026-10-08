"""Billing 2.0 purchase snapshots and source-linked legacy entitlement tests."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models import Base
from backend.models.payment_models import Order, Plan, UserSubscription
from backend.models.telegram_models import TelegramUser
from backend.services.payment_service import PaymentService
from backend.services.quota_service import QuotaService


class _AsyncSQLiteSession:
    """Use real SQLAlchemy persistence without an optional aiosqlite dependency."""

    def __init__(self, session):
        self._session = session

    def add(self, value):
        self._session.add(value)

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def flush(self):
        self._session.flush()

    async def get(self, model, value, **kwargs):
        return self._session.get(model, value, **kwargs)

    async def refresh(self, value, **kwargs):
        self._session.refresh(value, **kwargs)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    def begin_nested(self):
        transaction = self._session.begin_nested()

        class Nested:
            async def __aenter__(self):
                transaction.__enter__()

            async def __aexit__(self, *args):
                return transaction.__exit__(*args)

        return Nested()


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = _AsyncSQLiteSession(Session(engine, expire_on_commit=False))
    try:
        yield session
    finally:
        session._session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_one_time_purchase_never_permanently_expands_daily_limit(db):
    user = TelegramUser(github_username="buyer")
    plan = Plan(
        name="Legacy pack",
        plan_type="one_time",
        price_cents=100,
        pr_quota_bonus=10,
        issue_quota_bonus=20,
        agent_quota_bonus=3,
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    svc = PaymentService(db)
    await svc.grant_plan_to_user(user.id, plan.id, operator_id=user.id)
    assert (user.daily_quota, user.issue_daily_quota, user.agent_daily_quota) == (
        10,
        20,
        1,
    )
    for days in (1, 8, 32):
        QuotaService.reset_user_pr_quotas_if_expired(
            user, now_utc() + timedelta(days=days)
        )
        assert user.daily_quota == 10


@pytest.mark.asyncio
async def test_cycle_first_bonus_consumption_and_resets_do_not_mint_bonus(db):
    from backend.models.legacy_entitlement_models import (
        LegacyEntitlement,
        LegacyEntitlementEvent,
    )
    from backend.services.legacy_entitlement_service import LegacyEntitlementService

    user = TelegramUser(
        github_username="cycle", daily_quota=1, weekly_quota=100, monthly_quota=100
    )
    plan = Plan(
        name="Two extra requests",
        plan_type="one_time",
        price_cents=100,
        pr_quota_bonus=2,
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    await PaymentService(db).grant_plan_to_user(user.id, plan.id, user.id)
    svc = LegacyEntitlementService(db)
    assert await svc.consume(user, "pr_review", repo_name="owner/repo", number=1)
    assert (await svc.remaining(user.id))["pr"] == 2
    assert await svc.consume(user, "pr_review", repo_name="owner/repo", number=2)
    assert (await svc.remaining(user.id))["pr"] == 1
    assert await svc.consume(user, "pr_review", repo_name="owner/repo", number=3)
    assert not await svc.consume(user, "pr_review", repo_name="owner/repo", number=4)
    for days in (1, 8, 32):
        QuotaService.reset_user_pr_quotas_if_expired(
            user, now_utc() + timedelta(days=days)
        )
        await db.flush()
        assert (await svc.remaining(user.id))["pr"] == 0
        assert await svc.consume(
            user, "pr_review", repo_name="owner/repo", number=10 + days
        )
    entry = (await db.execute(select(LegacyEntitlement))).scalar_one()
    events = (
        (
            await db.execute(
                select(LegacyEntitlementEvent).where(
                    LegacyEntitlementEvent.entitlement_id == entry.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [event.units for event in events if event.kind == "consumption"] == [-1, -1]
    assert user.daily_quota == 1


@pytest.mark.asyncio
async def test_payment_fulfills_purchased_snapshot_and_callback_is_idempotent(db):
    from backend.models.legacy_entitlement_models import (
        LegacyEntitlement,
        PaymentReceipt,
    )

    user = TelegramUser(github_username="snapshot")
    plan = Plan(
        name="Purchased", plan_type="one_time", price_cents=100, pr_quota_bonus=2
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    svc = PaymentService(db)
    order = await svc.create_order(user.id, plan.id)
    plan.pr_quota_bonus = 900
    plan.price_cents = 999
    plan.name = "Changed"
    await db.flush()
    await svc.confirm_payment(order.order_no, "receipt-one", 100, "CNY")
    await svc.confirm_payment(order.order_no, "receipt-one", 100, "CNY")
    entry = (await db.execute(select(LegacyEntitlement))).scalar_one()
    assert entry.pr_remaining == 2
    assert entry.snapshot["name"] == "Purchased"
    assert entry.snapshot["price_cents"] == 100
    assert len((await db.execute(select(PaymentReceipt))).scalars().all()) == 1
    assert user.daily_quota == 10


@pytest.mark.asyncio
async def test_redeem_snapshot_repeat_is_same_order_and_does_not_use_second_slot(db):
    from backend.models.legacy_entitlement_models import (
        LegacyEntitlement,
        RedeemCodeRedemption,
    )

    user = TelegramUser(github_username="redeemer")
    second = TelegramUser(github_username="second-redeemer")
    plan = Plan(
        name="Redeemed", plan_type="one_time", price_cents=100, issue_quota_bonus=3
    )
    db.add(user)
    db.add(second)
    db.add(plan)
    await db.flush()
    svc = PaymentService(db)
    code = (await svc.generate_redeem_codes(plan.id, 1, max_uses=2))[0]
    plan.issue_quota_bonus = 99
    await db.flush()
    first_order = await svc.redeem_code(user.id, code.code)
    repeated = await svc.redeem_code(user.id, code.code)
    assert repeated.id == first_order.id
    assert code.used_count == 1
    await svc.redeem_code(second.id, code.code)
    assert code.used_count == 2
    assert (await svc.redeem_code(user.id, code.code)).id == first_order.id
    assert len((await db.execute(select(RedeemCodeRedemption))).scalars().all()) == 2
    assert [
        entry.issue_remaining
        for entry in (await db.execute(select(LegacyEntitlement))).scalars().all()
    ] == [3, 3]


@pytest.mark.asyncio
async def test_multiple_subscriptions_expiry_only_removes_that_source(db):
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.legacy_entitlement_service import LegacyEntitlementService

    user = TelegramUser(github_username="subscriber")
    first = Plan(
        name="First",
        plan_type="subscription",
        price_cents=100,
        duration_days=30,
        pr_daily_add=3,
    )
    second = Plan(
        name="Second",
        plan_type="subscription",
        price_cents=100,
        duration_days=30,
        pr_daily_add=5,
    )
    db.add(user)
    db.add(first)
    db.add(second)
    await db.flush()
    payment = PaymentService(db)
    await payment.grant_plan_to_user(user.id, first.id, user.id)
    await payment.grant_plan_to_user(user.id, second.id, user.id)
    service = LegacyEntitlementService(db)
    assert await service.effective_limits(user, "pr_review") == (18, 50, 200)
    entries = (
        (await db.execute(select(LegacyEntitlement).order_by(LegacyEntitlement.id)))
        .scalars()
        .all()
    )
    entries[0].expires_at = now_utc() - timedelta(seconds=1)
    subscriptions = (
        (await db.execute(select(UserSubscription).order_by(UserSubscription.id)))
        .scalars()
        .all()
    )
    subscriptions[0].expires_at = entries[0].expires_at
    await db.flush()
    assert await payment.expire_due_subscriptions(user.id) == 1
    assert await service.effective_limits(user, "pr_review") == (15, 50, 200)
    assert user.daily_quota == 10
    assert (await payment.get_active_subscription(user.id)).plan_id == second.id


@pytest.mark.asyncio
async def test_renewal_has_one_source_per_paid_period_without_stacking_limits(db):
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.legacy_entitlement_service import LegacyEntitlementService

    user = TelegramUser(github_username="renewal")
    plan = Plan(
        name="Monthly",
        plan_type="subscription",
        price_cents=100,
        duration_days=30,
        pr_daily_add=3,
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    await service.grant_plan_to_user(user.id, plan.id, user.id)
    await service.grant_plan_to_user(user.id, plan.id, user.id)
    entries = (
        (await db.execute(select(LegacyEntitlement).order_by(LegacyEntitlement.id)))
        .scalars()
        .all()
    )
    assert len(entries) == 2
    assert entries[1].starts_at == entries[0].expires_at
    assert await LegacyEntitlementService(db).effective_limits(user, "pr_review") == (
        13,
        50,
        200,
    )
    assert user.daily_quota == 10


@pytest.mark.asyncio
async def test_historical_migration_dry_run_review_and_idempotent_repair(db):
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )

    user = TelegramUser(github_username="historical", daily_quota=20)
    actor = TelegramUser(github_username="operator", role="super_admin")
    plan = Plan(
        name="Current plan is not historical evidence",
        plan_type="one_time",
        price_cents=100,
        pr_quota_bonus=99,
    )
    db.add(user)
    db.add(actor)
    db.add(plan)
    await db.flush()
    order = Order(
        order_no="historical-order",
        user_id=user.id,
        plan_id=plan.id,
        amount_cents=100,
        currency="CNY",
        status="fulfilled",
    )
    db.add(order)
    await db.flush()
    entry = {
        "order_id": order.id,
        "user_id": user.id,
        "snapshot": {
            "id": plan.id,
            "name": "Purchased ten",
            "plan_type": "one_time",
            "pr_quota_bonus": 10,
            "credit_grant": "0",
        },
        "remaining": {"pr": 6},
        "inflated_daily": {"pr": 10},
        "evidence": "Archived invoice and reviewed usage log",
    }
    svc = LegacyBillingMigrationService(db)
    audit = await svc.audit()
    assert audit["orders"][0]["status"] == "needs_historical_evidence"
    report = await svc.repair(entry, actor_id=actor.id)
    assert report["status"] == "dry_run"
    assert user.daily_quota == 20
    assert (await db.execute(select(LegacyEntitlement))).scalars().all() == []
    report = await svc.repair(entry, actor_id=actor.id, dry_run=False)
    assert report["status"] == "repaired"
    assert user.daily_quota == 10
    assert (await db.execute(select(LegacyEntitlement))).scalar_one().pr_remaining == 6
    assert (await svc.repair(entry, actor_id=actor.id, dry_run=False))[
        "status"
    ] == "already_migrated"
    assert user.daily_quota == 10


async def _historical_periodic_source(db):
    from backend.core.time_service import format_rfc3339

    base = {
        "pr_daily": 10,
        "pr_weekly": 50,
        "pr_monthly": 200,
        "issue_daily": 20,
        "issue_weekly": 100,
        "issue_monthly": 500,
        "agent_daily": 1,
        "agent_weekly": 10,
        "agent_monthly": 40,
    }
    applied = {
        "pr_daily": 5,
        "pr_weekly": 5,
        "pr_monthly": 5,
        "issue_daily": 2,
        "issue_weekly": 2,
        "issue_monthly": 2,
        "agent_daily": 1,
        "agent_weekly": 1,
        "agent_monthly": 2,
    }
    fields = {
        key: f"{'' if key.startswith('pr_') else key.split('_')[0] + '_'}{key.split('_')[1]}_quota"
        for key in base
    }
    initial = {key: base[key] + applied[key] for key in base}
    initial["pr_daily"] += 3
    user = TelegramUser(
        github_username="historical-periodic",
        **{fields[key]: value for key, value in initial.items()},
    )
    actor = TelegramUser(github_username="periodic-auditor", role="super_admin")
    plan = Plan(
        name="Current offer is unrelated",
        plan_type="subscription",
        duration_days=90,
        price_cents=100,
    )
    for row in (user, actor, plan):
        db.add(row)
    await db.flush()
    order = Order(
        order_no="historic-periodic-order",
        user_id=user.id,
        plan_id=plan.id,
        amount_cents=100,
        status="fulfilled",
    )
    db.add(order)
    await db.flush()
    entry = {
        "order_id": order.id,
        "user_id": user.id,
        "snapshot": {
            "id": plan.id,
            "name": "Evidenced historical periodic benefits",
            "plan_type": "subscription",
            "pr_quota_bonus": 3,
            "credit_grant": "0",
            **{f"{key}_add": value for key, value in applied.items()},
        },
        "remaining": {"pr": 2},
        "inflated_daily": {"pr": 3},
        "applied_periodic": applied,
        "expires_at": format_rfc3339(now_utc() + timedelta(days=2)),
        "evidence": "Archived order and audited pre-upgrade quota application log",
    }
    return user, actor, order, entry, base, fields, initial


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_applied", [True, False])
async def test_historical_periodic_migration_separates_base_and_expires_only_source(
    db, previously_applied
):
    from backend.models.legacy_entitlement_models import (
        LegacyEntitlement,
        LegacyEntitlementEvent,
    )
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )
    from backend.services.legacy_entitlement_service import LegacyEntitlementService

    (
        user,
        actor,
        order,
        entry,
        base,
        fields,
        initial,
    ) = await _historical_periodic_source(db)
    if not previously_applied:
        # Explicitly evidenced non-application must not subtract bought limits
        # from unrelated base allowance merely because the old offer had them.
        user.weekly_quota -= entry["applied_periodic"]["pr_weekly"]
        initial["pr_weekly"] = user.weekly_quota
        entry["applied_periodic"]["pr_weekly"] = 0
    other = await LegacyEntitlementService(db).grant(
        user.id,
        {"rate_limits": {"pr_daily": 4}, "credit_grant": "0"},
        "other-paid-source",
    )
    await db.commit()
    service = LegacyBillingMigrationService(db)
    preview = await service.repair(entry, actor_id=actor.id)
    assert preview["status"] == "dry_run"
    assert {key: getattr(user, field) for key, field in fields.items()} == initial
    assert order.plan_snapshot is None
    assert (
        await db.execute(
            select(LegacyEntitlement).where(LegacyEntitlement.order_id == order.id)
        )
    ).scalar_one_or_none() is None
    repaired = await service.repair(entry, actor_id=actor.id, dry_run=False)
    await db.commit()
    assert repaired["base_before"] == initial
    assert repaired["base_after"] == base
    assert repaired["applied_periodic"] == entry["applied_periodic"]
    assert {key: getattr(user, field) for key, field in fields.items()} == base
    legacy = LegacyEntitlementService(db)
    assert await legacy.effective_limits(user, "pr_review") == (19, 55, 205)
    assert await legacy.effective_limits(user, "issue_analysis") == (22, 102, 502)
    assert await legacy.effective_limits(user, "agent") == (2, 11, 42)
    source = (
        await db.execute(
            select(LegacyEntitlement).where(LegacyEntitlement.order_id == order.id)
        )
    ).scalar_one()
    assert source.pr_remaining == 2
    audit = (
        await db.execute(
            select(LegacyEntitlementEvent).where(
                LegacyEntitlementEvent.event_key == f"audit:migration:order:{order.id}"
            )
        )
    ).scalar_one()
    assert audit.detail["base_after"] == base
    assert (await service.repair(entry, actor_id=actor.id, dry_run=False))[
        "status"
    ] == "already_migrated"
    assert {key: getattr(user, field) for key, field in fields.items()} == base
    source.expires_at = now_utc() - timedelta(seconds=1)
    await db.flush()
    assert await legacy.expire_due(user.id) == 1
    assert await legacy.effective_limits(user, "pr_review") == (14, 50, 200)
    assert await legacy.effective_limits(user, "issue_analysis") == (20, 100, 500)
    assert await legacy.effective_limits(user, "agent") == (1, 10, 40)
    assert other.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["missing", "partial", "boolean", "excessive", "unknown"]
)
async def test_historical_periodic_migration_refuses_unevidenced_base_changes(
    db, invalid
):
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )
    from backend.services.payment_service import PaymentError

    user, actor, order, entry, _, fields, initial = await _historical_periodic_source(
        db
    )
    if invalid == "missing":
        entry.pop("applied_periodic")
    elif invalid == "partial":
        entry["applied_periodic"] = {"pr_daily": 5}
    elif invalid == "boolean":
        entry["applied_periodic"]["pr_daily"] = True
    elif invalid == "excessive":
        entry["applied_periodic"]["pr_daily"] = 6
    else:
        entry["applied_periodic"]["unconfirmed_daily"] = 1
    with pytest.raises(PaymentError, match="periodic"):
        await LegacyBillingMigrationService(db).repair(
            entry, actor_id=actor.id, dry_run=False
        )
    assert {key: getattr(user, field) for key, field in fields.items()} == initial
    assert order.plan_snapshot is None
    assert (await db.execute(select(LegacyEntitlement))).scalars().all() == []


@pytest.mark.asyncio
async def test_historical_migration_rejects_unevidenced_or_oversized_repair(db):
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="unknown", daily_quota=20)
    plan = Plan(name="Unknown source", plan_type="one_time", price_cents=100)
    db.add(user)
    db.add(plan)
    await db.flush()
    order = Order(
        order_no="unknown-order",
        user_id=user.id,
        plan_id=plan.id,
        amount_cents=100,
        status="fulfilled",
    )
    db.add(order)
    await db.flush()
    svc = LegacyBillingMigrationService(db)
    entry = {
        "order_id": order.id,
        "user_id": user.id,
        "snapshot": {"id": plan.id, "pr_quota_bonus": 10},
        "remaining": {"pr": 11},
        "evidence": "Archived invoice",
    }
    with pytest.raises(PaymentError, match="exceeds evidenced"):
        await svc.repair(entry, actor_id=user.id)
    entry.pop("evidence")
    with pytest.raises(PaymentError, match="reviewed source evidence"):
        await svc.repair(entry, actor_id=user.id)
    assert user.daily_quota == 20


@pytest.mark.asyncio
async def test_old_private_grant_api_cannot_silently_reintroduce_bug(db):
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="guard")
    plan = Plan(name="Guard", plan_type="one_time", price_cents=100, pr_quota_bonus=10)
    db.add(user)
    db.add(plan)
    await db.flush()
    with pytest.raises(PaymentError, match="source-linked order"):
        await PaymentService(db)._apply_plan_to_user(user, plan)
    assert user.daily_quota == 10


@pytest.mark.asyncio
async def test_tron_requires_exact_decimal_amount_and_confirmed_contract(db):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from backend.services.payment.tron_gateway import USDT_TRC20_CONTRACT, TronGateway

    gateway = TronGateway("destination")
    gateway._get_trc20_transfers = AsyncMock(
        return_value=[
            {
                "to": "destination",
                "token_info": {"symbol": "USDT", "address": "fake-contract"},
                "value": "1000000",
                "transaction_id": "fake",
                "block_timestamp": 1000,
            },
            {
                "to": "destination",
                "token_info": {"symbol": "USDT", "address": USDT_TRC20_CONTRACT},
                "value": "999999",
                "transaction_id": "short",
                "block_timestamp": 1000,
            },
            {
                "to": "destination",
                "token_info": {"symbol": "USDT", "address": USDT_TRC20_CONTRACT},
                "value": "1000000",
                "transaction_id": "too-old",
                "block_timestamp": 999,
            },
        ]
    )
    result = await gateway.check_payment_by_amount(
        "order", Decimal("1.000000"), min_block_timestamp=1000
    )
    assert result.status == "waiting"
    gateway._get_trc20_transfers.return_value.append(
        {
            "to": "destination",
            "token_info": {"address": USDT_TRC20_CONTRACT},
            "value": "1000000",
            "transaction_id": "exact",
            "block_timestamp": 1000,
        }
    )
    result = await gateway.check_payment_by_amount(
        "order", Decimal("1.000000"), min_block_timestamp=1000
    )
    assert result.status == "completed"
    assert result.provider_tx_id == "exact"
    assert result.amount_cents == 1000000
    assert result.raw_data["amount"] == "1"


@pytest.mark.asyncio
async def test_credits_purchase_full_refund_append_ledger_and_do_not_touch_other_sources(
    db,
):
    from decimal import Decimal

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.models.legacy_entitlement_models import PaymentRefundAttempt

    user = TelegramUser(github_username="refundable")
    plan = Plan(
        name="Credits", plan_type="one_time", price_cents=100, credit_grant=Decimal(5)
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    order = await service.grant_plan_to_user(user.id, plan.id, user.id)
    wallet = await db.get(BillingWallet, user.id)
    assert wallet.balance_units == 5_000_000
    assert (
        await service.process_refund(order.id, operator_id=user.id)
    ).status == "refunded"
    assert (
        await service.process_refund(order.id, operator_id=user.id)
    ).status == "refunded"
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    ledger = (
        (await db.execute(select(BillingTransaction).order_by(BillingTransaction.id)))
        .scalars()
        .all()
    )
    assert [row.delta_units for row in ledger] == [5_000_000, -5_000_000]
    assert ledger[1].reference_transaction_id == ledger[0].id
    assert len((await db.execute(select(PaymentRefundAttempt))).scalars().all()) == 1
    assert user.daily_quota == 10


@pytest.mark.asyncio
async def test_partial_refunds_round_cumulative_and_repeated_request_is_idempotent(
    db, monkeypatch
):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingTransaction, BillingWallet

    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    user = TelegramUser(github_username="partial")
    plan = Plan(
        name="Fractional sample",
        plan_type="one_time",
        price_cents=3,
        credit_grant=Decimal(1),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    order = await service.create_order(user.id, plan.id)
    await service.confirm_payment(order.order_no, "partial-payment", 3, "CNY")
    await service.process_refund(
        order.id, amount_cents=1, operator_id=user.id, idempotency_key="partial:first"
    )
    assert (await db.get(BillingWallet, user.id)).balance_units == 666667
    await service.process_refund(
        order.id, amount_cents=1, operator_id=user.id, idempotency_key="partial:first"
    )
    assert (await db.get(BillingWallet, user.id)).balance_units == 666667
    await service.process_refund(
        order.id, amount_cents=1, operator_id=user.id, idempotency_key="partial:second"
    )
    assert (await db.get(BillingWallet, user.id)).balance_units == 333334
    await service.process_refund(
        order.id, amount_cents=1, operator_id=user.id, idempotency_key="partial:third"
    )
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert order.refunded_amount_cents == 3
    assert order.status == "refunded"
    assert [
        row.delta_units
        for row in (
            await db.execute(select(BillingTransaction).order_by(BillingTransaction.id))
        )
        .scalars()
        .all()
    ] == [1000000, -333333, -333333, -333334]


async def _external_credits_order(db, name):
    from decimal import Decimal

    user = TelegramUser(github_username=name, role="super_admin")
    plan = Plan(
        name="Refund fixture",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    order = await service.create_order(user.id, plan.id)
    await service.confirm_payment(order.order_no, f"payment:{name}", 100, "CNY")
    order.payment_provider = "stripe"
    await db.commit()
    return service, user, order


@pytest.mark.asyncio
async def test_unknown_external_refund_is_durable_held_and_never_automatically_replayed(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.models.legacy_entitlement_models import PaymentRefundAttempt
    from backend.services.payment.gateway_base import RefundResult
    from backend.services.payment_service import PaymentError

    service, user, order = await _external_credits_order(db, "unknown-refund")
    gateway = AsyncMock()

    async def refund(**kwargs):
        assert not db._session.in_transaction(), (
            "Wallet and order locks must be released before upstream I/O"
        )
        return RefundResult(success=False, error_message="transport timeout")

    gateway.refund.side_effect = refund
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(PaymentError) as error:
        await service.process_refund(order.id, operator_id=user.id)
    assert error.value.code == "refund_reconciliation_required"
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    wallet = await db.get(BillingWallet, user.id)
    assert attempt.status == "unknown"
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 5_000_000)
    with pytest.raises(PaymentError, match="reconciliation"):
        await service.process_refund(order.id, operator_id=user.id)
    assert gateway.refund.await_count == 1
    await service.reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="refunded",
        evidence="Verified provider refund object",
        provider_refund_id="refund-confirmed",
    )
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    assert order.status == "refunded"
    assert (
        sum(
            row.delta_units
            for row in (await db.execute(select(BillingTransaction))).scalars().all()
        )
        == 0
    )


@pytest.mark.asyncio
async def test_refund_external_success_write_interruption_recovers_without_second_request(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingWallet
    from backend.models.legacy_entitlement_models import PaymentRefundAttempt
    from backend.services.payment.gateway_base import RefundResult

    service, user, order = await _external_credits_order(db, "recover-refund")
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, refund_id="refund-durable", amount_cents=100, status="succeeded"
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    finalize = service._finalize_refund
    service._finalize_refund = AsyncMock(
        side_effect=RuntimeError("interrupted final ledger write")
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await service.process_refund(order.id, operator_id=user.id)
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.status == "upstream_succeeded"
    assert (await db.get(BillingWallet, user.id)).reserved_units == 5_000_000
    service._finalize_refund = finalize
    await service.process_refund(order.id, operator_id=user.id)
    await db.commit()
    assert gateway.refund.await_count == 1
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    assert order.status == "refunded"


@pytest.mark.asyncio
async def test_verified_unrefunded_outcome_releases_source_hold_without_new_balance(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingWallet
    from backend.models.legacy_entitlement_models import PaymentRefundAttempt
    from backend.services.payment_service import PaymentError

    service, user, order = await _external_credits_order(db, "failed-refund")
    gateway = AsyncMock()
    gateway.refund.side_effect = RuntimeError("unknown network outcome")
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(RuntimeError):
        await service.process_refund(order.id, operator_id=user.id)
    await db.rollback()
    attempt = (await db.execute(select(PaymentRefundAttempt))).scalar_one()
    assert attempt.status == "pending"
    await service.reconcile_refund(
        attempt.id,
        operator_id=user.id,
        upstream_status="not_refunded",
        evidence="Verified no refund occurred at provider",
    )
    await db.commit()
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 0)
    assert order.status == "fulfilled"
    with pytest.raises(PaymentError, match="reconciliation"):
        await service.process_refund(order.id, operator_id=user.id)
    assert gateway.refund.await_count == 1


@pytest.mark.parametrize("scenario", ("callback", "redemption"))
def test_real_parallel_duplicate_payment_and_code_grants_once(tmp_path, scenario):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from threading import Barrier

    from sqlalchemy.exc import IntegrityError

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.models.payment_models import RedeemCode
    from backend.services.payment_service import PaymentError

    engine = create_engine(
        f"sqlite:///{tmp_path / 'parallel.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as setup:
        user = TelegramUser(github_username=f"parallel-{scenario}")
        plan = Plan(
            name="Parallel Credits",
            plan_type="one_time",
            price_cents=100,
            credit_grant=Decimal(5),
        )
        setup.add_all((user, plan))
        setup.flush()
        adapter = _AsyncSQLiteSession(setup)
        service = PaymentService(adapter)
        if scenario == "callback":
            source = asyncio.run(service.create_order(user.id, plan.id))
            source_identity = source.order_no
            query_model = Order
        else:
            source = asyncio.run(service.generate_redeem_codes(plan.id, 1, max_uses=2))[
                0
            ]
            source_identity = source.code
            query_model = RedeemCode
        user_id = user.id
        setup.commit()
    barrier = Barrier(2)

    class RacingSession(_AsyncSQLiteSession):
        synchronized = False

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", ())
            if (
                not self.synchronized
                and descriptions
                and descriptions[0].get("entity") is query_model
            ):
                self.synchronized = True
                barrier.wait(timeout=10)
            return result

    def work(_number):
        with Session(engine, expire_on_commit=False) as session:
            adapter = RacingSession(session)
            service = PaymentService(adapter)
            for attempt in range(2):
                try:

                    async def grant():
                        if scenario == "callback":
                            return await service.confirm_payment(
                                source_identity, "parallel-payment", 100, "CNY"
                            )
                        return await service.redeem_code(user_id, source_identity)

                    order = asyncio.run(grant())
                    session.commit()
                    return order.id
                except IntegrityError, PaymentError:
                    session.rollback()
                    if attempt:
                        raise

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            order_ids = list(executor.map(work, range(2)))
        assert order_ids[0] == order_ids[1]
        with Session(engine) as verification:
            transactions = verification.scalars(select(BillingTransaction)).all()
            assert len(transactions) == 1
            assert transactions[0].delta_units == 5_000_000
            assert verification.get(BillingWallet, user_id).balance_units == 5_000_000
            if scenario == "redemption":
                assert verification.scalar(select(RedeemCode)).used_count == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_admin_grant_key_is_source_idempotent_and_conflict_rejects(db):
    from decimal import Decimal

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="admin-grant")
    other = TelegramUser(github_username="admin-other")
    plan = Plan(
        name="Audited grant",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(other)
    db.add(plan)
    await db.flush()
    svc = PaymentService(db)
    first = await svc.grant_plan_to_user(
        user.id, plan.id, user.id, idempotency_key="admin-request-key"
    )
    again = await svc.grant_plan_to_user(
        user.id, plan.id, user.id, idempotency_key="admin-request-key"
    )
    assert first.id == again.id
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
    with pytest.raises(PaymentError, match="conflicts"):
        await svc.grant_plan_to_user(
            other.id, plan.id, user.id, idempotency_key="admin-request-key"
        )


@pytest.mark.asyncio
async def test_refund_of_consumed_purchase_never_claws_back_other_credit_sources(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingWallet
    from backend.services.billing_service import BillingService
    from backend.services.payment_service import PaymentError

    service, user, order = await _external_credits_order(db, "spent-refund")
    billing = BillingService(db)
    await billing.adjust(
        user.id,
        "-1",
        idempotency_key="spend-purchased-source",
        actor_id=user.id,
        reason="Explicit source allocation fixture",
    )
    await billing.grant(user.id, "100", "unrelated-new-source")
    await db.commit()
    gateway = AsyncMock()
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    with pytest.raises(PaymentError) as error:
        await service.process_refund(order.id, operator_id=user.id)
    assert error.value.code == "credits_already_consumed"
    await db.rollback()
    gateway.refund.assert_not_awaited()
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (104_000_000, 0)
    assert order.status == "fulfilled"


@pytest.mark.asyncio
async def test_insufficient_credits_is_distinct_and_does_not_consume_legacy_allowance(
    db, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.services.legacy_entitlement_service import LegacyEntitlementService
    from backend.services.telegram_service import TelegramService

    async def setting(name, **kwargs):
        return {"billing_enabled": True, "billing_initial_reserve_credits": "0"}.get(
            name
        )

    monkeypatch.setattr("backend.services.billing_service.get_dynamic_config", setting)
    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled",
        AsyncMock(return_value=False),
    )
    user = TelegramUser(
        github_username="no-credits", daily_quota=0, weekly_quota=0, monthly_quota=0
    )
    plan = Plan(
        name="Legacy admission", plan_type="one_time", price_cents=100, pr_quota_bonus=2
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    await PaymentService(db).grant_plan_to_user(user.id, plan.id, user.id)
    allowed, reason = await TelegramService(db).check_and_consume_quota(
        user.github_username, "owner/repo", 1
    )
    assert not allowed
    assert reason == "Credits 余额不足"
    assert (await LegacyEntitlementService(db).remaining(user.id))["pr"] == 2
    assert (user.daily_used, user.weekly_used, user.monthly_used) == (0, 0, 0)


@pytest.mark.asyncio
async def test_same_business_admission_does_not_consume_bonus_or_cycle_again(db):
    from backend.models.legacy_entitlement_models import RateLimitAdmission
    from backend.services.legacy_entitlement_service import LegacyEntitlementService

    user = TelegramUser(
        github_username="admission", daily_quota=1, weekly_quota=100, monthly_quota=100
    )
    plan = Plan(
        name="Legacy remaining", plan_type="one_time", price_cents=100, pr_quota_bonus=2
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    await PaymentService(db).grant_plan_to_user(user.id, plan.id, user.id)
    svc = LegacyEntitlementService(db)
    for _ in range(2):
        assert await svc.consume(
            user,
            "pr_review",
            repo_name="owner/repo",
            number=1,
            event_key="operation:cycle",
        )
    assert user.daily_used == 1
    assert (await svc.remaining(user.id))["pr"] == 2
    for _ in range(2):
        assert await svc.consume(
            user,
            "pr_review",
            repo_name="owner/repo",
            number=1,
            event_key="operation:bonus",
        )
    assert (await svc.remaining(user.id))["pr"] == 1
    assert len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 2
    with pytest.raises(ValueError, match="conflicts"):
        await svc.consume(
            user,
            "pr_review",
            repo_name="unrelated/repo",
            number=1,
            event_key="operation:bonus",
        )


@pytest.mark.asyncio
async def test_audited_source_conversion_is_atomic_dry_run_and_idempotent(db):
    from decimal import Decimal

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="converted-owner")
    actor = TelegramUser(github_username="conversion-operator", role="super_admin")
    plan = Plan(
        name="Paid historical value",
        plan_type="one_time",
        price_cents=100,
        pr_quota_bonus=10,
    )
    db.add(user)
    db.add(actor)
    db.add(plan)
    await db.flush()
    payment = PaymentService(db)
    order = await payment.create_order(user.id, plan.id)
    await payment.confirm_payment(order.order_no, "conversion-purchase", 100, "CNY")
    source = (await db.execute(select(LegacyEntitlement))).scalar_one()
    service = LegacyBillingMigrationService(db)
    result = await service.convert_source(
        source.id,
        Decimal("7.25"),
        actor_id=actor.id,
        evidence="Business-approved sample conversion, not a production rate",
    )
    assert result["status"] == "dry_run"
    assert source.pr_remaining == 10
    assert (await db.execute(select(BillingTransaction))).scalars().all() == []
    result = await service.convert_source(
        source.id,
        Decimal("7.25"),
        actor_id=actor.id,
        evidence="Reviewed purchased source",
        dry_run=False,
    )
    assert result["status"] == "converted"
    assert source.pr_remaining == 0
    assert source.converted_at is not None
    assert (await db.get(BillingWallet, user.id)).balance_units == 7_250_000
    assert (
        await db.execute(select(BillingTransaction))
    ).scalar_one().kind == "migration"
    assert (
        await service.convert_source(
            source.id,
            Decimal("7.25"),
            actor_id=actor.id,
            evidence="Same approved source",
            dry_run=False,
        )
    )["status"] == "already_converted"
    with pytest.raises(PaymentError, match="conflicts"):
        await service.convert_source(
            source.id,
            Decimal(9),
            actor_id=actor.id,
            evidence="Changed amount",
            dry_run=False,
        )
    assert (await db.get(BillingWallet, user.id)).balance_units == 7_250_000


@pytest.mark.asyncio
async def test_tron_invoice_amount_identity_never_reused_after_cancel(db, monkeypatch):
    from decimal import Decimal
    from unittest.mock import AsyncMock, Mock

    from backend.services.payment.tron_gateway import TronGateway

    user = TelegramUser(github_username="tron-invoice")
    plan = Plan(
        name="Exact invoice", plan_type="one_time", price_cents=100, currency="USD"
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    gateway = TronGateway("trusted-wallet")
    gateway._calculate_unique_amount = Mock(
        side_effect=[Decimal("1.000001"), Decimal("1.000001"), Decimal("1.000002")]
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    svc = PaymentService(db)
    svc._get_provider_currency = AsyncMock(return_value="USD")
    first = await svc.create_order(user.id, plan.id, "tron")
    original_identity = first.invoice_identity
    await svc.cancel_order(first.order_no, user.id)
    assert first.invoice_identity == original_identity
    second = await svc.create_order(user.id, plan.id, "tron")
    assert second.invoice_identity != original_identity
    assert gateway._calculate_unique_amount.call_count == 3
    assert first.invoice_identity == original_identity


@pytest.mark.asyncio
async def test_tron_historical_invoice_without_identity_cannot_auto_fulfill(db):
    import json

    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="tron-historical")
    plan = Plan(
        name="Old invoice", plan_type="one_time", price_cents=100, currency="USD"
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    svc = PaymentService(db)
    order = await svc.create_order(user.id, plan.id)
    order.payment_provider = "tron"
    order.metadata_json = json.dumps(
        {
            "pay_address": "trusted-wallet",
            "pay_amount": "1.000001",
            "pay_currency": "usdttrc20",
        }
    )
    await db.flush()
    with pytest.raises(PaymentError, match="identity must be audited"):
        await svc.confirm_payment(order.order_no, "old-transfer", 100, "USD")
    assert order.status == "pending"


@pytest.mark.asyncio
async def test_tron_invoice_audit_dry_run_restores_unique_only_and_quarantines_collisions(
    db,
):
    import json

    user = TelegramUser(github_username="tron-audit", role="super_admin")
    plan = Plan(
        name="Gateway source", plan_type="one_time", price_cents=100, currency="USD"
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    service = PaymentService(db)
    orders = []
    for index in range(3):
        order = await service.create_order(user.id, plan.id)
        order.payment_provider = "tron"
        order.metadata_json = json.dumps(
            {
                "pay_address": "trusted-wallet",
                "pay_currency": "usdttrc20",
                "pay_amount": "1.000001" if index < 2 else "1.000002",
            }
        )
        orders.append(order)
    await db.flush()
    report = await service.audit_tron_invoices()
    assert [row["status"] for row in report["orders"]] == [
        "ambiguous",
        "ambiguous",
        "dry_run",
    ]
    assert all(order.invoice_identity is None for order in orders)
    report = await service.audit_tron_invoices(operator_id=user.id, dry_run=False)
    assert [row["status"] for row in report["orders"]] == [
        "ambiguous",
        "ambiguous",
        "identified",
    ]
    assert orders[0].invoice_identity is None
    assert orders[1].invoice_identity is None
    assert json.loads(orders[0].metadata_json)["invoice_audit_status"] == "ambiguous"
    assert orders[2].invoice_identity
    assert (await service.audit_tron_invoices(operator_id=user.id, dry_run=False))[
        "orders"
    ][2]["status"] == "already_identified"


def test_tron_retry_tags_enumerate_all_atomic_suffixes_exactly_once():
    from decimal import Decimal

    from backend.services.payment.tron_gateway import TronGateway

    seed = "ORD20261008000000ABCDEF12"
    amounts = {
        TronGateway._calculate_unique_amount(Decimal(1), f"{seed}T{index:04d}")
        for index in range(TronGateway.INVOICE_SUFFIX_VARIANTS)
    }
    assert len(amounts) == 10000
    assert min(amounts) == Decimal("1.000000")
    assert max(amounts) == Decimal("1.009999")
    for index in (0, 1, 9999):
        order_no = f"{seed}T{index:04d}"
        assert TronGateway._extract_base_amount(
            TronGateway._calculate_unique_amount(Decimal(1), order_no), order_no
        ) == Decimal("1.000000")


@pytest.mark.asyncio
async def test_new_paid_legacy_quota_sale_requires_confirmed_credits_after_activation(
    db, monkeypatch
):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="new-credits-era")
    plan = Plan(
        name="Unconverted legacy offer",
        plan_type="one_time",
        price_cents=100,
        pr_quota_bonus=10,
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value=True),
    )
    service = PaymentService(db)
    with pytest.raises(PaymentError) as error:
        await service.create_order(user.id, plan.id)
    assert error.value.code == "legacy_plan_needs_credits"
    assert (await db.execute(select(Order))).scalars().all() == []
    plan.credit_grant = Decimal(5)
    await db.flush()
    order = await service.create_order(user.id, plan.id)
    assert order.plan_snapshot["credit_grant"] == "5"


@pytest.mark.asyncio
async def test_historical_code_snapshot_requires_audit_and_remains_immutable(db):
    from backend.models.payment_models import RedeemCode
    from backend.services.legacy_billing_migration_service import (
        LegacyBillingMigrationService,
    )
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="legacy-code-owner")
    actor = TelegramUser(github_username="code-auditor", role="super_admin")
    plan = Plan(
        name="Changed current plan",
        plan_type="one_time",
        price_cents=999,
        currency="USD",
        pr_quota_bonus=99,
    )
    db.add(user)
    db.add(actor)
    db.add(plan)
    await db.flush()
    code = RedeemCode(code="HISTORICAL-CODE", plan_id=plan.id, max_uses=1, used_count=0)
    db.add(code)
    await db.flush()
    payment = PaymentService(db)
    with pytest.raises(PaymentError, match="audited purchased snapshot"):
        await payment.redeem_code(user.id, code.code)
    assert code.used_count == 0
    reviewed = {
        "id": plan.id,
        "name": "Originally purchased",
        "plan_type": "one_time",
        "price_cents": 100,
        "currency": "CNY",
        "credit_grant": "0",
        "pr_quota_bonus": 10,
    }
    migration = LegacyBillingMigrationService(db)
    assert (
        await migration.prepare_code_snapshot(
            code.id, reviewed, actor_id=actor.id, evidence="Archived voucher offer"
        )
    )["status"] == "dry_run"
    assert code.plan_snapshot is None
    assert (
        await migration.prepare_code_snapshot(
            code.id,
            reviewed,
            actor_id=actor.id,
            evidence="Archived voucher offer",
            dry_run=False,
        )
    )["status"] == "prepared"
    assert (
        await migration.prepare_code_snapshot(
            code.id, reviewed, actor_id=actor.id, evidence="Same source", dry_run=False
        )
    )["status"] == "already_prepared"
    plan.is_active = False
    await db.flush()
    order = await payment.redeem_code(user.id, code.code)
    assert order.amount_cents == 100
    assert order.currency == "CNY"
    assert order.plan_snapshot["pr_quota_bonus"] == 10
    with pytest.raises(PaymentError, match="different immutable"):
        await migration.prepare_code_snapshot(
            code.id,
            {**reviewed, "pr_quota_bonus": 12},
            actor_id=actor.id,
            evidence="Changed claim",
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_plan_deactivation_and_edit_do_not_change_existing_voucher_offer(db):
    from backend.models.legacy_entitlement_models import LegacyEntitlement
    from backend.services.payment_service import PaymentError

    user = TelegramUser(github_username="voucher-owner")
    plan = Plan(
        name="Original offer",
        plan_type="one_time",
        price_cents=100,
        currency="CNY",
        pr_quota_bonus=2,
    )
    other = Plan(name="Other", plan_type="one_time", price_cents=1, currency="USD")
    db.add(user)
    db.add(plan)
    db.add(other)
    await db.flush()
    payment = PaymentService(db)
    code = (await payment.generate_redeem_codes(plan.id, 1))[0]
    with pytest.raises(PaymentError, match="issued redeem codes"):
        await payment.delete_plan(plan.id, hard_delete=True)
    with pytest.raises(PaymentError, match="immutable purchased offer"):
        await payment.update_redeem_code(code.id, plan_id=other.id)
    plan.price_cents = 999
    plan.currency = "USD"
    plan.is_active = False
    await db.flush()
    order = await payment.redeem_code(user.id, code.code)
    assert order.amount_cents == 100
    assert order.currency == "CNY"
    assert (await db.execute(select(LegacyEntitlement))).scalar_one().pr_remaining == 2


@pytest.mark.asyncio
async def test_account_removal_keeps_billing_identity_and_source_history(db):
    from decimal import Decimal

    from backend.models.billing_models import BillingTransaction
    from backend.services.telegram_service import TelegramService

    user = TelegramUser(github_username="retained-ledger")
    plan = Plan(
        name="Preserve identity",
        plan_type="one_time",
        price_cents=100,
        credit_grant=Decimal(5),
    )
    db.add(user)
    db.add(plan)
    await db.flush()
    await PaymentService(db).grant_plan_to_user(user.id, plan.id, user.id)
    removed, message = await TelegramService(db).remove_user(user.github_username)
    assert removed
    assert "账务与权益记录已保留" in message
    assert (await db.get(TelegramUser, user.id)).is_active is False
    assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_fx_uses_currency_minor_units_and_explicit_decimal_rate(db, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.services.payment.currency_units import format_minor_amount
    from backend.services.payment_service import PaymentError

    # Fixed test fixture only: one major CNY buys 20 major JPY.
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="20"),
    )
    service = PaymentService(db)
    assert await service._convert_currency(1500, "CNY", "JPY") == 300
    assert format_minor_amount(1500, "CNY") == "15.00"
    assert format_minor_amount(300, "JPY") == "300"
    assert format_minor_amount(1234, "KWD") == "1.234"
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value=None),
    )
    with pytest.raises(PaymentError, match="Confirmed decimal exchange rate"):
        await service._convert_currency(1500, "CNY", "JPY")


@pytest.mark.parametrize("credits", ("9000000000000.000001", "1e30", 1.5, True))
def test_plan_credit_grant_cannot_exceed_wallet_precision_or_integer_range(credits):
    from backend.services.payment_service import PaymentError

    with pytest.raises(PaymentError, match="exact|wallet range"):
        PaymentService._validate_billing_plan(credits, None, None)
