"""A stale missing-row read is followed by a real unique-key/savepoint conflict."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.models.billing_models import BillingTransaction, BillingWallet
from backend.models.payment_models import Order, Plan
from backend.services.billing_service import BillingService
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [None, "user", "plan", "actor"])
async def test_concurrent_admin_grant_replay_recovers_unique_winner(
    sql_runtime, monkeypatch, conflict
):
    factory, _, _ = sql_runtime
    async with factory() as db:
        plan = Plan(
            name="TEST concurrent grant",
            plan_type="one_time",
            price_cents=100,
            credit_grant=Decimal(5),
        )
        other = Plan(
            name="TEST other plan",
            plan_type="one_time",
            price_cents=200,
            credit_grant=Decimal(9),
        )
        db.add(plan)
        db.add(other)
        await db.commit()
        winner = await PaymentService(db).grant_plan_to_user(
            1, plan.id, operator_id=1, idempotency_key="TEST-raced-grant"
        )
        await db.commit()
        ids = (winner.id, plan.id, other.id)
    async with factory() as db:
        execute = db.execute
        raced = False

        async def stale_first_read(statement, *args, **kwargs):
            nonlocal raced
            if not raced and "payment_orders.grant_idempotency_key =" in str(statement):
                raced = True
                # Emulate both workers observing absence before the winning
                # transaction commits. The insert still hits the real DB unique key.
                return await execute(select(Order).where(Order.id == -1))
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", stale_first_read)
        service = PaymentService(db)
        kwargs = {
            "user_id": 2 if conflict == "user" else 1,
            "plan_id": ids[2] if conflict == "plan" else ids[1],
            "operator_id": 2 if conflict == "actor" else 1,
            "idempotency_key": "TEST-raced-grant",
        }
        if conflict:
            with pytest.raises(PaymentError) as rejected:
                await service.grant_plan_to_user(**kwargs)
            assert rejected.value.code == "idempotency_conflict"
        else:
            replay = await service.grant_plan_to_user(**kwargs)
            assert replay.id == ids[0] and replay.status == "fulfilled"
        assert raced
        # The outer unit of work remains usable after rolling back only the loser.
        assert (await db.execute(select(Order.id))).scalars().all() == [ids[0]]
        assert (await db.get(BillingWallet, 1)).balance_units == 5_000_000
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]
        await db.commit()
