"""Frozen non-charging executions retain cost evidence without blocking work."""

import pytest
from sqlalchemy import select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingCallAttempt, BillingTransaction
from backend.models.legacy_entitlement_models import LegacyEntitlement
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_settlement import POLICY, add_call, prepare
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_billing_wallet import db as wallet_db

db = wallet_db
sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["agent", "repo_scan"])
@pytest.mark.parametrize("reported", [True, False])
async def test_disabled_operation_resumes_without_prices_and_keeps_original_evidence(
    db, feature, reported
):
    service = BillingService(db, policy={**POLICY, "billing_enabled": False})
    await service.grant(1, "10", "test:fund")
    operation = await service.register_operation(1, "op-1", feature)
    await add_call(db, service, "first", reported=reported)
    await service.finish_operation("op-1", "completed")
    pending = operation.pending_reason
    assert pending == ("missing_price" if reported else "pending_reconciliation")
    service.policy["billing_enabled"] = True
    resumed = await service.resume_operation("op-1")
    assert resumed.operation_id == "op-1" and resumed.outcome is None
    assert resumed.charging_enabled is False
    assert resumed.pending_reason == pending
    await add_call(db, service, "second", tokens=100)
    await service.finish_operation("op-1", "completed")
    records = (await db.execute(select(AIUsageRecord))).scalars().all()
    assert len(records) == 2
    assert records[0].input_tokens == (1 if reported else None)
    attempts = (await db.execute(select(BillingCallAttempt))).scalars().all()
    assert len(attempts) == 2 and all(a.price_profile_id is None for a in attempts)
    assert (await service.get_wallet(1)).balance_units == 10_000_000
    assert (await service.get_wallet(1)).reserved_units == 0
    assert (
        await db.execute(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        )
    ).scalars().all() == []
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_disabled_resume_still_checks_user_concurrency(db):
    service = BillingService(db, policy={**POLICY, "billing_enabled": False})
    db.add(
        LegacyEntitlement(
            user_id=1,
            source_key="test:limit",
            snapshot={"version": 2, "concurrency_limit": 1},
        )
    )
    await db.flush()
    await service.register_operation(1, "op-1", "repo_scan")
    await add_call(db, service, "unpriced")
    await service.finish_operation("op-1", "completed")
    await service.register_operation(1, "other", "agent")
    with pytest.raises(BillingError) as rejected:
        await service.resume_operation("op-1")
    assert rejected.value.code == "concurrency_limit"
    assert (await service._operation("op-1")).outcome == "completed"


@pytest.mark.asyncio
async def test_disabling_global_charging_does_not_unlock_frozen_paid_unknown_usage(db):
    service = await prepare(db)
    await add_call(db, service, "unknown", reported=False)
    await service.finish_operation("op-1", "completed")
    service.policy = {**POLICY, "billing_enabled": False}
    with pytest.raises(BillingError, match="reconciliation"):
        await service.resume_operation("op-1")
    assert (await service._operation("op-1")).charging_enabled is True


@pytest.mark.asyncio
async def test_platform_operation_resumes_without_user_payable_prices(db):
    service = BillingService(db, policy=POLICY)
    await service.register_operation(
        None, "platform", "agent", platform_reason="test:system"
    )
    attempt = await service.start_call("platform", "platform-call", "p1", "m1", "chat")
    db.add(
        AIUsageRecord(
            record_key="platform-usage",
            actual_call_id="platform-call",
            operation_id="platform",
            feature="agent",
            call_kind="chat",
            provider_id="p1",
            model_id="m1",
            role="main",
            protocol_family="openai_compatible",
            input_tokens=100,
            output_tokens=0,
            usage_reported=True,
            outcome="completed",
            platform_reason="test:system",
        )
    )
    attempt.state = "usage_known"
    attempt.usage_record_key = "platform-usage"
    await db.flush()
    await service.finish_operation("platform", "completed")
    assert (
        await service.resume_operation("platform")
    ).pending_reason == "missing_price"


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["agent", "repo_scan"])
async def test_persisted_worker_continuation_retains_disabled_operation(
    sql_runtime, feature
):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.billing_models import BillingOperation
    from backend.models.scan_models import RepoScan
    from backend.services.ai_usage_service import ProviderUsageMeter
    from backend.services.billing_context import billable_record
    from tests.test_billing_usage_attribution import complete_usage

    factory, _, policy = sql_runtime
    policy["billing_enabled"] = False
    model = AgentTeamTask if feature == "agent" else RepoScan
    async with factory() as session:
        await BillingService(session).grant(2, "10", "test:worker-fund")
        record = (
            AgentTeamTask(
                id=7,
                title="TEST disabled billing",
                repo_full_name="owner2/repo",
                repo_owner="owner2",
                repo_name="repo",
                source_type="manual_issue",
                started_by="owner2",
                billing_user_id=2,
                status="queued",
            )
            if feature == "agent"
            else RepoScan(
                id=7,
                repo_name="owner2/repo",
                repo_owner="owner2",
                trigger_type="manual",
                triggered_by="api:2",
                status="pending",
            )
        )
        session.add(record)
        await session.commit()

    class Worker:
        async def call(self, record_id):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="unpriced",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="worker-continuation",
            ) as meter:
                meter.usage = complete_usage()
            async with factory() as session:
                record = await session.get(model, record_id)
                record.status = "completed"
                await session.commit()

        @billable_record("agent")
        async def process_task(self, record_id):
            await self.call(record_id)

        @billable_record("agent")
        async def process_external_review_iteration(self, record_id):
            await self.call(record_id)

        @billable_record("repo_scan")
        async def process_scan(self, record_id, *, resume=False):
            await self.call(record_id)

    worker = Worker()
    if feature == "agent":
        await worker.process_task(7)
        policy["billing_enabled"] = True
        await worker.process_external_review_iteration(7)
    else:
        await worker.process_scan(7)
        policy["billing_enabled"] = True
        await worker.process_scan(7, resume=True)
    async with factory() as session:
        record = await session.get(model, 7)
        operation = await session.get(BillingOperation, record.billing_operation_id)
        assert operation.outcome == "completed" and operation.charging_enabled is False
        assert operation.pending_reason == "missing_price"
        rows = (await session.execute(select(AIUsageRecord))).scalars().all()
        assert len(rows) == 2 and {r.operation_id for r in rows} == {
            operation.operation_id
        }
        assert {r.user_id for r in rows} == {2}
        assert len({r.actual_call_id for r in rows}) == 2
        wallet = await BillingService(session).get_wallet(2)
        assert wallet.balance_units == 10_000_000 and wallet.reserved_units == 0
