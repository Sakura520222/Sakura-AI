"""Reanalysis acknowledges only durable server-owned wallet admission."""

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from backend.api.v1 import issues as api
from backend.api.v1.deps import require_api_auth
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.database import IssueAnalysis, WebUIConfig
from backend.models.legacy_entitlement_models import LegacyEntitlement
from backend.services.billing_service import BillingService
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeAdmissionClosed,
)
from backend.webui.deps import get_db
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest.fixture
def reanalysis_app(sql_runtime):
    factory, _, _ = sql_runtime
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    async def db_dependency():
        async with factory() as db:
            yield db

    async def identity(request: Request):
        return {
            "user_id": 1,
            "sub": "owner1",
            "role": request.headers.get("x-test-role", "user"),
        }

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_api_auth] = identity
    return app, factory


async def seed(factory, *, funded=False, full=False, state="open"):
    async with factory() as db:
        db.add(
            IssueAnalysis(
                id=1,
                repo_owner="owner1",
                repo_name="repo",
                issue_number=7,
                author="owner1",
                title="TEST original",
                body="TEST body",
                status="completed",
                issue_state=state,
                analysis_version=2,
            )
        )
        db.add(WebUIConfig(user_id=1, language="en"))
        if funded:
            await BillingService(db).grant(1, "10", "TEST-fund")
        if full:
            db.add(
                LegacyEntitlement(
                    user_id=1,
                    source_key="TEST-plan",
                    snapshot={
                        "version": 2,
                        "credit_grant": "10",
                        "concurrency_limit": 1,
                    },
                )
            )
            await db.flush()
            await BillingService(db).register_operation(1, "TEST-active", "agent")
        await db.commit()


REANALYZE_URL = "/api/v1/issues/1/reanalyze"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,status,code",
    [
        ("balance", 402, "insufficient_credits"),
        ("concurrency", 429, "concurrency_limit"),
    ],
)
async def test_rejected_reanalysis_has_no_task_id_or_new_admission(
    reanalysis_app, monkeypatch, reason, status, code
):
    app, factory = reanalysis_app
    await seed(factory, funded=reason == "concurrency", full=reason == "concurrency")
    calls = []

    async def submit(payload):
        calls.append(payload)
        return "SHOULD-NOT-ACK"

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(REANALYZE_URL)
    assert response.status_code == status and response.json()["code"] == code
    assert "task_id" not in response.text and calls == []
    assert ("Insufficient" if reason == "balance" else "limit") in response.text
    async with factory() as db:
        record = await db.get(IssueAnalysis, 1)
        assert record.status == "completed" and record.analysis_version == 2
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == (
            1 if reason == "concurrency" else 0
        )
        wallet = await db.get(BillingWallet, 1)
        assert wallet is None or wallet.reserved_units == (
            1_000_000 if reason == "concurrency" else 0
        )


@pytest.mark.asyncio
async def test_accepted_task_is_admitted_before_queue_and_cannot_nominate_payer(
    reanalysis_app, monkeypatch
):
    app, factory = reanalysis_app
    await seed(factory, funded=True)
    captured = []

    async def submit(payload):
        context = payload["billing_context"]
        async with factory() as db:
            operation = await db.get(BillingOperation, context["operation_id"])
            assert operation.user_id == 1 and operation.feature == "issue_analysis"
            assert operation.source["repo_full_name"] == "owner1/repo"
            assert operation.source["issue_number"] == 7
            assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
            await BillingService(db).register_operation(
                1, context["operation_id"], "issue_analysis", source=context["source"]
            )
            await db.commit()
        captured.append(payload)
        return payload["task_id"]

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            REANALYZE_URL, json={"user_id": 2, "billing_user_id": 2}
        )
    assert response.status_code == 200 and response.json()["success"] is True
    assert len(captured) == 1 and captured[0]["analysis_version"] == 3
    async with factory() as db:
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_admin_platform_task_is_durable_without_credits(
    reanalysis_app, monkeypatch
):
    app, factory = reanalysis_app
    await seed(factory)

    async def submit(payload):
        context = payload["billing_context"]
        assert context["user_id"] is None
        assert context["platform_reason"] == "administrator_issue_reanalysis"
        assert context["source"]["trigger_user_id"] == 1
        async with factory() as db:
            assert (
                await db.get(BillingOperation, context["operation_id"])
            ).user_id is None
        return payload["task_id"]

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            REANALYZE_URL, headers={"x-test-role": "super_admin"}
        )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_failed_queue_is_not_acknowledged_and_unknown_handoff_is_preserved(
    reanalysis_app, monkeypatch, known
):
    app, factory = reanalysis_app
    await seed(factory, funded=True)

    async def submit(payload):
        if known:
            raise DatabaseResetRuntimeAdmissionClosed("TEST gate closed")
        raise RuntimeError("TEST-PRIVATE-SUBMIT-DETAIL")

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(REANALYZE_URL)
    assert response.status_code == 503 and response.json()["success"] is False
    assert "TEST-PRIVATE" not in response.text and "task_id" not in response.text
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        assert operation.outcome == ("failed" if known else None)
        assert (await db.get(BillingWallet, 1)).reserved_units == (
            0 if known else 1_000_000
        )
        if not known:
            assert operation.status == "pending_reconciliation"
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_financial_commit_failure_cannot_ack_or_enqueue(
    reanalysis_app, monkeypatch
):
    app, factory = reanalysis_app
    await seed(factory, funded=True)
    calls = []

    async def broken_db():
        async with factory() as db:

            async def commit():
                raise RuntimeError("TEST-PRIVATE-COMMIT-DETAIL")

            db.commit = commit
            yield db

    async def submit(payload):
        calls.append(payload)
        return "MUST-NOT-ACK"

    app.dependency_overrides[get_db] = broken_db
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(REANALYZE_URL)
    assert response.status_code == 503 and response.json()["success"] is False
    assert "TEST-PRIVATE" not in response.text and calls == []
    async with factory() as db:
        assert (await db.execute(select(BillingOperation))).scalars().all() == []
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_late_worker_after_lost_ack_is_not_blocked_as_unknown_usage(
    reanalysis_app, sql_runtime, monkeypatch
):
    from tests.test_billing_settlement import PRICE

    app, factory = reanalysis_app
    _, _, policy = sql_runtime
    policy["billing_charge_failed_calls"] = True
    await seed(factory, funded=True)
    captured = []

    async def submit(payload):
        captured.append(payload)
        raise RuntimeError("TEST lost acknowledgement")

    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(REANALYZE_URL)
    assert response.status_code == 503
    async with factory() as db:
        service = BillingService(db)
        await service.publish_price("p1", "m1", "chat", PRICE, actor_id=1)
        call = await service.start_call(
            captured[0]["billing_context"]["operation_id"],
            "TEST-late-call",
            "p1",
            "m1",
            "chat",
        )
        assert call.call_id == "TEST-late-call"
