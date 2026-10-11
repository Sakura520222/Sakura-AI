"""Manual queued admissions are recoverable before and after process handoff."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from backend.models.agent_team_models import AgentTeamTask
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.legacy_entitlement_models import RateLimitAdmission
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.agent_team.billing_admission import (
    AgentDeliveryClaim,
    claim_agent_delivery_worker,
    run_agent_delivery,
)
from backend.services.billing_service import BillingService
from backend.services.webhook_execution_reconciliation import reconcile_receipt
from backend.webui.routes import agent_team as routes
from tests.test_agent_webui_atomic_admission import agent_app as agent_fixture
from tests.test_agent_webui_atomic_admission import prepare
from tests.test_billing_pricing_editor import pricing_app as pricing_fixture
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

agent_app = agent_fixture
pricing_app = pricing_fixture
sql_runtime = runtime_fixture
actual_background = routes._run_agent_task_background


async def create(client, token):
    return await client.post(
        "/agent-team/tasks/create-from-issue",
        headers={"x-test-role": "user"},
        data={"csrf_token": token, "issue_ref": "owner1/repo#5"},
    )


@pytest.mark.asyncio
async def test_create_ack_persists_receipt_and_queued_retry_keeps_original_admission(
    agent_app,
):
    app, factory, token = agent_app
    await prepare(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await create(client, token)
        assert response.json()["success"]
        task_id = response.json()["task_id"]
        async with factory() as db:
            task = await db.get(AgentTeamTask, task_id)
            original_operation = task.billing_operation_id
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            assert (
                receipt.status == "retryable"
                and receipt.operation_id == original_operation
            )
            assert (
                receipt.source["task_id"] == task_id
                and receipt.source["origin"] == "webui"
            )
        retry = await client.post(
            f"/agent-team/tasks/{task_id}/retry",
            headers={"x-test-role": "user"},
            data={"csrf_token": token},
        )
        assert retry.json()["success"]
    async with factory() as db:
        assert (
            await db.get(AgentTeamTask, task_id)
        ).billing_operation_id == original_operation
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert len((await db.execute(select(RateLimitAdmission))).scalars().all()) == 1
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_dispatch_staging_interruption_rolls_back_task_quota_and_reserve(
    agent_app, monkeypatch
):
    app, factory, token = agent_app
    await prepare(factory)
    stage = routes.stage_manual_agent_dispatch

    async def interrupted(db, task, **kwargs):
        await stage(db, task, **kwargs)
        raise RuntimeError("TEST before dispatch transaction commit")

    monkeypatch.setattr(routes, "stage_manual_agent_dispatch", interrupted)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        assert (await create(client, token)).status_code == 500
    async with factory() as db:
        for model in (
            AgentTeamTask,
            BillingOperation,
            WebhookExecutionReceipt,
            RateLimitAdmission,
        ):
            assert (await db.execute(select(model))).scalars().all() == []
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 0
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_duplicate_background_claims_run_one_worker_and_keep_original_payer(
    agent_app, monkeypatch
):
    app, factory, token = agent_app
    await prepare(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await create(client, token)
        task_id = response.json()["task_id"]
        retried = await client.post(
            f"/agent-team/tasks/{task_id}/retry",
            headers={"x-test-role": "super_admin"},
            data={"csrf_token": token},
        )
        assert retried.json()["success"]
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        receipt_id = receipt.receipt_id
        task = await db.get(AgentTeamTask, task_id)
        assert task.billing_user_id == 1 and task.billing_platform_reason is None
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def worker(task_id):
        calls.append(task_id)
        async with factory() as db:
            assert await claim_agent_delivery_worker(db, task_id)
            await db.commit()
        entered.set()
        await finish.wait()
        async with factory() as db:
            task = await db.get(AgentTeamTask, task_id)
            await BillingService(db).finish_operation(
                task.billing_operation_id, "completed"
            )
            task.status = "completed"
            await db.commit()

    monkeypatch.setattr(
        routes, "register_current_background_task", lambda *args: object()
    )
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", worker
    )
    first = asyncio.create_task(actual_background(task_id, receipt_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await actual_background(task_id, receipt_id)
    finally:
        finish.set()
        await asyncio.wait_for(first, 2)
    assert calls == [task_id]
    async with factory() as db:
        assert (await db.get(WebhookExecutionReceipt, receipt_id)).status == "accepted"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1


@pytest.mark.asyncio
async def test_reviewed_unknown_handoff_releases_hold_and_fences_late_worker(
    agent_app, monkeypatch
):
    app, factory, token = agent_app
    await prepare(factory)
    monkeypatch.setattr(routes, "_run_agent_task_background", actual_background)
    monkeypatch.setattr(
        routes, "register_current_background_task", lambda *args: object()
    )
    submit = AsyncMock(side_effect=RuntimeError("TEST unknown dispatch"))
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await create(client, token)
        task_id = response.json()["task_id"]
    async with factory() as db:
        (await db.get(TelegramUser, 2)).role = "super_admin"
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        late = AgentDeliveryClaim(
            receipt.receipt_id, receipt.owner_token, receipt.operation_id, task_id
        )
        await reconcile_receipt(
            db,
            receipt.receipt_id,
            actor_id=2,
            resolution="cancelled_unstarted",
            evidence="TEST verified mock submit sent no upstream work",
            reason="Restore stopped dispatch",
        )
        await db.commit()
    late_submit = AsyncMock()
    await run_agent_delivery(late, late_submit, factory)
    late_submit.assert_not_awaited()
    async with factory() as db:
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (
            await db.get(BillingOperation, late.operation_id)
        ).outcome == "cancelled"
        assert (await db.get(AgentTeamTask, task_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_legacy_queued_admission_is_quarantined_without_new_financial_effect(
    agent_app,
):
    app, factory, token = agent_app
    await prepare(factory)
    async with factory() as db:
        task = AgentTeamTask(
            title="TEST upgrade queued",
            repo_full_name="owner1/repo",
            repo_owner="owner1",
            repo_name="repo",
            source_type="manual_issue",
            started_by="owner1",
            source_issue_number=5,
            billing_user_id=1,
            billing_operation_id="TEST-pre-receipt",
            status="queued",
        )
        db.add(task)
        await db.flush()
        await BillingService(db).register_operation(
            1,
            task.billing_operation_id,
            "agent",
            {"repo_full_name": task.repo_full_name, "task_id": task.id},
        )
        task_id = task.id
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        retry = await client.post(
            f"/agent-team/tasks/{task_id}/retry",
            headers={"x-test-role": "user"},
            data={"csrf_token": token},
        )
        assert (
            retry.status_code == 503
            and retry.json()["code"] == "agent_dispatch_pending"
        )
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert (
            receipt.status == "pending_reconciliation"
            and receipt.operation_id == "TEST-pre-receipt"
        )
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_registration_failure_remains_retryable_then_worker_claim_finishes_same_receipt(
    agent_app, monkeypatch
):
    app, factory, token = agent_app
    await prepare(factory)
    monkeypatch.setattr(routes, "_run_agent_task_background", actual_background)
    monkeypatch.setattr(routes, "register_current_background_task", lambda *args: None)
    submitted = AsyncMock()
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submitted
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await create(client, token)
        task_id = response.json()["task_id"]
        submitted.assert_not_awaited()
        async with factory() as db:
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            assert receipt.status == "retryable"
            original_operation = receipt.operation_id

        async def worker(task_id):
            async with factory() as db:
                assert await claim_agent_delivery_worker(db, task_id)
                await db.commit()
            async with factory() as db:
                task = await db.get(AgentTeamTask, task_id)
                await BillingService(db).finish_operation(
                    task.billing_operation_id, "completed"
                )
                task.status = "completed"
                await db.commit()

        monkeypatch.setattr(
            routes, "register_current_background_task", lambda *args: object()
        )
        monkeypatch.setattr(
            "backend.workers.agent_team_worker.submit_agent_team_task", worker
        )
        retry = await client.post(
            f"/agent-team/tasks/{task_id}/retry",
            headers={"x-test-role": "user"},
            data={"csrf_token": token},
        )
        assert retry.json()["success"]
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert (
            receipt.status == "accepted" and receipt.operation_id == original_operation
        )
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1


@pytest.mark.asyncio
async def test_uncertain_submission_keeps_receipt_and_refuses_fresh_queued_charge(
    agent_app, monkeypatch
):
    app, factory, token = agent_app
    await prepare(factory)
    monkeypatch.setattr(routes, "_run_agent_task_background", actual_background)
    monkeypatch.setattr(
        routes, "register_current_background_task", lambda *args: object()
    )
    submit = AsyncMock(side_effect=RuntimeError("TEST submission result unknown"))
    monkeypatch.setattr(
        "backend.workers.agent_team_worker.submit_agent_team_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await create(client, token)
        assert response.json()["success"]
        task_id = response.json()["task_id"]
        async with factory() as db:
            receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
            assert receipt.status == "pending_reconciliation"
            original_operation = receipt.operation_id
        replay = await client.post(
            f"/agent-team/tasks/{task_id}/retry",
            headers={"x-test-role": "user"},
            data={"csrf_token": token},
        )
        assert replay.status_code == 503 and not replay.json()["success"]
        assert replay.json()["code"] == "agent_dispatch_pending"
    submit.assert_awaited_once()
    async with factory() as db:
        assert (
            await db.get(AgentTeamTask, task_id)
        ).billing_operation_id == original_operation
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
