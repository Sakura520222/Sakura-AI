"""Billing identity must follow a new queue/admission, never a previous payer."""

from types import SimpleNamespace

import pytest

from backend.services.billing_context import context_for_payload
from tests.test_billing_usage_attribution import sql_runtime as usage_sql_runtime


@pytest.fixture
def sql_runtime(monkeypatch, tmp_path):
    yield from usage_sql_runtime.__wrapped__(monkeypatch, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_payer", "queued_context", "expected_payer", "platform_reason"),
    [
        (11, {"user_id": 22}, 22, None),
        (None, {"user_id": 22}, 22, None),
        (
            11,
            {"user_id": None, "platform_reason": "unbound_auto"},
            None,
            "unbound_auto",
        ),
        (11, None, None, "legacy_incremental_without_verified_payer"),
    ],
)
async def test_incremental_drain_replaces_previous_verified_payer(
    monkeypatch, previous_payer, queued_context, expected_payer, platform_reason
):
    from backend.workers import review_worker

    previous = {
        "action": "full_review",
        "user_id": 11,
        "billing_user_id": previous_payer,
        "billing_platform_reason": "previous_admin_run",
        "repo_full_name": "owner/repo",
        "pr_number": 7,
    }
    original = context_for_payload(previous, "pr_review")
    pending = SimpleNamespace(
        id=101, base_sha="base", head_sha="head", billing_context=queued_context
    )

    class Queue:
        async def list_pending(self, payload):
            return [pending]

    submitted = []

    async def submit(payload):
        submitted.append(context_for_payload(payload, "pr_review"))

    monkeypatch.setattr(
        "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
        Queue,
    )
    monkeypatch.setattr(review_worker, "submit_review_task", submit)
    await review_worker._drain_pending_incremental(previous)
    await review_worker._drain_pending_incremental(previous)
    assert len(submitted) == 2
    assert submitted[0].user_id == expected_payer
    assert submitted[0].platform_reason == platform_reason
    assert submitted[0].operation_id != original.operation_id
    assert submitted[0].operation_id == submitted[1].operation_id
    assert context_for_payload(previous, "pr_review") == original


@pytest.mark.asyncio
async def test_scan_manual_retry_uses_new_operation_and_verified_requester(
    sql_runtime, monkeypatch
):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from backend.api.v1 import scans
    from backend.models.ai_usage_models import AIUsageRecord
    from backend.models.billing_models import BillingOperation
    from backend.models.scan_models import RepoScan
    from backend.models.telegram_models import TelegramUser
    from backend.services.ai_usage_service import ProviderUsageMeter
    from backend.services.billing_context import billable_record
    from tests.test_billing_usage_attribution import complete_usage, fund_and_price

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        admin = await db.get(TelegramUser, 1)
        admin.role = "super_admin"
        db.add(
            RepoScan(
                id=1,
                repo_name="owner2/repo",
                repo_owner="owner2",
                trigger_type="manual",
                triggered_by="api:2",
                status="pending",
            )
        )
        await db.commit()

    class Worker:
        @billable_record("repo_scan")
        async def process_scan(self, scan_id, *, fail=False):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="scan-logical",
            ) as meter:
                meter.usage = complete_usage()
            async with factory() as db:
                record = await db.get(RepoScan, scan_id)
                record.status = "failed" if fail else "completed"
                await db.commit()

    await Worker().process_scan(1, fail=True)
    with Session(engine) as db:
        previous_operation = db.get(RepoScan, 1).billing_operation_id
        assert db.get(BillingOperation, previous_operation).outcome == "failed"

    def hold_dispatch(coroutine, category):
        coroutine.close()
        return SimpleNamespace(add_done_callback=lambda callback: None)

    monkeypatch.setattr(scans, "create_registered_background_task", hold_dispatch)
    async with factory() as db:
        response = await scans.retry_scan(1, db=db, user={"user_id": 1})
        assert response.status_code == 200
    with Session(engine) as db:
        record = db.get(RepoScan, 1)
        assert record.billing_operation_id != previous_operation
        assert record.trigger_type == "manual"
        assert record.triggered_by == "api:1"
    await Worker().process_scan(1)
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        assert len(rows) == 2
        assert rows[0].operation_id == previous_operation
        assert rows[0].user_id == 2
        assert rows[1].operation_id != previous_operation
        assert rows[1].user_id is None
        assert rows[1].platform_reason == "administrator_repo_scan"
        assert db.get(BillingOperation, rows[0].operation_id).outcome == "failed"
        assert db.get(BillingOperation, rows[1].operation_id).outcome == "completed"


@pytest.mark.asyncio
async def test_trusted_scan_resume_keeps_operation_and_cumulative_usage(
    sql_runtime,
):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from backend.models.ai_usage_models import AIUsageRecord
    from backend.models.billing_models import BillingOperation, BillingTransaction
    from backend.models.scan_models import RepoScan
    from backend.services.ai_usage_service import ProviderUsageMeter
    from backend.services.billing_context import billable_record
    from tests.test_billing_usage_attribution import complete_usage, fund_and_price

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        db.add(
            RepoScan(
                id=1,
                repo_name="owner2/repo",
                repo_owner="owner2",
                trigger_type="manual",
                triggered_by="api:2",
                status="pending",
            )
        )
        await db.commit()

    class Worker:
        @billable_record("repo_scan")
        async def process_scan(self, scan_id, *, resume=False):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="resumed-scan",
            ) as meter:
                meter.usage = complete_usage()
            async with factory() as db:
                record = await db.get(RepoScan, scan_id)
                record.status = "completed" if resume else "failed"
                await db.commit()

    await Worker().process_scan(1)
    with Session(engine) as db:
        original_id = db.get(RepoScan, 1).billing_operation_id
        assert db.get(BillingOperation, original_id).outcome == "failed"
    await Worker().process_scan(1, resume=True)
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord)).all()
        assert len(rows) == 2
        assert {row.operation_id for row in rows} == {original_id}
        assert {row.user_id for row in rows} == {2}
        assert len({row.actual_call_id for row in rows}) == 2
        operation = db.get(BillingOperation, original_id)
        assert operation.outcome == "completed"
        debits = db.scalars(
            select(BillingTransaction).where(BillingTransaction.kind == "consumption")
        ).all()
        assert len(debits) == 1
        assert -debits[0].delta_units == operation.settled_units > 0
