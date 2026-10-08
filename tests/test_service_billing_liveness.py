"""Recovery respects live workers and the gap before financial finalization."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models import database
from backend.models.billing_models import BillingOperation
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import (
    admit_billing_operation,
    finish_billing_operation,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingService
from backend.services.service_execution_capacity import (
    has_live_service_execution_ownership,
)
from tests.test_service_execution_capacity import capacity_db as capacity_fixture

capacity_db = capacity_fixture


@pytest.fixture
def billing_capacity(capacity_db, monkeypatch):
    _, limiter, engine, _ = capacity_db
    with Session(engine) as db:
        db.add(TelegramUser(id=1, github_username="owner"))
        db.commit()

    async def setting(key, **kwargs):
        return {
            "billing_enabled": False,
            "billing_charge_failed_operations": False,
            "billing_charge_failed_calls": False,
            "billing_initial_reserve_credits": "0",
            "billing_reservation_ttl_seconds": 3600,
        }.get(key)

    monkeypatch.setattr("backend.services.billing_service.get_dynamic_config", setting)
    monkeypatch.setattr(database, "async_session", limiter.session_factory)
    return limiter, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_recovery_does_not_fail_expired_billing_with_live_owner(
    billing_capacity, running
):
    limiter, engine = billing_capacity
    context = BillingContext(1, str(uuid4()), "agent", {})
    with bind_billing_context(context):
        await admit_billing_operation()
        ownership = await limiter.acquire_ownership("agent")
        if running:
            assert await limiter.renew_ownership(ownership, running=True)
        with Session(engine) as db:
            db.get(BillingOperation, context.operation_id).expires_at = (
                now_utc() - timedelta(days=1)
            )
            db.commit()
        async with limiter.session_factory() as db:
            service = BillingService(db)
            assert await service.recover_operations(dry_run=True) == []
            assert await service.recover_operations(dry_run=False) == []
            await db.commit()
        with Session(engine) as db:
            assert db.get(BillingOperation, context.operation_id).outcome is None
        await limiter.release_ownership(ownership)
        async with limiter.session_factory() as db:
            report = await BillingService(db).recover_operations(dry_run=False)
            assert [item["operation_id"] for item in report] == [context.operation_id]
            await db.commit()
        with Session(engine) as db:
            assert db.get(BillingOperation, context.operation_id).outcome == "failed"


@pytest.mark.asyncio
async def test_recovery_live_owners_do_not_starve_batch_of_abandoned_work(
    billing_capacity,
):
    limiter, engine = billing_capacity
    live = BillingContext(1, "a-live", "agent", {})
    with bind_billing_context(live):
        await admit_billing_operation()
        ownership = await limiter.acquire_ownership("agent")
    with bind_billing_context(BillingContext(1, "z-abandoned", "pr_review", {})):
        await admit_billing_operation()
    with Session(engine) as db:
        for row in db.scalars(select(BillingOperation)):
            row.expires_at = now_utc() - timedelta(days=1)
        db.commit()
    async with limiter.session_factory() as db:
        report = await BillingService(db).recover_operations(limit=1, dry_run=False)
        assert [item["operation_id"] for item in report] == ["z-abandoned"]
        await db.commit()
    await limiter.release_ownership(ownership)


@pytest.mark.asyncio
async def test_service_exit_renews_billing_until_outer_finalization(billing_capacity):
    limiter, engine = billing_capacity
    context = BillingContext(1, str(uuid4()), "agent", {})
    with bind_billing_context(context):
        async with limiter.slot("agent"):
            with Session(engine) as db:
                db.get(BillingOperation, context.operation_id).expires_at = (
                    now_utc() - timedelta(days=1)
                )
                db.commit()
        async with limiter.session_factory() as db:
            assert await BillingService(db).recover_operations(dry_run=False) == []
            await db.commit()
        with Session(engine) as db:
            row = db.get(BillingOperation, context.operation_id)
            assert row.outcome is None and row.expires_at > now_utc()
        await finish_billing_operation("completed")
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome == "completed"


@pytest.mark.asyncio
async def test_resuming_old_execution_renews_admission_before_ownership(
    billing_capacity,
):
    limiter, _ = billing_capacity
    async with limiter.session_factory() as db:
        service = BillingService(db)
        await service.register_operation(1, "resume", "agent")
        await service.finish_operation("resume", "completed")
        row = await db.get(BillingOperation, "resume")
        row.expires_at = now_utc() - timedelta(days=1)
        await db.commit()
        await service.resume_operation("resume")
        assert row.outcome is None and row.expires_at > now_utc()
        await db.commit()
        assert await service.recover_operations(dry_run=False) == []


@pytest.mark.asyncio
async def test_native_owner_recovery_uses_current_read_after_old_snapshot(
    billing_capacity,
):
    limiter, _ = billing_capacity
    statements = []

    class CurrentReadRecorder:
        async def execute(self, statement):
            statements.append(str(statement.compile(dialect=mysql.dialect())))
            async with limiter.session_factory() as db:
                return await db.execute(statement)

    assert not await has_live_service_execution_ownership(
        CurrentReadRecorder(), "missing", for_update=True
    )
    assert "FOR UPDATE" in statements[0]
