"""Signed Agent redelivery resumes durable admission without inventing handoff."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.api import webhook
from backend.core.time_service import now_utc
from backend.models.agent_team_models import AgentTeamTask
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.legacy_entitlement_models import RateLimitAdmission
from backend.models.service_execution_models import ServiceExecutionOwnership
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.agent_team.billing_admission import delivery_operation_id
from backend.services.billing_service import BillingService
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


def payload(is_pr, delivery="TEST-agent-delivery"):
    return {
        "action": "created",
        "repository": {
            "owner": {"login": "owner1"},
            "name": "repo",
            "full_name": "owner1/repo",
        },
        "issue": {"number": 42, **({"pull_request": {}} if is_pr else {})},
        "comment": {"id": 51, "body": "/agent", "user": {"login": "collaborator"}},
        "_sakura_delivery_id": delivery,
    }


async def prepare(factory, monkeypatch, is_pr):
    monkeypatch.setattr(webhook, "get_async_session", factory)
    monkeypatch.setattr(
        webhook, "_check_agent_team_enabled", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        webhook, "_check_agent_permission", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(webhook, "_post_issue_comment", AsyncMock())

    class NoSourceRefetch:
        def get_repo_client(self, *args):
            pytest.fail("Recovery must use the persisted, verified Agent source")

    monkeypatch.setattr(webhook, "GitHubAppClient", NoSourceRefetch)

    async def payment_disabled():
        return False

    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled", payment_disabled
    )
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        user.agent_daily_quota = user.agent_weekly_quota = user.agent_monthly_quota = 10
        task = AgentTeamTask(
            repo_owner="owner1",
            repo_name="repo",
            repo_full_name="owner1/repo",
            source_type="pr_review" if is_pr else "manual_issue",
            source_issue_number=42,
            title="TEST durable task",
            started_by="collaborator",
            status="queued",
            webhook_delivery_id="TEST-agent-delivery",
            billing_operation_id=delivery_operation_id("TEST-agent-delivery"),
        )
        db.add(task)
        await BillingService(db).grant(1, "20", "TEST-fund")
        await db.commit()
        return task.id


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
@pytest.mark.parametrize("already_admitted", [False, True])
async def test_persisted_unsubmitted_agent_replays_admission_and_dispatch_once(
    sql_runtime, monkeypatch, is_pr, already_admitted
):
    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, is_pr)
    if already_admitted:
        assert (
            await webhook._consume_agent_quota_or_cleanup(
                object(), "owner1", "repo", "owner1/repo", task_id, 42
            )
            is None
        )
    submitted = []
    entered, release = asyncio.Event(), asyncio.Event()
    backgrounds = []

    async def worker(record_id):
        submitted.append(record_id)
        async with factory() as db:
            task = await db.get(AgentTeamTask, record_id)
            db.add(
                ServiceExecutionOwnership(
                    token="TEST-owner",
                    operation_id=task.billing_operation_id,
                    feature="agent",
                    owner_id="TEST-worker",
                    user_id=1,
                    state="queued",
                    expires_at=now_utc() + timedelta(seconds=60),
                )
            )
            await db.commit()
        entered.set()
        await release.wait()
        async with factory() as db:
            task = await db.get(AgentTeamTask, record_id)
            task.status = "completed"
            await BillingService(db).finish_operation(
                task.billing_operation_id, "completed"
            )
            owner = await db.get(ServiceExecutionOwnership, "TEST-owner")
            owner.state = "released"
            await db.commit()
        return record_id

    def schedule(coroutine, source):
        background = asyncio.create_task(coroutine)
        backgrounds.append(background)
        return background

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", worker
    )
    monkeypatch.setattr(webhook, "create_registered_background_task", schedule)
    handler = webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    first = await handler(payload(is_pr))
    try:
        async with factory() as db:
            assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
            assert (
                len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 1
            )
            assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
            assert (await db.get(AgentTeamTask, task_id)).billing_user_id == 1
        assert first.status_code == 202
        assert json.loads(first.body)["status"] == "queued"
        await asyncio.wait_for(entered.wait(), timeout=1)
        replay = await handler(payload(is_pr))
        assert (
            replay.status_code == 200 and json.loads(replay.body)["duplicate"] is True
        )
        assert submitted == [task_id]
    finally:
        release.set()
        await asyncio.gather(*backgrounds)
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        assert (
            await db.get(BillingOperation, receipt.operation_id)
        ).outcome == "completed"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


async def finish_worker(factory, task_id):
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        task.status = "completed"
        await BillingService(db).finish_operation(
            task.billing_operation_id, "completed"
        )
        await db.commit()
    return task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
@pytest.mark.parametrize("after_admission", [False, True])
async def test_interrupted_retryable_receipt_resumes_same_admission(
    sql_runtime, monkeypatch, is_pr, after_admission
):
    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, is_pr)
    consume = webhook._consume_agent_quota_or_cleanup

    async def interrupted(*args, **kwargs):
        if after_admission:
            assert await consume(*args, **kwargs) is None
        raise RuntimeError("TEST crash before handoff")

    monkeypatch.setattr(webhook, "_consume_agent_quota_or_cleanup", interrupted)
    handler = webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    first = await handler(payload(is_pr))
    assert first.status_code == 500
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "retryable"
        assert (await db.get(TelegramUser, 1)).agent_daily_used == int(after_admission)
    monkeypatch.setattr(webhook, "_consume_agent_quota_or_cleanup", consume)
    backgrounds, submissions = [], []

    async def submit(record_id):
        submissions.append(record_id)
        return await finish_worker(factory, record_id)

    def schedule(coroutine, source):
        task = asyncio.create_task(coroutine)
        backgrounds.append(task)
        return task

    monkeypatch.setattr(webhook, "create_registered_background_task", schedule)
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submit
    )
    assert (await handler(payload(is_pr))).status_code == 202
    await asyncio.gather(*backgrounds)
    assert submissions == [task_id]
    async with factory() as db:
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
async def test_unknown_local_handoff_is_not_accepted_or_repeated(
    sql_runtime, monkeypatch, is_pr
):
    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, is_pr)
    submitted = AsyncMock()
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submitted
    )

    def unknown(coroutine, source):
        coroutine.close()
        raise TimeoutError("TEST lost handoff acknowledgement")

    monkeypatch.setattr(webhook, "create_registered_background_task", unknown)
    handler = webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    assert (await handler(payload(is_pr))).status_code == 500
    second = await handler(payload(is_pr))
    assert second.status_code == 503
    assert json.loads(second.body)["reason"] == "delivery_pending_reconciliation"
    assert submitted.await_count == 0
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.source["task_id"] == task_id
        assert receipt.status == "pending_reconciliation"
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
async def test_parallel_agent_replays_share_one_dispatch_and_fingerprint(
    sql_runtime, monkeypatch, is_pr
):
    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, is_pr)
    submitted, deferred = [], []

    async def submit(record_id):
        submitted.append(record_id)
        return await finish_worker(factory, record_id)

    def schedule(coroutine, source):
        deferred.append(coroutine)

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submit
    )
    monkeypatch.setattr(webhook, "create_registered_background_task", schedule)
    handler = webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    responses = await asyncio.gather(*(handler(payload(is_pr)) for _ in range(3)))
    assert sorted(r.status_code for r in responses) == [202, 503, 503]
    assert len(deferred) == 1
    changed = payload(is_pr)
    changed["comment"]["body"] = "/agent base:other"
    assert (await handler(changed)).status_code >= 400
    await deferred[0]
    assert submitted == [task_id]
    async with factory() as db:
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert (await db.get(TelegramUser, 2)).agent_daily_used == 0
        assert len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_old_delivery_uses_original_outcome_after_manual_retry(
    sql_runtime, monkeypatch
):
    from backend.services.agent_team.billing_admission import prepare_agent_delivery

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, False)
    assert (
        await webhook._consume_agent_quota_or_cleanup(
            object(), "owner1", "repo", "owner1/repo", task_id, 42
        )
        is None
    )
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        await prepare_agent_delivery(db, payload(False), task, is_pr=False)
        original = task.billing_operation_id
        await BillingService(db).finish_operation(original, "failed")
        task.billing_operation_id = "TEST-fresh-manual-retry"
        await BillingService(db).register_operation(
            1, task.billing_operation_id, "agent"
        )
        await db.commit()
    response = await webhook.handle_agent_command(payload(False))
    assert response.status_code == 200 and json.loads(response.body)["duplicate"]
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.operation_id == original and receipt.status == "accepted"
        assert (
            await db.get(BillingOperation, "TEST-fresh-manual-retry")
        ).outcome is None


@pytest.mark.asyncio
async def test_database_dispatch_claim_is_atomic_across_independent_workers(
    sql_runtime, monkeypatch
):
    from backend.services.agent_team.billing_admission import (
        claim_agent_dispatch,
        prepare_agent_delivery,
    )

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, False)
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        await prepare_agent_delivery(db, payload(False), task, is_pr=False)

    def claim_in_worker():
        async def claim():
            async with factory() as db:
                return await claim_agent_dispatch(
                    db, payload(False), task_id, is_pr=False
                )

        return asyncio.run(claim())

    claims = await asyncio.gather(
        *(asyncio.to_thread(claim_in_worker) for _ in range(8))
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.owner_token == winners[0].owner_token
        assert receipt.status == "pending_reconciliation"


@pytest.mark.asyncio
@pytest.mark.parametrize("intervention", ["cancel", "manual_retry"])
async def test_late_delivery_is_fenced_at_worker_carrier_read(
    sql_runtime, monkeypatch, intervention
):
    from backend.services.agent_team.billing_admission import (
        claim_agent_dispatch,
        prepare_agent_delivery,
        run_agent_delivery,
    )
    from backend.services.billing_context import billable_record
    from backend.services.webhook_execution_reconciliation import reconcile_receipt

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, False)
    assert (
        await webhook._consume_agent_quota_or_cleanup(
            object(), "owner1", "repo", "owner1/repo", task_id, 42
        )
        is None
    )
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        await prepare_agent_delivery(db, payload(False), task, is_pr=False)
        claim = await claim_agent_dispatch(db, payload(False), task_id, is_pr=False)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    class Worker:
        @billable_record("agent")
        async def execute(self, record_id):
            effects.append(record_id)
            return record_id

    async def submit(record_id):
        # The local wrapper's earlier check succeeded, but the worker has not
        # loaded its carrier. This is the precise late-cancel/retry race.
        entered.set()
        await release.wait()
        return await Worker().execute(record_id)

    running = asyncio.create_task(run_agent_delivery(claim, submit, factory))
    await entered.wait()
    async with factory() as db:
        if intervention == "cancel":
            actor = await db.get(TelegramUser, 2)
            actor.role = "super_admin"
            await reconcile_receipt(
                db,
                claim.receipt_id,
                actor_id=2,
                resolution="cancelled_unstarted",
                evidence="TEST original process stopped before dispatch",
                reason="TEST operator recovery",
            )
        else:
            await BillingService(db).finish_operation(claim.operation_id, "failed")
            task = await db.get(AgentTeamTask, task_id)
            task.billing_operation_id = "TEST-new-user-execution"
            task.billing_user_id = 2
            await BillingService(db).grant(2, "20", "TEST-new-payer-fund")
            await BillingService(db).register_operation(
                2, task.billing_operation_id, "agent"
            )
        await db.commit()
    release.set()
    await asyncio.gather(running, return_exceptions=True)
    assert effects == []
    async with factory() as db:
        original = await db.get(BillingOperation, claim.operation_id)
        assert original.outcome == (
            "cancelled" if intervention == "cancel" else "failed"
        )
        if intervention == "manual_retry":
            assert (
                await db.get(BillingOperation, "TEST-new-user-execution")
            ).outcome is None
        else:
            assert (await db.get(AgentTeamTask, task_id)).status == "cancelled"
            assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_operator_cannot_label_claimed_agent_worker_unstarted(
    sql_runtime, monkeypatch
):
    from backend.services.agent_team.billing_admission import (
        claim_agent_dispatch,
        prepare_agent_delivery,
        run_agent_delivery,
    )
    from backend.services.billing_context import billable_record
    from backend.services.billing_service import BillingError
    from backend.services.webhook_execution_reconciliation import reconcile_receipt

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, False)
    assert (
        await webhook._consume_agent_quota_or_cleanup(
            object(), "owner1", "repo", "owner1/repo", task_id, 42
        )
        is None
    )
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        await prepare_agent_delivery(db, payload(False), task, is_pr=False)
        claim = await claim_agent_dispatch(db, payload(False), task_id, is_pr=False)
        actor = await db.get(TelegramUser, 2)
        actor.role = "super_admin"
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class Worker:
        @billable_record("agent")
        async def execute(self, record_id):
            entered.set()
            await release.wait()
            return await finish_worker(factory, record_id)

    running = asyncio.create_task(run_agent_delivery(claim, Worker().execute, factory))
    await entered.wait()
    try:
        async with factory() as db:
            receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
            assert receipt.status == "processing"
            with pytest.raises(BillingError, match="Claimed Agent"):
                await reconcile_receipt(
                    db,
                    claim.receipt_id,
                    actor_id=2,
                    resolution="cancelled_unstarted",
                    evidence="TEST no live ownership yet",
                    reason="TEST stale evidence",
                )
            await db.rollback()
    finally:
        release.set()
        await running


@pytest.mark.asyncio
@pytest.mark.parametrize("new_carrier", [False, True])
async def test_expired_processing_recovers_finance_then_original_carrier_only(
    sql_runtime, monkeypatch, new_carrier
):
    from backend.services.agent_team.billing_admission import (
        claim_agent_delivery_worker,
        claim_agent_dispatch,
        prepare_agent_delivery,
        run_agent_delivery,
    )
    from backend.services.webhook_execution_reconciliation import reconcile_receipt

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, False)
    assert (
        await webhook._consume_agent_quota_or_cleanup(
            object(), "owner1", "repo", "owner1/repo", task_id, 42
        )
        is None
    )
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        await prepare_agent_delivery(db, payload(False), task, is_pr=False)
        claim = await claim_agent_dispatch(db, payload(False), task_id, is_pr=False)

    async def interrupted_worker(record_id):
        async with factory() as db:
            assert await claim_agent_delivery_worker(db, record_id)
            await db.commit()
        raise RuntimeError("TEST process stopped after worker claim, before ownership")

    with pytest.raises(RuntimeError, match="TEST process stopped"):
        await run_agent_delivery(claim, interrupted_worker, factory)
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        assert receipt.status == "processing"
        operation = await db.get(BillingOperation, claim.operation_id)
        operation.expires_at = now_utc() - timedelta(seconds=1)
        actor = await db.get(TelegramUser, 2)
        actor.role = "super_admin"
        if new_carrier:
            task = await db.get(AgentTeamTask, task_id)
            task.billing_operation_id = "TEST-independent-new-operation"
        await db.commit()
    async with factory() as db:
        report = await BillingService(db).recover_operations(dry_run=True)
        assert [item["operation_id"] for item in report] == [claim.operation_id]
        assert (await db.get(BillingOperation, claim.operation_id)).outcome is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
    async with factory() as db:
        await BillingService(db).recover_operations(dry_run=False)
        await db.commit()
    async with factory() as db:
        await reconcile_receipt(
            db,
            claim.receipt_id,
            actor_id=2,
            resolution="terminal",
            evidence="TEST recovered expired execution with no live owner",
            reason="TEST carrier recovery",
        )
        await db.commit()
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        assert task.status == ("queued" if new_carrier else "failed")
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (
            await db.get(WebhookExecutionReceipt, claim.receipt_id)
        ).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pr", [False, True])
async def test_post_creation_unique_race_replay_dispatches_the_winning_task(
    sql_runtime, monkeypatch, is_pr
):
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    factory, _, _ = sql_runtime
    task_id = await prepare(factory, monkeypatch, is_pr)
    monkeypatch.setattr(webhook, "find_delivery_task", AsyncMock(return_value=None))

    class GitHubBoundary:
        def get_repo_client(self, *args):
            return self

        def get_repo(self, *args):
            return self

        def get_pull(self, *args):
            return SimpleNamespace(
                head=SimpleNamespace(
                    sha="TEST-head",
                    ref="TEST-branch",
                    repo=SimpleNamespace(full_name="owner1/repo"),
                ),
                html_url="https://github.com/owner1/repo/pull/42",
            )

    async def unique_race_winner(self, db, **kwargs):
        task = await db.get(AgentTeamTask, task_id)
        task._billing_delivery_replayed = True
        return task

    monkeypatch.setattr(webhook, "GitHubAppClient", GitHubBoundary)
    monkeypatch.setattr(webhook, "_validate_pr_head_admission", lambda *args: None)
    monkeypatch.setattr(
        "backend.services.agent_team.submission_context.load_issue_comments_for_context",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        AgentTeamCandidateService,
        "create_task_from_pr_review" if is_pr else "create_task_from_manual_issue",
        unique_race_winner,
    )
    deferred = []
    monkeypatch.setattr(
        webhook,
        "create_registered_background_task",
        lambda coroutine, source: deferred.append(coroutine),
    )

    async def submit(record_id):
        return await finish_worker(factory, record_id)

    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submit
    )
    handler = webhook.handle_pr_agent_command if is_pr else webhook.handle_agent_command
    assert (await handler(payload(is_pr))).status_code == 202
    assert len(deferred) == 1
    await deferred[0]
    async with factory() as db:
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert len((await db.execute(select(AgentTeamTask))).scalars().all()) == 1
        assert (
            await db.get(BillingOperation, delivery_operation_id("TEST-agent-delivery"))
        ).outcome == "completed"
