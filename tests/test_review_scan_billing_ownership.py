"""Queued scan financial ownership survives later account-role and switch changes."""

import pytest

from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.scan_models import RepoScan
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import ProviderUsageMeter
from backend.services.billing_context import billable_record, get_billing_context
from backend.workers.scan_worker import ScanWorker
from tests.test_billing_usage_attribution import complete_usage, fund_and_price
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_role", ["admin", "super_admin", "user"])
async def test_scan_submission_freezes_payer_before_role_changes(
    sql_runtime, initial_role
):
    factory, _, policy = sql_runtime
    await fund_and_price(factory)
    async with factory() as db:
        (await db.get(TelegramUser, 1)).role = initial_role
        await db.commit()
    scan_id = await ScanWorker.__new__(ScanWorker).create_scan_record(
        "Acme/repo", "manual", "api:1"
    )
    async with factory() as db:
        scan = await db.get(RepoScan, scan_id)
        operation_id = scan.billing_operation_id
        operation = await db.get(BillingOperation, operation_id)
        payer = 1 if initial_role == "user" else None
        assert operation.user_id == payer
        assert operation.platform_reason == (
            None if payer else "administrator_repo_scan"
        )
        (await db.get(TelegramUser, 1)).role = "super_admin" if payer else "user"
        await db.commit()
    policy["billing_enabled"] = False
    contexts = []

    class Worker:
        @billable_record("repo_scan")
        async def run(self, record_id):
            contexts.append(get_billing_context())
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai-compatible",
                call_kind="chat",
                role="main",
                logical_call_id="scan-call",
            ) as meter:
                meter.usage = complete_usage()

    await Worker().run(scan_id)
    assert contexts[0].user_id == payer
    assert contexts[0].operation_id == operation_id
    async with factory() as db:
        operation = await db.get(BillingOperation, operation_id)
        assert operation.user_id == payer
        assert operation.settled_units == (156_000 if payer else 0)
        assert (await db.get(BillingWallet, 1)).balance_units == (
            99_844_000 if payer else 100_000_000
        )


@pytest.mark.asyncio
async def test_legacy_scan_without_admission_does_not_guess_payer_from_current_role(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    async with factory() as db:
        db.add(
            RepoScan(
                id=646,
                repo_name="Acme/repo",
                repo_owner="Acme",
                trigger_type="manual",
                triggered_by="api:1",
            )
        )
        await db.commit()
    contexts = []

    class Worker:
        @billable_record("repo_scan")
        async def run(self, record_id):
            contexts.append(get_billing_context())

    await Worker().run(646)
    assert contexts[0].user_id is None
    assert contexts[0].platform_reason == "legacy_repo_scan_without_verified_payer"
