"""Fallback cost uncertainty only gates continuation under frozen user policy."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingOperation,
    BillingTransaction,
)
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    finish_billing_operation,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_usage_attribution import complete_usage, fund_and_price
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["agent", "pr_review"])
@pytest.mark.parametrize("charge_failed", [False, True])
async def test_failed_unknown_attempt_and_successful_fallback_respect_frozen_policy(
    sql_runtime, feature, charge_failed
):
    factory, _, policy = sql_runtime
    policy["billing_charge_failed_calls"] = charge_failed
    await fund_and_price(factory)
    context = BillingContext(
        1, str(uuid4()), feature, {"repo_full_name": "owner1/repo"}
    )
    with bind_billing_context(context):
        with pytest.raises(TimeoutError):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                call_kind="chat",
                protocol_family="openai_compatible",
                logical_call_id="logical",
                role="main",
            ):
                raise TimeoutError("TEST upstream response unknown")
        if charge_failed:
            with pytest.raises(
                BillingError, match="Previous request needs reconciliation"
            ):
                async with ProviderUsageMeter(
                    provider_id="provider",
                    model_id="model",
                    call_kind="chat",
                    protocol_family="openai_compatible",
                    logical_call_id="logical",
                    role="fallback",
                ):
                    pytest.fail(
                        "An unresolved chargeable request must block the next send"
                    )
        else:
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                call_kind="chat",
                protocol_family="openai_compatible",
                logical_call_id="logical",
                role="fallback",
            ) as meter:
                meter.usage = complete_usage()
        if charge_failed:
            with pytest.raises(
                BillingError, match="Previous request needs reconciliation"
            ):
                await finish_billing_operation("completed")
        else:
            await finish_billing_operation("completed")
    policy["billing_charge_failed_calls"] = not charge_failed
    async with factory() as db:
        operation = await db.get(BillingOperation, context.operation_id)
        settled = operation.settled_units
        assert operation.charge_failed_calls is charge_failed
        failed = (
            await db.execute(
                select(AIUsageRecord).where(AIUsageRecord.outcome == "failed")
            )
        ).scalar_one()
        assert not failed.usage_reported and failed.input_tokens is None
        attempt = await db.get(BillingCallAttempt, failed.actual_call_id)
        assert attempt.state == "pending_reconciliation"
        service = BillingService(db)
        if charge_failed:
            assert settled == 0 and operation.pending_reason
            with pytest.raises(BillingError, match="reconciliation"):
                await service.resume_operation(context.operation_id)
        else:
            assert settled > 0
            assert operation.pending_reason == "pending_reconciliation"
            resumed = await service.resume_operation(context.operation_id)
            assert (
                resumed.operation_id == context.operation_id and resumed.outcome is None
            )
            await service.finish_operation(context.operation_id, "completed")
            assert (
                await db.get(BillingOperation, context.operation_id)
            ).settled_units == settled
            charges = (
                (
                    await db.execute(
                        select(BillingTransaction).where(
                            BillingTransaction.kind == "consumption"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(charges) == 1
            assert (await service.reconcile_wallet(1))["consistent"]
        await db.commit()


@pytest.mark.asyncio
async def test_started_attempt_without_a_failed_receipt_still_blocks_continuation(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        service = BillingService(db)
        await service.register_operation(1, "TEST-unknown-send", "agent")
        await service.start_call(
            "TEST-unknown-send", "TEST-unconfirmed", "provider", "model", "chat"
        )
        await service.finish_operation("TEST-unknown-send", "completed")
        with pytest.raises(BillingError, match="reconciliation"):
            await service.resume_operation("TEST-unknown-send")
