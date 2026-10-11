"""A manual reanalysis has a visible, recoverable carrier before I/O handoff."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.database import IssueAnalysis
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.issues import reanalysis_admission as service
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_review_issue_reanalysis_billing import reanalysis_app as app_fixture
from tests.test_review_issue_reanalysis_billing import seed, url

sql_runtime = runtime_fixture
reanalysis_app = app_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_carrier_and_receipt_are_committed_before_submit(
    reanalysis_app, monkeypatch, surface
):
    app, factory = reanalysis_app
    await seed(factory, funded=True)
    captured = []

    async def submit(payload):
        captured.append(payload)
        async with factory() as db:
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            assert receipt.source["origin"] == "issue_reanalysis"
            row = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
            assert row.id != 1 and row.status == "pending" and row.analysis_version == 3
            assert row.body == (await db.get(IssueAnalysis, 1)).body
            assert receipt.operation_id == payload["billing_context"]["operation_id"]
            assert receipt.source["task_id"] == payload["task_id"]
            assert "body" not in receipt.source and "title" not in receipt.source
            assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        return payload["task_id"]

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(url(surface))
        assert response.status_code == 200 and response.json()["success"]
        data = response.json().get("data", response.json())
        task_id = data["task_id"]
        assert task_id == captured[0]["task_id"]
        status = await client.get(f"/api/v1/issues/tasks/{task_id}")
        assert status.status_code == 200
        assert status.json()["data"]["status"] in {"pending", "queued"}
    async with factory() as db:
        assert (await db.get(IssueAnalysis, 1)).status == "completed"
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_process_exit_after_admission_leaves_retryable_persisted_status(
    reanalysis_app, monkeypatch
):
    app, factory = reanalysis_app
    await seed(factory, funded=True)
    original = service.admit_reanalysis_operation

    class SimulatedExit(BaseException):
        pass

    async def crash_after_commit(db, context):
        await original(db, context)
        raise SimulatedExit()

    monkeypatch.setattr(service, "admit_reanalysis_operation", crash_after_commit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(SimulatedExit):
            await client.post(url("api"))
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "retryable"
        row = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        assert row.status == "pending"
        assert (await db.get(BillingOperation, receipt.operation_id)).outcome is None
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_committed_unstarted_task_retries_same_identity_without_new_reserve(
    reanalysis_app, monkeypatch
):
    from backend.api.v1.deps import require_api_auth

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    original = service.admit_reanalysis_operation

    class SimulatedExit(BaseException):
        pass

    async def crash_after_commit(db, context):
        await original(db, context)
        raise SimulatedExit()

    monkeypatch.setattr(service, "admit_reanalysis_operation", crash_after_commit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(SimulatedExit):
            await client.post(url("api"))
        async with factory() as db:
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            task_id = receipt.source["task_id"]
            operation_id = receipt.operation_id
        monkeypatch.setattr(service, "admit_reanalysis_operation", original)
        submit = AsyncMock(side_effect=lambda info: info["task_id"])
        monkeypatch.setattr(
            "backend.workers.issue_worker.submit_issue_analysis_task", submit
        )
        response = await client.post(f"/api/v1/issues/tasks/{task_id}/retry")
        assert response.status_code == 200
        assert response.json()["data"]["task_id"] == task_id
        assert (
            submit.call_args.args[0]["billing_context"]["operation_id"] == operation_id
        )
        duplicate = await client.post(f"/api/v1/issues/tasks/{task_id}/retry")
        assert duplicate.status_code == 503
        submit.assert_awaited_once()
        app.dependency_overrides[require_api_auth] = lambda: {
            "user_id": 2,
            "role": "user",
        }
        assert (await client.get(f"/api/v1/issues/tasks/{task_id}")).status_code == 404
        assert (
            await client.post(f"/api/v1/issues/tasks/{task_id}/retry")
        ).status_code == 404
    async with factory() as db:
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert len((await db.execute(select(IssueAnalysis))).scalars().all()) == 2
        wallet = await db.get(BillingWallet, 1)
        assert (wallet.balance_units, wallet.reserved_units) == (10_000_000, 1_000_000)


@pytest.mark.asyncio
async def test_unknown_handoff_can_be_fenced_and_cancelled_without_running_late_worker(
    reanalysis_app, monkeypatch
):
    from backend.models.telegram_models import TelegramUser
    from backend.services.issues.reanalysis_dispatch import durable_reanalysis_worker
    from backend.services.webhook_execution_reconciliation import reconcile_receipt

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    captured = []

    async def unknown_submit(payload):
        captured.append(dict(payload))
        raise RuntimeError("TEST acknowledgement lost")

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", unknown_submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(url("api"))
        assert response.status_code == 503
    async with factory() as db:
        admin = await db.get(TelegramUser, 2)
        admin.role = "super_admin"
        await db.commit()
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        await reconcile_receipt(
            db,
            receipt.receipt_id,
            resolution="cancelled_unstarted",
            actor_id=2,
            reason="TEST prove dispatch did not start",
            evidence="TEST supervisor confirms no Worker started",
        )
        await db.commit()
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (
            await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        ).status == "cancelled"
    body = AsyncMock()
    worker = SimpleNamespace(
        _task_handles={}, _make_task_key=lambda payload: ("owner1/repo", 7)
    )
    await durable_reanalysis_worker(body)(worker, captured[0])
    body.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_worker_claim_and_processing_cancel_use_durable_fence(
    reanalysis_app, monkeypatch
):
    from backend.models.telegram_models import TelegramUser
    from backend.services.billing_service import BillingError
    from backend.services.webhook_execution_reconciliation import reconcile_receipt
    from backend.workers import issue_worker as module

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    payloads = []

    async def submit(payload):
        payloads.append(dict(payload))
        return payload["task_id"]

    monkeypatch.setattr(module, "submit_issue_analysis_task", submit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(url("api"))).status_code == 200
    async with factory() as db:
        (await db.get(TelegramUser, 2)).role = "super_admin"
        await db.commit()

    started, finish = asyncio.Event(), asyncio.Event()
    worker = module.IssueWorker.__new__(module.IssueWorker)

    async def body(payload, **kwargs):
        started.set()
        await finish.wait()
        return payload["task_id"]  # Coalescing without a new AI analysis.

    worker._run_issue_analysis = AsyncMock(side_effect=body)
    first = asyncio.create_task(worker.process_issue_analysis(dict(payloads[0])))
    await asyncio.wait_for(started.wait(), 2)
    try:
        assert (
            await worker.process_issue_analysis(dict(payloads[0]))
            == payloads[0]["task_id"]
        )
        worker._run_issue_analysis.assert_awaited_once()
        assert (
            worker._task_handles[worker._make_task_key(payloads[0])][
                payloads[0]["task_id"]
            ]
            is first
        )
        async with factory() as db:
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            assert receipt.status == "processing"
            with pytest.raises(BillingError, match="Claimed Issue"):
                await reconcile_receipt(
                    db,
                    receipt.receipt_id,
                    actor_id=2,
                    resolution="cancelled_unstarted",
                    evidence="TEST no current Usage does not prove no execution",
                    reason="TEST cannot cancel a claimed Worker",
                )
            await db.rollback()
    finally:
        finish.set()
        await first
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        assert (
            await db.get(BillingOperation, receipt.operation_id)
        ).outcome == "completed"
        assert (
            await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        ).status == "cancelled"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_capacity_closes_prepared_row_and_reserve(
    reanalysis_app, monkeypatch
):
    from backend.workers import issue_worker as module

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    payloads = []

    async def submit(payload):
        payloads.append(dict(payload))
        return payload["task_id"]

    monkeypatch.setattr(module, "submit_issue_analysis_task", submit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(url("api"))).status_code == 200
    waiting = asyncio.Event()

    class CapacityWait:
        async def __aenter__(self):
            waiting.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            return False

    worker = module.IssueWorker.__new__(module.IssueWorker)
    worker._log_activity = AsyncMock()
    monkeypatch.setattr(module, "async_session", factory)
    monkeypatch.setattr(
        module, "_get_issue_semaphore", AsyncMock(return_value=CapacityWait())
    )
    monkeypatch.setattr("backend.webui.sse.publish_event", AsyncMock())
    running = asyncio.create_task(worker.process_issue_analysis(payloads[0]))
    await asyncio.wait_for(waiting.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        operation = await db.get(BillingOperation, receipt.operation_id)
        assert operation.outcome == "cancelled" and operation.reserve_units == 0
        row = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        assert row.status == "cancelled"
        assert (await db.get(IssueAnalysis, 1)).status == "completed"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_known_unregistered_refusal_closes_receipt_and_record_without_hold(
    reanalysis_app, monkeypatch
):
    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeAdmissionClosed,
    )

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    refused = AsyncMock(
        side_effect=DatabaseResetRuntimeAdmissionClosed(
            "TEST runtime paused before registration"
        )
    )
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", refused
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(url("api"))
        assert response.status_code == 503
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        row = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        assert row.status == "failed"
        assert (
            await db.get(BillingOperation, receipt.operation_id)
        ).outcome == "failed"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_real_worker_reuses_persisted_analysis_on_presend_failure(
    reanalysis_app, monkeypatch
):
    from backend.workers import issue_worker as module

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    captured = []

    async def submit(payload):
        captured.append(dict(payload))
        return payload["task_id"]

    monkeypatch.setattr(module, "submit_issue_analysis_task", submit)
    worker = module.IssueWorker.__new__(module.IssueWorker)
    worker.analyzer = SimpleNamespace(api_client=None)
    worker.activity_integration = SimpleNamespace(
        admit_issue=AsyncMock(side_effect=RuntimeError("TEST no observability"))
    )
    worker._log_activity = AsyncMock()
    worker.github_app = SimpleNamespace(
        get_repo_client=lambda *args: (_ for _ in ()).throw(
            RuntimeError("TEST stop before AI")
        )
    )
    monkeypatch.setattr(module, "async_session", factory)
    monkeypatch.setattr(
        module, "_get_issue_semaphore", AsyncMock(return_value=asyncio.Semaphore(1))
    )
    monkeypatch.setattr("backend.webui.sse.publish_event", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(url("api"))
        assert response.status_code == 200
    await worker.process_issue_analysis(captured[0])
    async with factory() as db:
        rows = (
            (await db.execute(select(IssueAnalysis).order_by(IssueAnalysis.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].status == "completed" and rows[1].status == "failed"
        assert rows[1].id == captured[0]["_reanalysis_analysis_id"]
        assert rows[1].analysis_version == 3
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_carrier_cancelled_before_first_worker_instruction_releases_admission(
    reanalysis_app, monkeypatch, closed
):
    from backend.services.issues.reanalysis_dispatch import durable_reanalysis_worker

    app, factory = reanalysis_app
    await seed(factory, funded=True)
    payloads = []

    async def submit(payload):
        payloads.append(dict(payload))
        return payload["task_id"]

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(url("api"))).status_code == 200
    async with factory() as db:
        row = await db.get(IssueAnalysis, payloads[0]["_reanalysis_analysis_id"])
        if closed:
            row.issue_state = "closed"
        else:
            row.status = "cancelled"
        await db.commit()
    body = AsyncMock()
    worker = SimpleNamespace(
        _task_handles={}, _make_task_key=lambda payload: "owner1/repo#7"
    )
    await durable_reanalysis_worker(body)(worker, payloads[0])
    body.assert_not_awaited()
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        assert (
            await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
        ).status == "cancelled"
        assert (
            await db.get(BillingOperation, receipt.operation_id)
        ).outcome == "cancelled"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
