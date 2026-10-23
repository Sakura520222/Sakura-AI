"""Wallet capacity and one-time/periodic rate admission commit atomically."""

import pytest
from sqlalchemy import select

from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    RateLimitAdmission,
)
from backend.models.telegram_models import QuotaUsageLog, TelegramUser
from backend.services.billing_service import BillingService
from backend.services.telegram_service import TelegramService
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


async def prepare(factory, monkeypatch, feature, *, fund=True, periodic=True, legacy=2):
    async def payment_disabled():
        return False

    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled", payment_disabled
    )
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        prefix = {"pr_review": "", "issue_analysis": "issue_", "agent": "agent_"}[
            feature
        ]
        for period in ("daily", "weekly", "monthly"):
            setattr(user, f"{prefix}{period}_quota", 10 if periodic else 0)
        legacy_prefix = {
            "pr_review": "pr",
            "issue_analysis": "issue",
            "agent": "agent",
        }[feature]
        entitlement = LegacyEntitlement(
            user_id=1,
            source_key="fixture:legacy",
            snapshot={"version": 2, "concurrency_limit": 1},
            **{f"{legacy_prefix}_remaining": legacy},
        )
        db.add(entitlement)
        if fund:
            await BillingService(db).grant(1, "20", "fixture:fund")
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis", "agent"])
@pytest.mark.parametrize("periodic", [True, False])
async def test_full_cross_feature_capacity_does_not_consume_allowance(
    sql_runtime, monkeypatch, feature, periodic
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, feature, periodic=periodic)
    async with factory() as db:
        await BillingService(db).register_operation(1, "already-running", "repo_scan")
        await db.commit()
    async with factory() as db:
        allowed, reason = await TelegramService(db)._consume_rate_limit(
            "owner1", "owner1/repo", 5, feature, "rejected-capacity"
        )
        assert not allowed and reason == "每用户业务执行上限已达到"
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        prefix = {"pr_review": "", "issue_analysis": "issue_", "agent": "agent_"}[
            feature
        ]
        assert all(
            getattr(user, f"{prefix}{period}_used") == 0
            for period in ("daily", "weekly", "monthly")
        )
        source = (await db.execute(select(LegacyEntitlement))).scalar_one()
        key = {"pr_review": "pr", "issue_analysis": "issue", "agent": "agent"}[feature]
        assert getattr(source, f"{key}_remaining") == 2
        assert (await db.execute(select(RateLimitAdmission))).scalars().all() == []
        assert await db.get(BillingOperation, "rejected-capacity") is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("periodic", [True, False])
async def test_balance_rejection_does_not_consume_any_allowance(
    sql_runtime, monkeypatch, periodic
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, "pr_review", fund=False, periodic=periodic)
    async with factory() as db:
        allowed, reason = await TelegramService(db).check_and_consume_quota(
            "owner1", "owner1/repo", 7, operation_id="insufficient"
        )
        assert not allowed and reason == "Credits 余额不足"
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        assert (user.daily_used, user.weekly_used, user.monthly_used) == (0, 0, 0)
        assert (
            await db.execute(select(LegacyEntitlement))
        ).scalar_one().pr_remaining == 2
        assert await db.get(BillingOperation, "insufficient") is None
        wallet = await db.get(BillingWallet, 1)
        assert wallet is None or wallet.reserved_units == 0


@pytest.mark.asyncio
async def test_quota_rejection_rolls_back_new_operation_and_reservation(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, "pr_review", periodic=False, legacy=0)
    async with factory() as db:
        allowed, reason = await TelegramService(db).check_and_consume_quota(
            "owner1", "owner1/repo", 7, operation_id="quota-rejected"
        )
        assert not allowed and "次数限流" in reason
    async with factory() as db:
        assert await db.get(BillingOperation, "quota-rejected") is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_duplicate_success_commits_one_admission_and_one_reservation(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, "pr_review")
    for _ in range(2):
        async with factory() as db:
            assert (
                await TelegramService(db).check_and_consume_quota(
                    "owner1", "owner1/repo", 7, operation_id="accepted"
                )
            )[0]
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        assert (user.daily_used, user.weekly_used, user.monthly_used) == (1, 1, 1)
        assert len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 1
        assert len((await db.execute(select(QuotaUsageLog))).scalars().all()) == 1
        assert (
            await db.execute(select(LegacyEntitlement))
        ).scalar_one().pr_remaining == 2
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_delayed_commit_keeps_carrier_quota_and_reserve_rollbackable(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, "agent")
    async with factory() as db:
        assert (
            await TelegramService(db).check_and_consume_agent_quota(
                "owner1",
                repo_name="owner1/repo",
                task_id=7,
                operation_id="delayed",
                commit=False,
            )
        )[0]
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        await db.rollback()
    async with factory() as db:
        assert await db.get(BillingOperation, "delayed") is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        user = await db.get(TelegramUser, 1)
        assert (
            user.agent_daily_used,
            user.agent_weekly_used,
            user.agent_monthly_used,
        ) == (0, 0, 0)


@pytest.mark.asyncio
async def test_terminal_operation_never_consumes_a_new_allowance(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    await prepare(factory, monkeypatch, "pr_review")
    async with factory() as db:
        service = BillingService(db)
        await service.register_operation(
            1,
            "terminal",
            "pr_review",
            source={"repo_full_name": "owner1/repo", "pr_number": 7},
        )
        await service.finish_operation("terminal", "completed")
        await db.commit()
    async with factory() as db:
        allowed, reason = await TelegramService(db).check_and_consume_quota(
            "owner1", "owner1/repo", 7, operation_id="terminal"
        )
        assert not allowed and reason == "业务执行已结束"
    async with factory() as db:
        assert (await db.get(TelegramUser, 1)).daily_used == 0
        assert (await db.execute(select(RateLimitAdmission))).scalars().all() == []
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
