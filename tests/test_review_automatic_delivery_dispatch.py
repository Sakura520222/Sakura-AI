"""A persisted delivery must survive loss of its in-process scheduled worker."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from backend.api import webhook
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_service import BillingService
from backend.services.webhook_execution_service import verified_delivery
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_webhook_billing_admission import automatic_issue, automatic_pr

sql_runtime = runtime_fixture


@pytest.mark.asyncio
async def test_incremental_carrier_proves_ack_on_initial_delivery_and_replay(
    sql_runtime,
):
    from backend.services.webhook_execution_service import _claim, _finish
    from tests.test_billing_incremental_operations import add_increment

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-incremental-ack-fund")
        await db.commit()
    raw = automatic_pr(action="synchronize")
    raw["_sakura_delivery_id"] = "TEST-incremental-ack"
    claim = await _claim(raw, "pr_review", raw["_sakura_delivery_id"], factory)
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        operation_id = receipt.operation_id
    await add_increment(factory, operation_id)
    assert await _finish(
        claim,
        webhook.JSONResponse({"status": "accepted", "action": "queued_incremental"}),
    )
    replay = await _claim(raw, "pr_review", raw["_sakura_delivery_id"], factory)
    assert replay.status_code == 200
    assert json.loads(replay.body)["status"] == "deduplicated"
    async with factory() as db:
        assert (
            await db.get(WebhookExecutionReceipt, claim.receipt_id)
        ).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict", ["repository", "payer", "source", "feature", "cancelled"]
)
async def test_incremental_ack_rejects_unrelated_or_terminal_queue_carriers(
    sql_runtime, conflict
):
    from backend.models.database import PRReviewIncrementalQueue
    from backend.services.webhook_execution_service import _claim, _finish
    from tests.test_billing_incremental_operations import add_increment

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-incremental-conflict-fund")
        await db.commit()
    raw = {
        **automatic_pr(action="synchronize"),
        "_sakura_delivery_id": "TEST-conflict-ack",
    }
    claim = await _claim(raw, "pr_review", raw["_sakura_delivery_id"], factory)
    async with factory() as db:
        operation_id = (
            await db.get(WebhookExecutionReceipt, claim.receipt_id)
        ).operation_id
    _, queue_id = await add_increment(factory, operation_id)
    async with factory() as db:
        row = await db.get(PRReviewIncrementalQueue, queue_id)
        if conflict == "repository":
            row.repo_full_name = "other/private"
        elif conflict == "cancelled":
            row.status = "cancelled"
        else:
            context = dict(row.billing_context)
            if conflict == "payer":
                context["user_id"] = 2
            elif conflict == "feature":
                context["feature"] = "issue_analysis"
            else:
                context["source"] = {**context["source"], "pr_number": 9}
            row.billing_context = context
        await db.commit()
    assert not await _finish(claim, webhook.JSONResponse({"status": "accepted"}))
    assert (
        await _claim(raw, "pr_review", raw["_sakura_delivery_id"], factory)
    ).status_code == 503


@pytest.mark.asyncio
async def test_drain_detaches_previous_delivery_and_runs_the_next_increment(
    sql_runtime, monkeypatch
):
    from backend.services.billing_context import (
        billable_payload,
        mark_registered_operation,
    )
    from backend.services.pr_review_incremental_queue import (
        PRReviewIncrementalQueueService,
    )
    from backend.services.webhook_review_dispatch import delivery_worker
    from backend.workers import review_worker
    from tests.test_billing_incremental_operations import add_increment

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-incremental-detach-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, "pr_review")
    infos = []

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        info = {
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "pr_number": 7,
            "user_id": 1,
            "delivery_id": payload["_sakura_delivery_id"],
        }
        await submit(info)
        infos.append(info)
        return webhook.JSONResponse({"status": "accepted"})

    class Worker:
        @delivery_worker("pr_review")
        @billable_payload("pr_review")
        async def process(self, value):
            mark_registered_operation(value["billing_context"]["operation_id"])
            return value["billing_context"]["operation_id"]

    await handler({**automatic_pr(), "_sakura_delivery_id": "TEST-detached-original"})
    worker = Worker()
    await worker.process(infos[0])
    next_context, queue_id = await add_increment(factory, "TEST-next-increment")
    captured = []

    async def capture(value):
        captured.append(value)

    monkeypatch.setattr(review_worker, "submit_review_task", capture)
    assert await review_worker._drain_pending_incremental(infos[0])
    assert captured[0]["billing_context"]["operation_id"] == next_context.operation_id
    queue = PRReviewIncrementalQueueService()
    assert await queue.start_dispatch(captured[0])
    assert await worker.process(captured[0]) == next_context.operation_id
    await queue.finalize_for_operation(captured[0])
    from backend.models.database import PRReviewIncrementalQueue

    async with factory() as db:
        assert (await db.get(PRReviewIncrementalQueue, queue_id)).status == "consumed"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
    for handle in scheduled:
        handle.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["issue", "review_helper", "review_webhook"])
async def test_live_cancellation_precedes_slow_durable_cancel(
    sql_runtime, monkeypatch, path
):
    from backend.services import webhook_review_dispatch as dispatch
    from backend.workers import issue_worker, review_worker

    entered, release = asyncio.Event(), asyncio.Event()
    worker = (
        issue_worker.IssueWorker if path == "issue" else review_worker.ReviewWorker
    ).__new__(
        issue_worker.IssueWorker if path == "issue" else review_worker.ReviewWorker
    )
    event = asyncio.Event()
    key = "owner1/repo#7"
    worker._cancel_events = {key: {"TEST-running": event} if path == "issue" else event}
    monkeypatch.setattr(review_worker, "get_worker", lambda: worker)

    async def persist(*args, **kwargs):
        entered.set()
        await release.wait()
        return 0

    monkeypatch.setattr(dispatch, "cancel_queued_deliveries", persist)
    monkeypatch.setattr(
        "backend.services.check_run_service.CheckRunService.cancel_active_runs_by_sha",
        AsyncMock(return_value=0),
    )
    if path == "issue":
        task = asyncio.create_task(worker.cancel_task(key))
    elif path == "review_helper":
        task = asyncio.create_task(review_worker.cancel_review_task_durably(key))
    else:
        task = asyncio.create_task(
            webhook.handle_pull_request_event(automatic_pr(action="closed"))
        )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert event.is_set(), (
            "A resumed provider result must observe cancellation during database I/O"
        )
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_startup_dispatch_failure_is_reported_and_other_carrier_still_resumes(
    sql_runtime, monkeypatch
):
    from backend.services import webhook_review_dispatch as dispatch

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-isolated-resume-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, "pr_review")

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        await submit(
            {
                "repo_full_name": "owner1/repo",
                "repo_owner": "owner1",
                "repo_name": "repo",
                "pr_number": 7,
                "user_id": 1,
                "delivery_id": payload["_sakura_delivery_id"],
            }
        )
        return webhook.JSONResponse({"status": "accepted"})

    for delivery in ("TEST-isolate-one", "TEST-isolate-two"):
        await handler({**automatic_pr(), "_sakura_delivery_id": delivery})
    real = dispatch.dispatch_restored
    restored = []

    async def sometimes(payload, feature):
        restored.append(payload["_delivery_receipt_id"])
        if len(restored) == 1:
            raise RuntimeError("TEST-private-error-do-not-echo")
        return await real(payload, feature)

    monkeypatch.setattr(dispatch, "dispatch_restored", sometimes)
    assert await dispatch.resume_queued_deliveries(session_factory=factory) == 1
    assert len(restored) == 2
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, restored[0])
        assert receipt.status == "pending_reconciliation"
        assert receipt.response["_dispatch"]["last_dispatch_error"] == "RuntimeError"
        assert "TEST-private-error" not in json.dumps(receipt.response)
        assert (await db.get(BillingWallet, 1)).reserved_units == 2_000_000
    for task in scheduled:
        task.cancel()


def queue_without_start(monkeypatch, feature):
    from backend.workers import issue_worker, review_worker

    module = review_worker if feature == "pr_review" else issue_worker
    worker = (
        review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
        if feature == "pr_review"
        else issue_worker.IssueWorker.__new__(issue_worker.IssueWorker)
    )
    worker._background_tasks = set()
    worker._cancel_events = {}
    scheduled = []

    def lost(coroutine):
        from backend.services.webhook_execution_service import _delivery_claim

        assert _delivery_claim.get() is None
        coroutine.close()  # Isolated process exit before the first instruction.
        handle = asyncio.get_running_loop().create_future()
        scheduled.append(handle)
        return handle

    proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    proxy.create_task = lost
    monkeypatch.setattr(module, "asyncio", proxy)
    monkeypatch.setattr(module, "ensure_background_admission", lambda *a: None)
    monkeypatch.setattr(module, "register_background_task", lambda *a: None)
    monkeypatch.setattr(
        module,
        "get_worker" if feature == "pr_review" else "get_issue_worker",
        lambda: worker,
    )
    return (
        module.submit_review_task
        if feature == "pr_review"
        else module.submit_issue_analysis_task,
        scheduled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_successful_handoff_has_durable_payload_and_replay_resumes_same_operation(
    sql_runtime, monkeypatch, feature
):
    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-dispatch-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, feature)
    resource = automatic_pr() if feature == "pr_review" else automatic_issue()
    resource["_sakura_delivery_id"] = f"TEST-persistent-{feature}"
    infos = []

    @verified_delivery(feature, factory)
    async def handler(payload):
        info = {
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "user_id": 1,
            "delivery_id": payload["_sakura_delivery_id"],
            "pr_number" if feature == "pr_review" else "issue_number": 7
            if feature == "pr_review"
            else 8,
            "title": "TEST title",
            "body": "TEST body",
            "author": "owner1",
            "state": "open",
        }
        infos.append(info)
        task = await submit(info)
        return webhook.JSONResponse({"status": "accepted", "task_id": task})

    assert (await handler(resource)).status_code == 200
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert "_dispatch" in receipt.response
        assert receipt.response["_dispatch"]["state"] == "queued"
        assert receipt.response["_dispatch"]["payload"]["body"] == "TEST body"
        operation_id = receipt.operation_id
        assert (await db.get(BillingOperation, operation_id)).outcome is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
    replay = await handler(resource)
    assert replay.status_code == 200
    assert (
        "TEST body" not in replay.body.decode()
        and "_dispatch" not in replay.body.decode()
    )
    assert len(infos) == 1
    assert len(scheduled) == 2
    async with factory() as db:
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
    for task in scheduled:
        task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_started_without_live_or_terminal_evidence_never_acknowledges_or_requeues(
    sql_runtime, monkeypatch, feature
):
    from backend.services.webhook_review_dispatch import resume_queued_deliveries

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-started-unknown-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, feature)
    raw = automatic_pr() if feature == "pr_review" else automatic_issue()
    raw["_sakura_delivery_id"] = f"TEST-started-unknown-{feature}"
    calls = []

    @verified_delivery(feature, factory)
    async def handler(payload):
        calls.append(payload)
        info = {
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "user_id": 1,
            "delivery_id": payload["_sakura_delivery_id"],
            "pr_number" if feature == "pr_review" else "issue_number": 7
            if feature == "pr_review"
            else 8,
            "title": "TEST",
            "body": "TEST",
            "author": "owner1",
            "state": "open",
        }
        await submit(info)
        return webhook.JSONResponse({"status": "accepted"})

    assert (await handler(raw)).status_code == 200
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        receipt.response = {
            **receipt.response,
            "_dispatch": {**receipt.response["_dispatch"], "state": "started"},
        }
        receipt.status = "accepted"  # Lost parent ACK overwrote the startup fence.
        await db.commit()
    replay = await handler(raw)
    assert replay.status_code == 503
    assert await resume_queued_deliveries(session_factory=factory) == 0
    assert len(calls) == len(scheduled) == 1
    async with factory() as db:
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
    for task in scheduled:
        task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_startup_resumes_queued_carrier_and_fences_old_and_duplicate_workers(
    sql_runtime, monkeypatch, feature
):
    from backend.services.ai_usage_service import ProviderUsageMeter
    from backend.services.billing_context import (
        billable_payload,
        get_billing_context,
        mark_registered_operation,
    )
    from backend.services.webhook_review_dispatch import (
        delivery_worker,
        resume_queued_deliveries,
    )
    from tests.test_billing_usage_attribution import complete_usage, fund_and_price

    factory, _, _ = sql_runtime
    await fund_and_price(factory)
    submit, scheduled = queue_without_start(monkeypatch, feature)
    payload = automatic_pr() if feature == "pr_review" else automatic_issue()
    payload["_sakura_delivery_id"] = f"TEST-startup-{feature}"
    old = []

    @verified_delivery(feature, factory)
    async def handler(raw):
        info = {
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "user_id": 1,
            "delivery_id": raw["_sakura_delivery_id"],
            "pr_number" if feature == "pr_review" else "issue_number": 7
            if feature == "pr_review"
            else 8,
            "title": "TEST",
            "body": "TEST",
            "author": "owner1",
            "state": "open",
        }
        result = await submit(info)
        old.append(dict(info))
        return webhook.JSONResponse({"status": "accepted", "task_id": result})

    assert (await handler(payload)).status_code == 200
    assert await resume_queued_deliveries(session_factory=factory) == 1
    assert len(scheduled) == 2
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        current = {
            **receipt.response["_dispatch"]["payload"],
            "_delivery_receipt_id": receipt.receipt_id,
            "_delivery_owner_token": receipt.owner_token,
        }
    calls = []

    class Worker:
        @delivery_worker(feature)
        @billable_payload(feature)
        async def run(self, info):
            context = get_billing_context()
            mark_registered_operation(context.operation_id)
            calls.append(context.operation_id)
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind="chat",
                role="main",
                logical_call_id="TEST-main",
            ) as meter:
                meter.usage = complete_usage()
            return info["task_id"]

    worker = Worker()
    assert await worker.run(old[0]) == "deduplicated_delivery"
    await worker.run(current)
    assert await worker.run(current) == "deduplicated_delivery"
    assert len(calls) == 1
    assert await resume_queued_deliveries(session_factory=factory) == 0
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        assert operation.outcome == "completed" and operation.settled_units == 156_000
        wallet = await db.get(BillingWallet, 1)
        assert (wallet.balance_units, wallet.reserved_units) == (99_844_000, 0)
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]
    for task in scheduled:
        task.cancel()


@pytest.mark.asyncio
async def test_signed_analyze_concurrent_delivery_is_claimed_before_quota_and_submit(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        for period in ("daily", "weekly", "monthly"):
            setattr(user, f"issue_{period}_quota", 10)
        await BillingService(db).grant(1, "20", "TEST-analyze-fund")
        await db.commit()
    monkeypatch.setattr(webhook, "get_async_session", factory)
    monkeypatch.setattr(
        webhook.settings, "github_webhook_secret", "TEST-native-signature"
    )
    monkeypatch.setattr(webhook, "get_dynamic_config", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled",
        AsyncMock(return_value=False),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    submits = []

    async def submit(info):
        submits.append(dict(info))
        entered.set()
        await release.wait()
        return "TEST-analyze-task"

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    payload = automatic_issue()
    payload.pop("_sakura_delivery_id")
    payload.update(
        action="created",
        comment={"id": 101, "body": "/analyze", "user": {"login": "owner1"}},
    )
    raw = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"TEST-native-signature", raw, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "TEST-analyze-delivery",
        "Content-Type": "application/json",
    }
    app = FastAPI()
    app.include_router(webhook.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(
            client.post("/github", content=raw, headers=headers)
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            client.post("/github", content=raw, headers=headers)
        )
        await asyncio.sleep(0)
        release.set()
        replies = await asyncio.gather(first, second)
    assert len(submits) == 1
    assert all(response.status_code in {200, 503} for response in replies)
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.feature == "issue_analysis"
        assert receipt.operation_id == submits[0]["billing_context"]["operation_id"]
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_audited_operator_handoff_remains_acknowledged_on_replay(sql_runtime):
    from backend.services.webhook_execution_reconciliation import reconcile_receipt
    from backend.services.webhook_execution_service import _claim
    from tests.test_webhook_execution_reconciliation import pending

    factory, _, _ = sql_runtime
    claim = await pending(factory)
    async with factory() as db:
        await reconcile_receipt(
            db,
            claim.receipt_id,
            actor_id=2,
            resolution="accepted",
            evidence="TEST reviewed handoff evidence",
            reason="TEST restore ACK",
        )
        await db.commit()
    payload = automatic_pr()
    payload["_sakura_delivery_id"] = "reviewed-pending"
    reply = await _claim(payload, "pr_review", "reviewed-pending", factory)
    assert (
        reply.status_code == 200 and json.loads(reply.body)["status"] == "deduplicated"
    )


@pytest.mark.asyncio
async def test_repeated_operator_ack_of_queued_carrier_has_one_audit(
    sql_runtime, monkeypatch
):
    from backend.models.admin_action_log import AdminActionLog
    from backend.services.webhook_execution_reconciliation import reconcile_receipt

    factory, _, _ = sql_runtime
    async with factory() as db:
        (await db.get(TelegramUser, 2)).role = "super_admin"
        await BillingService(db).grant(1, "20", "TEST-operator-queued-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, "pr_review")

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        await submit(
            {
                "repo_full_name": "owner1/repo",
                "pr_number": 7,
                "user_id": 1,
                "delivery_id": payload["_sakura_delivery_id"],
            }
        )
        return webhook.JSONResponse({"status": "accepted"})

    await handler({**automatic_pr(), "_sakura_delivery_id": "TEST-operator-queued"})
    async with factory() as db:
        receipt_id = (
            (await db.execute(select(WebhookExecutionReceipt))).scalar_one().receipt_id
        )
    for _ in range(2):
        async with factory() as db:
            await reconcile_receipt(
                db,
                receipt_id,
                actor_id=2,
                resolution="accepted",
                evidence="TEST durable carrier",
                reason="TEST restore ACK",
            )
            await db.commit()
    async with factory() as db:
        assert len((await db.execute(select(AdminActionLog))).scalars().all()) == 1
    for task in scheduled:
        task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_queued_cancel_and_finance_expiry_never_resurrect_or_discard_carrier(
    sql_runtime, monkeypatch, feature
):
    from datetime import timedelta

    from backend.core.time_service import now_utc
    from backend.services.webhook_review_dispatch import resume_queued_deliveries
    from backend.workers import issue_worker, review_worker

    factory, _, _ = sql_runtime
    async with factory() as db:
        await BillingService(db).grant(1, "20", "TEST-queued-cancel-fund")
        await db.commit()
    submit, scheduled = queue_without_start(monkeypatch, feature)
    raw = automatic_pr() if feature == "pr_review" else automatic_issue()
    raw["_sakura_delivery_id"] = f"TEST-queued-cancel-{feature}"

    @verified_delivery(feature, factory)
    async def handler(payload):
        info = {
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "user_id": 1,
            "delivery_id": payload["_sakura_delivery_id"],
            "pr_number" if feature == "pr_review" else "issue_number": 7
            if feature == "pr_review"
            else 8,
            "title": "TEST",
            "body": "TEST",
            "author": "owner1",
            "state": "open",
        }
        await submit(info)
        return webhook.JSONResponse({"status": "accepted"})

    assert (await handler(raw)).status_code == 200
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        operation.expires_at = now_utc() - timedelta(hours=1)
        await db.commit()
        report = await BillingService(db).recover_operations(dry_run=False)
        await db.commit()
        assert report[0]["status"] == "queued_dispatch_recovery"
        assert (await db.get(BillingOperation, operation.operation_id)).outcome is None
    if feature == "pr_review":
        assert await review_worker.cancel_review_task_durably("owner1/repo#7")
    else:
        assert await issue_worker.get_issue_worker().cancel_task("owner1/repo#8")
    assert await resume_queued_deliveries(session_factory=factory) == 0
    assert (await handler(raw)).status_code == 200
    assert len(scheduled) == 1
    async with factory() as db:
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (
            await db.execute(select(BillingOperation))
        ).scalar_one().outcome == "cancelled"
    for task in scheduled:
        task.cancel()
