"""Shared worker capacity and purchased user admission govern different scopes."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingOperation,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import LegacyEntitlement
from backend.models.service_execution_models import ServiceExecutionLease
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import finish_billing_operation
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingError, BillingService
from tests.test_service_execution_capacity import capacity_db as capacity_fixture

capacity_db = capacity_fixture


@pytest.mark.asyncio
async def test_global_waiting_counts_user_headroom_before_any_ai_request(
    capacity_db, monkeypatch
):
    _, limiter, engine, _ = capacity_db

    async def setting(key, **kwargs):
        return {
            "billing_enabled": False,
            "billing_charge_failed_operations": False,
            "billing_charge_failed_calls": False,
            "billing_reservation_ttl_seconds": 3600,
        }.get(key)

    monkeypatch.setattr("backend.services.billing_service.get_dynamic_config", setting)
    async with limiter.session_factory() as db:
        for user in (1, 2):
            db.add(TelegramUser(id=user, github_username=f"owner{user}"))
        await db.flush()
        for user in (1, 2):
            await BillingService(db).grant(user, "100", f"fund:{user}")
            db.add(
                LegacyEntitlement(
                    user_id=user,
                    source_key=f"plan:{user}",
                    snapshot={
                        "version": 2,
                        "credit_grant": "100",
                        "concurrency_limit": 2,
                    },
                )
            )
        await db.commit()
    first = BillingContext(1, str(uuid4()), "agent", {})
    second = BillingContext(1, str(uuid4()), "agent", {})
    cancellation = asyncio.Event()
    entered = asyncio.Event()

    async def waiting():
        with bind_billing_context(second):
            try:
                async with limiter.slot("agent", cancel_event=cancellation):
                    entered.set()
            except asyncio.CancelledError:
                await finish_billing_operation(
                    "cancelled", session_factory=limiter.session_factory
                )
                raise

    with bind_billing_context(first):
        async with limiter.slot("agent"):
            task = asyncio.create_task(waiting())
            for _ in range(100):
                with Session(engine) as db:
                    if db.get(BillingOperation, second.operation_id):
                        break
                await asyncio.sleep(0.005)
            with Session(engine) as db:
                queued = db.get(BillingOperation, second.operation_id)
                assert queued is not None and queued.outcome is None
                assert queued.status == "not_started"
                assert len(db.scalars(select(ServiceExecutionLease)).all()) == 1
            assert not entered.is_set()
            with bind_billing_context(
                BillingContext(1, str(uuid4()), "issue_analysis", {})
            ):
                with pytest.raises(BillingError) as error:
                    async with limiter.slot("issue_analysis"):
                        pytest.fail("The user already occupies both admission places")
                assert error.value.code == "concurrency_limit"
            # The shared Agent lane is full, but another user's PR has an
            # independent service lane and independent purchased user headroom.
            with bind_billing_context(BillingContext(2, str(uuid4()), "pr_review", {})):
                async with limiter.slot("pr_review"):
                    await finish_billing_operation(
                        "completed", session_factory=limiter.session_factory
                    )
            cancellation.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            with Session(engine) as db:
                assert (
                    db.get(BillingOperation, second.operation_id).outcome == "cancelled"
                )
                assert not db.scalars(select(AIUsageRecord)).all()
                assert not db.scalars(
                    select(BillingTransaction).where(
                        BillingTransaction.kind == "consumption"
                    )
                ).all()
                assert db.get(BillingWallet, 1).balance_units == 100_000_000
        await finish_billing_operation(
            "completed", session_factory=limiter.session_factory
        )
    with Session(engine) as db:
        assert not db.scalars(select(ServiceExecutionLease)).all()
        assert all(
            row.outcome is not None for row in db.scalars(select(BillingOperation))
        )
