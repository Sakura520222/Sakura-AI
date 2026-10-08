"""User admission happens before shared-capacity waiting, never after AI send."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.agent_team_models import AgentTeamTask
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingOperation,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import LegacyEntitlement
from backend.services import ai_usage_service as usage
from backend.services.billing_context import (
    BillingContext,
    billable_record,
    bind_billing_context,
)
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


async def prepare(factory, limit):
    async with factory() as db:
        service = BillingService(db)
        for user in (1, 2):
            await service.grant(user, "100", f"fund:{user}")
            db.add(
                LegacyEntitlement(
                    user_id=user,
                    source_key=f"plan:{user}",
                    snapshot={
                        "version": 2,
                        "credit_grant": "100",
                        "concurrency_limit": limit,
                    },
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_queue_admission_persists_without_call_or_consumption(sql_runtime):
    factory, _, _ = sql_runtime
    await prepare(factory, 3)
    context = BillingContext(1, str(uuid4()), "agent", {"agent_task_id": 4})
    with bind_billing_context(context):
        await usage.admit_billing_operation()
        await usage.admit_billing_operation()
        async with factory() as db:
            operation = (await db.execute(select(BillingOperation))).scalar_one()
            assert operation.operation_id == context.operation_id
            assert operation.user_id == 1 and operation.outcome is None
            assert operation.status == "not_started"
            assert (await db.execute(select(AIUsageRecord))).scalars().all() == []
            assert (
                await db.execute(
                    select(BillingTransaction).where(
                        BillingTransaction.kind == "consumption"
                    )
                )
            ).scalars().all() == []
        await usage.finish_billing_operation("cancelled")
    async with factory() as db:
        assert (
            await db.get(BillingOperation, context.operation_id)
        ).outcome == "cancelled"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_queued_features_share_user_limit_and_other_users_do_not(sql_runtime):
    factory, _, _ = sql_runtime
    await prepare(factory, 2)
    for feature in ("agent", "pr_review"):
        with bind_billing_context(BillingContext(1, str(uuid4()), feature, {})):
            await usage.admit_billing_operation()
    with bind_billing_context(BillingContext(1, str(uuid4()), "issue_analysis", {})):
        with pytest.raises(BillingError) as error:
            await usage.admit_billing_operation()
        assert error.value.code == "concurrency_limit"
    with bind_billing_context(BillingContext(2, str(uuid4()), "issue_analysis", {})):
        await usage.admit_billing_operation()
    async with factory() as db:
        rows = (await db.execute(select(BillingOperation))).scalars().all()
        assert len(rows) == 3
        assert sum(row.user_id == 1 for row in rows) == 2


@pytest.mark.asyncio
async def test_absent_context_does_not_invent_billing_owner(sql_runtime):
    factory, _, _ = sql_runtime
    with bind_billing_context(None):
        assert await usage.admit_billing_operation() is False
    async with factory() as db:
        assert (await db.execute(select(BillingOperation))).scalars().all() == []


@pytest.mark.asyncio
async def test_resume_rechecks_capacity_but_keeps_same_execution(sql_runtime):
    factory, _, _ = sql_runtime
    await prepare(factory, 1)
    async with factory() as db:
        service = BillingService(db)
        original = await service.register_operation(1, "original", "agent")
        await service.finish_operation("original", "completed")
        await service.register_operation(1, "other", "pr_review")
        await db.commit()
        with pytest.raises(BillingError) as error:
            await service.resume_operation("original")
        assert error.value.code == "concurrency_limit"
        assert original.outcome == "completed"
        await service.finish_operation("other", "cancelled")
        await service.resume_operation("original")
        assert original.outcome is None and original.operation_id == "original"
        await db.commit()


@pytest.mark.asyncio
async def test_scan_resume_does_not_reapply_daily_limit(sql_runtime):
    factory, _, _ = sql_runtime
    await prepare(factory, 1)
    async with factory() as db:
        source = (
            await db.execute(
                select(LegacyEntitlement).where(LegacyEntitlement.user_id == 1)
            )
        ).scalar_one()
        source.rate_limits = {"repo_scan_daily": 1}
        service = BillingService(db)
        await service.register_operation(1, "scan", "repo_scan")
        await service.finish_operation("scan", "completed")
        await service.resume_operation("scan")
        assert (await db.get(BillingOperation, "scan")).outcome is None
        await db.commit()


@pytest.mark.asyncio
async def test_rejected_agent_resume_leaves_retryable_task_instead_of_queued_forever(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    await prepare(factory, 1)
    async with factory() as db:
        service = BillingService(db)
        await service.register_operation(1, "finished-agent", "agent")
        await service.finish_operation("finished-agent", "completed")
        await service.register_operation(1, "active-pr", "pr_review")
        task = AgentTeamTask(
            title="Resume",
            repo_owner="owner",
            repo_name="project",
            repo_full_name="owner/project",
            source_type="manual_issue",
            started_by="owner1",
            status="pending",
            billing_operation_id="finished-agent",
            billing_user_id=1,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    class Worker:
        entered = False

        @billable_record("agent")
        async def process_task(self, record_id, *, resume=False):
            self.entered = True

    worker = Worker()
    with pytest.raises(BillingError) as error:
        await worker.process_task(task_id, resume=True)
    assert error.value.code == "concurrency_limit"
    assert not worker.entered
    async with factory() as db:
        saved = await db.get(AgentTeamTask, task_id)
        assert saved.status == "failed"
        assert saved.failed_phase == "billing_admission"
        assert (await db.get(BillingOperation, "finished-agent")).outcome == "completed"
