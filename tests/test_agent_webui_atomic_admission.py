"""WebUI Agent admission and its persisted task carrier share one transaction."""

import httpx
import pytest
from sqlalchemy import select

from backend.models.agent_team_models import AgentTeamTask, AgentTeamUserPrompt
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    RateLimitAdmission,
)
from backend.models.telegram_models import QuotaUsageLog, TelegramUser
from backend.services.agent_team.candidate_service import CandidateServiceError
from backend.services.billing_service import BillingService
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME
from backend.webui.deps import get_db
from backend.webui.routes import agent_team as routes
from tests.test_billing_pricing_editor import pricing_app as app_fixture
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

pricing_app = app_fixture
sql_runtime = runtime_fixture


@pytest.fixture
def agent_app(pricing_app, monkeypatch):
    app, factory, token = pricing_app
    app.include_router(routes.router)

    async def allowed(*args, **kwargs):
        return None

    async def context(*args, **kwargs):
        return {"agent_task_context": ""}

    async def no_worker(*args, **kwargs):
        return None

    async def no_paid_subscriptions():
        return False

    async def draft(*args, **kwargs):
        return {
            "source_type": "manual_issue",
            "source_id": None,
            "source_issue_number": 5,
            "repo_full_name": "owner1/repo",
            "repo_owner": "owner1",
            "repo_name": "repo",
            "title": "TEST task",
            "summary": "TEST summary",
            "priority": "medium",
            "status": "queued",
        }

    monkeypatch.setattr(routes, "_check_repo_access", allowed)
    monkeypatch.setattr(routes, "_build_manual_issue_submission_context", context)
    monkeypatch.setattr(routes, "_run_agent_task_background", no_worker)
    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled", no_paid_subscriptions
    )
    monkeypatch.setattr(
        routes.AgentTeamCandidateService, "build_manual_issue_task_draft", draft
    )
    return app, factory, token


async def prepare(factory, *, funded=True):
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        user.agent_daily_quota = user.agent_weekly_quota = user.agent_monthly_quota = 10
        if funded:
            await BillingService(db).grant(1, "20", "TEST-agent-funding")
        await db.commit()


async def assert_no_admission(factory):
    async with factory() as db:
        assert (await db.execute(select(BillingOperation))).scalars().all() == []
        assert (await db.execute(select(RateLimitAdmission))).scalars().all() == []
        assert (await db.execute(select(QuotaUsageLog))).scalars().all() == []
        assert (await db.execute(select(AgentTeamTask))).scalars().all() == []
        user = await db.get(TelegramUser, 1)
        assert (
            user.agent_daily_used
            == user.agent_weekly_used
            == user.agent_monthly_used
            == 0
        )
        wallet = await db.get(BillingWallet, 1)
        assert wallet is None or wallet.reserved_units == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["form", "task_creation", "insufficient"])
async def test_failed_agent_creation_leaves_no_quota_operation_or_reserve(
    agent_app, monkeypatch, failure
):
    app, factory, token = agent_app
    await prepare(factory, funded=failure != "insufficient")
    if failure == "task_creation":

        async def unavailable(*args, **kwargs):
            raise CandidateServiceError("TEST unavailable GitHub metadata")

        monkeypatch.setattr(
            routes.AgentTeamCandidateService,
            "build_manual_issue_task_draft",
            unavailable,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "isolated"},
    ) as client:
        response = await client.post(
            "/agent-team/tasks/create-from-issue",
            headers={"x-test-role": "user"},
            data={
                "csrf_token": token,
                "issue_ref": "owner1/repo#5",
                "priority": "invalid" if failure == "form" else "medium",
            },
        )
    assert response.status_code == 200 and response.json()["success"] is False
    await assert_no_admission(factory)


@pytest.mark.asyncio
async def test_agent_creation_saves_trusted_carrier_and_consumes_quota_once(agent_app):
    app, factory, token = agent_app
    await prepare(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "isolated"},
    ) as client:
        response = await client.post(
            "/agent-team/tasks/create-from-issue",
            headers={"x-test-role": "user"},
            data={"csrf_token": token, "issue_ref": "owner1/repo#5"},
        )
    assert response.status_code == 200 and response.json()["success"] is True
    async with factory() as db:
        task = await db.get(AgentTeamTask, response.json()["task_id"])
        operation = await db.get(BillingOperation, task.billing_operation_id)
        assert task.billing_user_id == operation.user_id == 1
        assert (
            operation.feature == "agent"
            and operation.source["repo_full_name"] == task.repo_full_name
        )
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        rows = (await db.execute(select(RateLimitAdmission))).scalars().all()
        assert (
            len(rows) == 1
            and rows[0].repo_name == task.repo_full_name
            and rows[0].number == task.id
        )
        await BillingService(db).register_operation(
            1,
            task.billing_operation_id,
            "agent",
            source={"repo_full_name": task.repo_full_name, "task_id": task.id},
        )
        await db.commit()
    async with factory() as db:
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1
        assert len((await db.execute(select(QuotaUsageLog))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["candidate", "completed", "failed"])
async def test_non_scheduled_task_creation_does_not_take_balance_quota_or_user_slot(
    agent_app, status
):
    app, factory, token = agent_app
    await prepare(factory)
    async with factory() as db:
        db.add(
            LegacyEntitlement(
                user_id=1,
                source_key="TEST-single-slot",
                snapshot={"version": 2, "concurrency_limit": 1},
            )
        )
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "isolated"},
    ) as client:
        saved = await client.post(
            "/agent-team/tasks/create-from-issue",
            headers={"x-test-role": "user"},
            data={"csrf_token": token, "issue_ref": "owner1/repo#5", "status": status},
        )
        assert saved.status_code == 200 and saved.json()["success"] is True
        async with factory() as db:
            task = await db.get(AgentTeamTask, saved.json()["task_id"])
            assert task.status == status and task.started_by == "owner1"
            assert task.billing_operation_id is None and task.billing_user_id is None
            assert task.billing_platform_reason is None
            assert (await db.execute(select(BillingOperation))).scalars().all() == []
            wallet = await db.get(BillingWallet, 1)
            assert wallet.balance_units == 20_000_000 and wallet.reserved_units == 0
            assert (await db.get(TelegramUser, 1)).agent_daily_used == 0
            assert (await db.execute(select(RateLimitAdmission))).scalars().all() == []
        executed = await client.post(
            "/agent-team/tasks/create-from-issue",
            headers={"x-test-role": "user"},
            data={
                "csrf_token": token,
                "issue_ref": "owner1/repo#5",
                "status": "queued",
            },
        )
        assert executed.status_code == 200 and executed.json()["success"] is True
    async with factory() as db:
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == 1
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["retry", "followup"])
async def test_failed_reentry_commit_preserves_old_task_and_rolls_back_new_admission(
    agent_app, entry
):
    app, factory, token = agent_app
    await prepare(factory)
    original_status = "failed" if entry == "retry" else "completed"
    async with factory() as db:
        operation = await BillingService(db).register_operation(1, "TEST-old", "agent")
        await BillingService(db).finish_operation(operation.operation_id, "failed")
        task = AgentTeamTask(
            source_type="manual_issue",
            source_issue_number=5,
            repo_full_name="owner1/repo",
            repo_owner="owner1",
            repo_name="repo",
            title="TEST reentry",
            started_by="owner1",
            status=original_status,
            billing_operation_id="TEST-old",
            billing_user_id=1,
            workspace_path="/tmp/TEST-unused-workspace",
            branch_name="TEST-branch",
            pr_number=1,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    async def interrupted_db():
        async with factory() as db:

            async def fail_commit():
                raise RuntimeError("TEST storage interruption")

            db.commit = fail_commit
            yield db

    app.dependency_overrides[get_db] = interrupted_db
    path = (
        f"/agent-team/tasks/{task_id}/retry"
        if entry == "retry"
        else f"/agent-team/api/tasks/{task_id}/prompts"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        cookies={WEBUI_TOKEN_COOKIE_NAME: "isolated"},
    ) as client:
        response = await client.post(
            path,
            headers={"x-test-role": "user"},
            data={"csrf_token": token, "content": "TEST further work"},
        )
    assert response.status_code == 500
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        assert (
            task.status == original_status and task.billing_operation_id == "TEST-old"
        )
        operations = (await db.execute(select(BillingOperation))).scalars().all()
        assert len(operations) == 1 and operations[0].operation_id == "TEST-old"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
        assert (await db.get(TelegramUser, 1)).agent_daily_used == 0
        assert (await db.execute(select(RateLimitAdmission))).scalars().all() == []
        assert (await db.execute(select(AgentTeamUserPrompt))).scalars().all() == []
