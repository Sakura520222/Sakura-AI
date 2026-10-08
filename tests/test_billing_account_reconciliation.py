"""Reviewed recovery cannot change the configured account behind a request."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.ai_protocol.models import ProtocolFamily
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingReconciliationEvent,
    BillingUsageCharge,
    BillingWallet,
)
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    finish_billing_operation,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_account_usage import account_prices
from tests.test_billing_usage_attribution import complete_usage
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_unified_client_fallback import _candidate

sql_runtime = runtime_fixture


async def unknown_call(factory, account_id):
    candidate = _candidate(
        ProtocolFamily.OPENAI_COMPATIBLE, "shared-model", account_id=account_id
    )
    await account_prices(factory, candidate)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        await db.commit()
    context = BillingContext(1, str(uuid4()), "pr_review", {"review_id": 8})
    with bind_billing_context(context):
        async with ProviderUsageMeter.for_candidate(
            candidate, call_kind="chat", role="main", logical_call_id="missing-result"
        ) as meter:
            # The actual upstream response omitted Usage; a provider invoice is
            # required before this request can receive an exact settled bill.
            pass
        await finish_billing_operation("completed")
    return context, meter.call_id, candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_account", ["account-a", None])
async def test_reviewed_recovery_rejects_another_account_price_before_appending(
    sql_runtime, requested_account
):
    factory, engine, _ = sql_runtime
    context, call_id, candidate = await unknown_call(factory, requested_account)
    async with factory() as db:
        profile = await BillingService(db)._price(
            candidate.provider.id,
            candidate.model.model_id,
            "chat",
            account_id="account-b",
        )
        with pytest.raises(BillingError, match="exact matching"):
            await resolve_billing_call(
                db,
                call_id=call_id,
                event_key="wrong-account",
                actor_id=1,
                reason="Reviewed provider invoice",
                usage=complete_usage(),
                price_profile_id=profile.id,
            )
        await db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count(AIUsageRecord.id))) == 1
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 0
        assert (
            db.get(BillingOperation, context.operation_id).status == "pending_pricing"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_account", ["account-a", None])
async def test_reviewed_recovery_appends_same_account_and_replays_only_once(
    sql_runtime, requested_account
):
    factory, engine, _ = sql_runtime
    context, call_id, _ = await unknown_call(factory, requested_account)
    evidence = {
        "call_id": call_id,
        "event_key": "invoice-confirmation",
        "actor_id": 1,
        "reason": "Reviewed provider invoice with exact usage",
        "usage": complete_usage(),
    }
    async with factory() as db:
        first = await resolve_billing_call(db, **evidence)
        first_id = first.id
        await db.commit()
    async with factory() as db:
        repeated = await resolve_billing_call(db, **evidence)
        assert repeated.id == first_id
        await db.commit()
    with Session(engine) as db:
        records = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert len(records) == 2
        assert records[0].account_id == records[1].account_id == requested_account
        assert records[0].usage_reported is False
        assert records[1].usage_reported is True
        assert records[0].actual_call_id == call_id
        assert records[1].actual_call_id is None
        assert db.get(BillingOperation, context.operation_id).status == "settled"
        assert db.get(BillingCallAttempt, call_id).account_id == requested_account
        assert db.scalar(select(func.count(BillingReconciliationEvent.id))) == 1
        assert db.scalar(select(func.count(BillingUsageCharge.id))) == 1
        assert db.get(BillingWallet, 1).balance_units == 99_844_000
