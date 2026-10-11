"""Queued increments keep their admitted payer, financial identity and outcome."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.database import PRReview, PRReviewIncrementalQueue
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    admit_billing_operation,
    finish_billing_operation,
)
from backend.services.billing_context import (
    BillingContext,
    billable_payload,
    bind_billing_context,
    context_for_payload,
)
from backend.services.billing_service import BillingService
from backend.services.pr_review_incremental_queue import PRReviewIncrementalQueueService
from tests.test_billing_usage_attribution import (
    complete_usage,
    fund_and_price,
)
from tests.test_billing_usage_attribution import (
    sql_runtime as runtime_fixture,
)

sql_runtime = runtime_fixture


def payload(context, *, base="base", head="head"):
    return {
        "repo_owner": "owner1",
        "repo_name": "repo",
        "repo_full_name": "owner1/repo",
        "pr_number": 7,
        "pr_id": 1,
        "user_id": context.user_id,
        "billing_user_id": context.user_id,
        "billing_context": context.to_payload(),
        "action": "synchronize",
        "before": base,
        "after": head,
        "head_sha": head,
    }


async def add_increment(factory, operation_id, user_id=1, *, head="head", base="base"):
    context = BillingContext(
        user_id,
        operation_id,
        "pr_review",
        {"repo_full_name": "owner1/repo", "pr_number": 7},
    )
    async with factory() as db:
        await BillingService(db).register_operation(
            context.user_id,
            context.operation_id,
            context.feature,
            source=dict(context.source),
        )
        item = PRReviewIncrementalQueue(
            repo_owner="owner1",
            repo_name="repo",
            repo_full_name="owner1/repo",
            pr_number=7,
            head_sha=head,
            base_sha=base,
            delivery_id=f"delivery:{operation_id}",
            billing_context=context.to_payload(),
            status="pending",
        )
        db.add(item)
        await db.commit()
        return context, item.id


@pytest.mark.asyncio
async def test_active_review_never_ingests_another_admitted_operation(sql_runtime):
    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    original = BillingContext(1, "active-A", "pr_review", {})
    await add_increment(factory, "queued-B", 2)
    prepared = await PRReviewIncrementalQueueService().prepare_pending_for_review(
        pr_info=payload(original),
        repo=SimpleNamespace(compare=lambda *_: SimpleNamespace(files=[], commits=[])),
    )
    assert prepared is None


@pytest.mark.asyncio
async def test_drain_reuses_first_queued_operation_and_target_head(
    sql_runtime, monkeypatch
):
    from backend.workers import review_worker

    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    first, _ = await add_increment(factory, "queued-B", 2, head="B")
    await add_increment(factory, "queued-C", 1, head="C", base="B")
    original = payload(BillingContext(1, "active-A", "pr_review", {}))
    submitted = []

    async def capture(value):
        submitted.append(value)

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    await review_worker._drain_pending_incremental(original)
    await review_worker._drain_pending_incremental(original)
    assert len(submitted) == 1
    assert context_for_payload(submitted[0], "pr_review") == first
    assert submitted[0]["head_sha"] == submitted[0]["after"] == "B"
    assert submitted[0]["before"] == "base"


@pytest.mark.asyncio
async def test_closing_pr_settles_unused_queued_operations_once(sql_runtime):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    await add_increment(factory, "queued-B", 1)
    await add_increment(factory, "queued-C", 2)
    service = PRReviewIncrementalQueueService()
    assert await service.cancel_pending_for_pr("owner1/repo", 7) == 2
    assert await service.cancel_pending_for_pr("owner1/repo", 7) == 0
    with Session(engine) as db:
        assert {row.outcome for row in db.scalars(select(BillingOperation))} == {
            "cancelled"
        }
        assert all(row.reserved_units == 0 for row in db.scalars(select(BillingWallet)))
    async with factory() as db:
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_sequential_runner_finishes_all_queued_payers(sql_runtime, monkeypatch):
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory, kinds=("chat", "context_compression"))
    contexts = [
        await add_increment(factory, "queued-B", 2, head="B"),
        await add_increment(factory, "queued-C", 1, head="C", base="B"),
    ]
    submitted = []

    async def capture(value):
        submitted.append(value)

    class Worker:
        @billable_payload("pr_review")
        async def process_review_task(self, value, *, deadline):
            await admit_billing_operation()
            for kind in ("chat", "context_compression"):
                async with ProviderUsageMeter(
                    provider_id="provider",
                    model_id="model",
                    protocol_family="openai_compatible",
                    call_kind=kind,
                    role="main",
                    logical_call_id=f"{value['billing_context']['operation_id']}:{kind}",
                ) as meter:
                    meter.usage = complete_usage()
            await finish_billing_operation("completed")
            return "done"

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    monkeypatch.setattr(
        review_worker, "register_background_task", lambda task, kind: task
    )
    await review_worker._drain_pending_incremental(
        payload(BillingContext(1, "active-A", "pr_review", {}))
    )
    await review_worker._run_review_task_with_timeout(
        Worker(), submitted.pop(0), "owner1/repo#7"
    )
    assert len(submitted) == 1
    await review_worker._run_review_task_with_timeout(
        Worker(), submitted.pop(0), "owner1/repo#7"
    )
    assert submitted == []
    with Session(engine) as db:
        rows = db.scalars(select(AIUsageRecord)).all()
        assert len(rows) == 4
        assert {(row.operation_id, row.user_id) for row in rows} == {
            ("queued-B", 2),
            ("queued-C", 1),
        }
        for context, queue_id in contexts:
            assert db.get(BillingOperation, context.operation_id).outcome == "completed"
            assert db.get(PRReviewIncrementalQueue, queue_id).status == "consumed"
        assert all(row.reserved_units == 0 for row in db.scalars(select(BillingWallet)))


@pytest.mark.asyncio
async def test_same_head_distinct_delivery_keeps_distinct_admitted_operation(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        db.add(
            PRReview(
                pr_id=1,
                pr_number=7,
                repo_owner="owner1",
                repo_name="repo",
                author="owner1",
                title="PR",
                branch="feature",
                strategy="standard",
                status="reviewing",
            )
        )
        for operation_id in ("first", "second"):
            await BillingService(db).register_operation(1, operation_id, "pr_review")
        await db.commit()
    service = PRReviewIncrementalQueueService()
    one = await service.enqueue_from_webhook(
        payload(BillingContext(1, "first", "pr_review", {})), "delivery-one"
    )
    two = await service.enqueue_from_webhook(
        payload(BillingContext(1, "second", "pr_review", {})), "delivery-two"
    )
    assert one.id != two.id
    with Session(engine) as db:
        assert {
            row.billing_context["operation_id"]
            for row in db.scalars(select(PRReviewIncrementalQueue))
        } == {"first", "second"}


@pytest.mark.asyncio
async def test_dispatch_replay_enters_worker_once(sql_runtime, monkeypatch):

    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    value = payload(context)
    value["incremental_queue_ids"] = [queue_id]
    service = PRReviewIncrementalQueueService()
    assert await service.claim_dispatch(value)
    assert await service.start_dispatch(value)
    assert not await service.start_dispatch(value)


@pytest.mark.asyncio
async def test_financial_recovery_terminal_head_does_not_starve_next_increment(
    sql_runtime, monkeypatch
):
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    _, first_id = await add_increment(factory, "queued-B", 2)
    next_context, _ = await add_increment(factory, "queued-C", 1, head="C")
    async with factory() as db:
        await BillingService(db).finish_operation("queued-B", "failed")
        await db.commit()
    submitted = []

    async def capture(value):
        submitted.append(value)

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    await review_worker._drain_pending_incremental(
        payload(BillingContext(1, "active-A", "pr_review", {}))
    )
    assert len(submitted) == 1
    assert context_for_payload(submitted[0], "pr_review") == next_context
    with Session(engine) as db:
        assert db.get(PRReviewIncrementalQueue, first_id).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected", [("dispatching", "ready"), ("running", "pending_reconciliation")]
)
async def test_recovery_only_requeues_known_unstarted_handoffs(
    sql_runtime, state, expected
):
    from backend.core.time_service import now_utc
    from backend.models.admin_action_log import AdminActionLog
    from backend.models.telegram_models import TelegramUser
    from backend.services.pr_review_incremental_recovery import (
        recover_increment_dispatch,
    )

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        item = await db.get(PRReviewIncrementalQueue, queue_id)
        item.status = state
        item.dispatch_token = "old-token"
        item.dispatch_expires_at = now_utc() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        report = await recover_increment_dispatch(db, queue_id, dry_run=True)
        assert report["status"] == expected
        await db.commit()
    with Session(engine) as db:
        assert db.get(PRReviewIncrementalQueue, queue_id).dispatch_token == "old-token"
        assert list(db.scalars(select(AdminActionLog))) == []
    async with factory() as db:
        report = await recover_increment_dispatch(
            db,
            queue_id,
            dry_run=False,
            actor_id=1,
            evidence="Verified old process stopped before worker entry",
            reason="restore queued delivery",
        )
        assert report["status"] == expected
        await db.commit()
    with Session(engine) as db:
        item = db.get(PRReviewIncrementalQueue, queue_id)
        assert item.billing_context == context.to_payload()
        assert db.get(BillingOperation, context.operation_id).outcome is None
        if state == "dispatching":
            assert item.status == "pending" and item.dispatch_token is None
            assert (
                db.scalar(select(AdminActionLog.action))
                == "billing.incremental_recovery"
            )
        else:
            assert item.status == "running" and item.dispatch_token == "old-token"


@pytest.mark.asyncio
async def test_old_dispatch_token_cannot_finalize_recovered_operation(
    sql_runtime, monkeypatch
):
    from backend.core.time_service import now_utc
    from backend.models.telegram_models import TelegramUser
    from backend.services.pr_review_incremental_recovery import (
        recover_increment_dispatch,
    )
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    old = payload(context)
    old["incremental_queue_ids"] = [queue_id]
    service = PRReviewIncrementalQueueService()
    assert await service.claim_dispatch(old)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        (await db.get(PRReviewIncrementalQueue, queue_id)).dispatch_expires_at = (
            now_utc() - timedelta(seconds=1)
        )
        await db.commit()
    async with factory() as db:
        await recover_increment_dispatch(
            db,
            queue_id,
            dry_run=False,
            actor_id=1,
            evidence="Verified no worker entered",
            reason="Restart recovery",
        )
        await db.commit()
    fresh = payload(context)
    fresh["incremental_queue_ids"] = [queue_id]
    assert await service.claim_dispatch(fresh)

    class Worker:
        @billable_payload("pr_review")
        async def process_review_task(self, value, *, deadline):
            await admit_billing_operation()
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="real-new-request",
            ) as meter:
                meter.usage = complete_usage()
            return "done"

    monkeypatch.setattr(
        review_worker, "register_background_task", lambda task, kind: task
    )
    assert (
        await review_worker._run_review_task_with_timeout(
            Worker(), old, "owner1/repo#7"
        )
        == "deduplicated_incremental"
    )
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome is None
        assert db.get(BillingWallet, 2).reserved_units == 1_000_000
        assert list(db.scalars(select(AIUsageRecord))) == []
    await review_worker._run_review_task_with_timeout(Worker(), fresh, "owner1/repo#7")
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome == "completed"
        assert db.get(BillingWallet, 2).reserved_units == 0
        assert len(list(db.scalars(select(AIUsageRecord)))) == 1


@pytest.mark.asyncio
async def test_known_increment_child_registration_rejection_releases_admission(
    sql_runtime, monkeypatch
):
    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeAdmissionClosed,
    )
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    value = payload(context)
    value["incremental_queue_ids"] = [queue_id]
    assert await PRReviewIncrementalQueueService().claim_dispatch(value)
    entered = False

    class Worker:
        @billable_payload("pr_review")
        async def process_review_task(self, value, *, deadline):
            nonlocal entered
            entered = True
            raise AssertionError("Child must be cancelled before first scheduling")

    def reject(task, category):
        raise DatabaseResetRuntimeAdmissionClosed("review_process")

    monkeypatch.setattr(review_worker, "register_background_task", reject)
    with pytest.raises(DatabaseResetRuntimeAdmissionClosed):
        await review_worker._run_review_task_with_timeout(
            Worker(), value, "owner1/repo#7"
        )
    assert not entered
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome == "failed"
        assert db.get(BillingWallet, 2).reserved_units == 0
        assert db.get(PRReviewIncrementalQueue, queue_id).status == "failed"
        assert list(db.scalars(select(AIUsageRecord))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_increment_preserves_actual_cost_and_drains_next(
    sql_runtime, monkeypatch, cancelled
):
    from backend.models.billing_models import BillingUsageCharge
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    failed, first_id = await add_increment(factory, "queued-B", 2)
    next_context, _ = await add_increment(factory, "queued-C", 1, head="C")
    captured = []

    async def capture(value):
        captured.append(value)

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    monkeypatch.setattr(
        review_worker, "register_background_task", lambda task, kind: task
    )
    await review_worker._drain_pending_incremental(
        payload(BillingContext(1, "active-A", "pr_review", {}))
    )

    class Worker:
        @billable_payload("pr_review")
        async def process_review_task(self, value, *, deadline):
            await admit_billing_operation()
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="failed-business",
            ) as meter:
                meter.usage = complete_usage()
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("publication failed after actual usage")

    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await review_worker._run_review_task_with_timeout(
            Worker(), captured.pop(0), "owner1/repo#7"
        )
    assert len(captured) == 1
    assert context_for_payload(captured[0], "pr_review") == next_context
    with Session(engine) as db:
        expected = "cancelled" if cancelled else "failed"
        assert db.get(BillingOperation, failed.operation_id).outcome == expected
        assert db.get(PRReviewIncrementalQueue, first_id).status == expected
        assert db.get(BillingWallet, 2).reserved_units == 0
        assert db.get(BillingWallet, 2).balance_units == 100_000_000
        assert db.scalar(select(BillingUsageCharge.provider_cost)) is not None


@pytest.mark.asyncio
async def test_maintenance_preserves_expired_but_recoverable_queued_admission(
    sql_runtime,
):
    from backend.core.time_service import now_utc

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    async with factory() as db:
        (await db.get(BillingOperation, context.operation_id)).expires_at = (
            now_utc() - timedelta(seconds=1)
        )
        await db.commit()
    async with factory() as db:
        report = await BillingService(db).recover_operations(dry_run=False)
        assert report == [
            {
                "operation_id": context.operation_id,
                "status": "queued_dispatch_recovery",
                "reserved_units": 1_000_000,
            }
        ]
        await db.commit()
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome is None
        assert db.get(BillingWallet, 2).reserved_units == 1_000_000
        assert db.get(PRReviewIncrementalQueue, queue_id).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_operator_resume_checks_pr_state_and_uses_stored_payer(
    sql_runtime, monkeypatch, closed
):
    from backend.api.v1.queue import IncrementalResumeRequest, resume_incremental_queue
    from backend.core.github_app import GitHubAppClient
    from backend.models.telegram_models import TelegramUser
    from backend.workers import review_worker

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        db.add(
            PRReview(
                pr_id=42,
                pr_number=7,
                repo_owner="owner1",
                repo_name="repo",
                author="trusted-author",
                title="stored title",
                branch="stored-branch",
                strategy="standard",
                status="completed",
            )
        )
        await db.commit()
    monkeypatch.setattr(
        GitHubAppClient,
        "get_repo_client",
        lambda *_: SimpleNamespace(
            get_repo=lambda _: SimpleNamespace(
                get_pull=lambda _: SimpleNamespace(
                    state="closed" if closed else "open", merged=False
                )
            )
        ),
    )
    submitted = []

    async def capture(value):
        submitted.append(value)

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    async with factory() as db:
        result = await resume_incremental_queue(
            queue_id,
            IncrementalResumeRequest(
                evidence="Verified worker never entered", reason="resume after restart"
            ),
            db=db,
            user={"user_id": 1},
        )
        assert result.status_code == 200
    if closed:
        assert submitted == []
        with Session(engine) as db:
            from backend.models.admin_action_log import AdminActionLog

            assert db.get(BillingOperation, context.operation_id).outcome == "cancelled"
            assert db.get(BillingWallet, 2).reserved_units == 0
            audit = db.scalar(select(AdminActionLog))
            assert audit.admin_id == 1 and audit.action == "billing.incremental_closed"
            assert "Verified worker never entered" in audit.detail
    else:
        assert len(submitted) == 1
        assert context_for_payload(submitted[0], "pr_review") == context
        assert submitted[0]["pr_id"] == 42
        assert submitted[0]["author"] == "trusted-author"


@pytest.mark.asyncio
async def test_incremental_recovery_rejects_non_admin_and_client_payer(sql_runtime):
    from pydantic import ValidationError

    from backend.api.v1.queue import IncrementalResumeRequest, resume_incremental_queue

    factory, _, _ = sql_runtime
    with pytest.raises(ValidationError):
        IncrementalResumeRequest(evidence="test", reason="test", user_id=2)
    async with factory() as db:
        result = await resume_incremental_queue(
            1,
            IncrementalResumeRequest(evidence="test", reason="test"),
            db=db,
            user={"user_id": 1},
        )
        assert result.status_code == 403


@pytest.mark.asyncio
async def test_nonleader_recovery_resets_whole_same_operation_group(sql_runtime):
    from backend.core.time_service import now_utc
    from backend.models.telegram_models import TelegramUser
    from backend.services.pr_review_incremental_recovery import (
        recover_increment_dispatch,
    )

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, leader_id = await add_increment(factory, "queued-B", 2, head="B1")
    _, second_id = await add_increment(factory, "queued-B", 2, head="B2", base="B1")
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        for queue_id in (leader_id, second_id):
            item = await db.get(PRReviewIncrementalQueue, queue_id)
            item.status = "dispatching"
            item.dispatch_token = "old-token"
            item.dispatch_expires_at = now_utc() - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        report = await recover_increment_dispatch(
            db,
            second_id,
            dry_run=False,
            actor_id=1,
            evidence="Both queued handoffs never entered worker",
            reason="resume same execution",
        )
        assert report["status"] == "ready"
        await db.commit()
    value = payload(context)
    value["incremental_queue_ids"] = [leader_id, second_id]
    assert await PRReviewIncrementalQueueService().claim_dispatch(value)
    with Session(engine) as db:
        assert {
            row.dispatch_token for row in db.scalars(select(PRReviewIncrementalQueue))
        } == {value["incremental_dispatch_token"]}


@pytest.mark.asyncio
async def test_closed_operator_resume_requires_evidence_before_cancellation(
    sql_runtime, monkeypatch
):
    from backend.api.v1.queue import IncrementalResumeRequest, resume_incremental_queue
    from backend.core.github_app import GitHubAppClient
    from backend.models.telegram_models import TelegramUser

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = "super_admin"
        db.add(
            PRReview(
                pr_id=42,
                pr_number=7,
                repo_owner="owner1",
                repo_name="repo",
                author="trusted-author",
                title="stored title",
                branch="stored-branch",
                strategy="standard",
                status="completed",
            )
        )
        await db.commit()
    monkeypatch.setattr(
        GitHubAppClient,
        "get_repo_client",
        lambda *_: SimpleNamespace(
            get_repo=lambda _: SimpleNamespace(
                get_pull=lambda _: SimpleNamespace(state="closed", merged=False)
            )
        ),
    )
    async with factory() as db:
        result = await resume_incremental_queue(
            queue_id,
            IncrementalResumeRequest(evidence=" ", reason=" "),
            db=db,
            user={"user_id": 1},
        )
        assert result.status_code == 400
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome is None
        assert db.get(BillingWallet, 2).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_pr_close_preserves_already_called_operation_until_worker_finishes(
    sql_runtime,
):
    from backend.models.billing_models import BillingUsageCharge

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    with bind_billing_context(context):
        await admit_billing_operation()
        async with ProviderUsageMeter(
            provider_id="provider",
            model_id="model",
            protocol_family="openai_compatible",
            call_kind="chat",
            role="main",
            logical_call_id="actual-request-before-close",
        ) as meter:
            meter.usage = complete_usage()
        assert (
            await PRReviewIncrementalQueueService().cancel_pending_for_pr(
                "owner1/repo", 7
            )
            == 1
        )
        with Session(engine) as db:
            assert db.get(BillingOperation, context.operation_id).outcome is None
            assert db.get(BillingWallet, 2).reserved_units > 0
            assert db.get(PRReviewIncrementalQueue, queue_id).status == "cancelled"
            assert len(list(db.scalars(select(AIUsageRecord)))) == 1
        await finish_billing_operation("cancelled")
    with Session(engine) as db:
        assert db.get(BillingOperation, context.operation_id).outcome == "cancelled"
        assert db.get(BillingWallet, 2).reserved_units == 0
        assert db.scalar(select(BillingUsageCharge.provider_cost)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "running",
        "malformed",
        "payer",
        "pr_source",
        "operation_source",
        "called",
    ],
)
async def test_recoverable_carrier_requires_verified_original_no_call_source(
    sql_runtime, case
):
    from backend.models.billing_models import BillingCallAttempt
    from backend.services.pr_review_incremental_recovery import (
        has_recoverable_incremental_carrier,
    )

    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    context, queue_id = await add_increment(factory, "queued-B", 2)
    async with factory() as db:
        item = await db.get(PRReviewIncrementalQueue, queue_id)
        if case == "running":
            item.status = "running"
        elif case == "malformed":
            item.billing_context = {"operation_id": context.operation_id}
        elif case == "payer":
            item.billing_context = {**context.to_payload(), "user_id": 1}
        elif case == "pr_source":
            item.billing_context = {
                **context.to_payload(),
                "source": {"repo_full_name": "owner1/repo", "pr_number": 99},
            }
        elif case == "operation_source":
            (await db.get(BillingOperation, context.operation_id)).source = {
                "repo_full_name": "owner1/repo",
                "pr_number": 99,
            }
        elif case == "called":
            db.add(
                BillingCallAttempt(
                    call_id="actual-unknown",
                    operation_id=context.operation_id,
                    provider_id="provider",
                    model_id="model",
                    call_kind="chat",
                    state="started",
                )
            )
        await db.commit()
    async with factory() as db:
        assert await has_recoverable_incremental_carrier(db, context.operation_id) == (
            case == "valid"
        )


@pytest.mark.asyncio
async def test_dispatch_rejects_mispaired_queue_ids_without_overwriting_payer(
    sql_runtime,
):
    from backend.services.billing_service import BillingError

    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    first, first_id = await add_increment(factory, "queued-B", 2)
    other, other_id = await add_increment(factory, "queued-C", 1)
    value = payload(first)
    value["incremental_queue_ids"] = [first_id, other_id]
    with pytest.raises(BillingError):
        await PRReviewIncrementalQueueService().claim_dispatch(value)
    with Session(engine) as db:
        assert (
            db.get(PRReviewIncrementalQueue, first_id).billing_context
            == first.to_payload()
        )
        assert (
            db.get(PRReviewIncrementalQueue, other_id).billing_context
            == other.to_payload()
        )
        assert db.get(PRReviewIncrementalQueue, first_id).dispatch_token is None
        assert db.get(PRReviewIncrementalQueue, other_id).dispatch_token is None
        assert db.get(BillingWallet, 1).reserved_units == 1_000_000
        assert db.get(BillingWallet, 2).reserved_units == 1_000_000
