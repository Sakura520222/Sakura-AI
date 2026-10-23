"""Unknown webhook handoffs require reviewed, audited operator decisions."""

import json

import pytest
from sqlalchemy import select

from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.service_execution_models import ServiceExecutionOwnership
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_service import BillingError, BillingService
from backend.services.webhook_execution_reconciliation import (
    list_pending_receipts,
    reconcile_receipt,
)
from backend.services.webhook_execution_service import (
    _claim,
    _delivery_claim,
    mark_delivery_side_effects,
)
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_webhook_billing_admission import automatic_pr

sql_runtime = runtime_fixture


async def pending(factory):
    payload = automatic_pr()
    payload["_sakura_delivery_id"] = "reviewed-pending"
    claim = await _claim(payload, "pr_review", "reviewed-pending", factory)
    token = _delivery_claim.set(claim)
    try:
        await mark_delivery_side_effects()
    finally:
        _delivery_claim.reset(token)
    async with factory() as db:
        actor = await db.get(TelegramUser, 2)
        actor.role = "super_admin"
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        service = BillingService(db)
        await service.grant(1, "20", "fixture:fund")
        await service.register_operation(
            1,
            receipt.operation_id,
            receipt.feature,
            source={"repo_full_name": "owner1/repo", "pr_number": 7},
        )
        await db.commit()
    return claim


@pytest.mark.asyncio
async def test_receipt_recovery_dry_run_preserves_state_and_balance(sql_runtime):
    factory, _, _ = sql_runtime
    claim = await pending(factory)
    async with factory() as db:
        report = await list_pending_receipts(db, limit=1)
        assert report[0]["receipt_id"] == claim.receipt_id
        await db.rollback()
    async with factory() as db:
        assert (
            await db.get(WebhookExecutionReceipt, claim.receipt_id)
        ).status == "pending_reconciliation"
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        assert (await db.execute(select(AdminActionLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_operator_verified_unstarted_cancel_releases_reserve_and_audits_once(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    claim = await pending(factory)
    for _ in range(2):
        async with factory() as db:
            receipt = await reconcile_receipt(
                db,
                claim.receipt_id,
                actor_id=2,
                resolution="cancelled_unstarted",
                evidence="Isolated fixture confirms no queue handoff",
                reason="Failed handoff",
            )
            await db.commit()
            assert receipt.owner_token != claim.owner_token
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        assert (
            receipt.status == "accepted" and receipt.response["status"] == "cancelled"
        )
        assert (
            await db.get(BillingOperation, receipt.operation_id)
        ).outcome == "cancelled"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        logs = (await db.execute(select(AdminActionLog))).scalars().all()
        assert len(logs) == 1
        assert json.loads(logs[0].detail)["resolution"] == "cancelled_unstarted"


@pytest.mark.asyncio
async def test_receipt_recovery_rejects_user_and_unproven_terminal(sql_runtime):
    factory, _, _ = sql_runtime
    claim = await pending(factory)
    async with factory() as db:
        with pytest.raises(BillingError, match="super-admin"):
            await reconcile_receipt(
                db,
                claim.receipt_id,
                actor_id=1,
                resolution="accepted",
                evidence="Fixture",
                reason="Fixture",
            )
        with pytest.raises(BillingError, match="terminal"):
            await reconcile_receipt(
                db,
                claim.receipt_id,
                actor_id=2,
                resolution="terminal",
                evidence="Fixture",
                reason="Fixture",
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_receipt_recovery_never_cancels_live_worker_as_unstarted(sql_runtime):
    from datetime import timedelta

    from backend.core.time_service import now_utc

    factory, _, _ = sql_runtime
    claim = await pending(factory)
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        db.add(
            ServiceExecutionOwnership(
                token="live-owner",
                feature="pr_review",
                operation_id=receipt.operation_id,
                owner_id="isolated-owner",
                state="queued",
                expires_at=now_utc() + timedelta(hours=1),
            )
        )
        await db.commit()
        with pytest.raises(BillingError, match="live execution"):
            await reconcile_receipt(
                db,
                claim.receipt_id,
                actor_id=2,
                resolution="cancelled_unstarted",
                evidence="Fixture",
                reason="Fixture",
            )
        await db.rollback()
    async with factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        assert receipt.status == "pending_reconciliation"
        assert (await db.get(BillingOperation, receipt.operation_id)).outcome is None
