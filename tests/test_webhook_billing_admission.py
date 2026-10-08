"""Verified delivery replay and manual-review admission against persisted SQL."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.api import webhook
from backend.models.billing_models import BillingOperation, BillingWallet
from backend.models.database import PRReview, PRStatus
from backend.models.legacy_entitlement_models import LegacyEntitlement
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_context import context_for_payload
from backend.services.billing_service import BillingService
from backend.services.webhook_execution_service import (
    mark_delivery_side_effects,
    verified_delivery,
)
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_webhook_incremental_queue import _payload as pr_payload

sql_runtime = runtime_fixture


@pytest.fixture
def webhook_runtime(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime
    monkeypatch.setattr(webhook, "get_async_session", factory)
    monkeypatch.setattr(webhook.settings, "enable_auto_review", True)
    monkeypatch.setattr(webhook.settings, "bot_username", "sakura-bot[bot]")
    submitted, quota, lifecycle = [], [], []

    class Users:
        def __init__(self, db):
            self.db = db

        async def get_user_by_github_username(self, name):
            return await self.db.get(TelegramUser, 1)

        async def check_and_consume_quota(self, **kwargs):
            quota.append(kwargs)
            return True, ""

        async def check_and_consume_issue_quota(self, **kwargs):
            quota.append(kwargs)
            return True, ""

        async def check_and_consume_agent_quota(self, **kwargs):
            quota.append(kwargs)
            return True, ""

    async def submit(info):
        submitted.append(dict(info))
        return "owner1/repo#7"

    async def marked(info):
        lifecycle.append(dict(info))

    async def config(name, **kwargs):
        return name == "enable_issue_analysis"

    monkeypatch.setattr(webhook, "TelegramService", Users)
    monkeypatch.setattr(webhook, "submit_review_task", submit)
    monkeypatch.setattr(webhook, "_mark_agent_task_external_reviewing", marked)
    monkeypatch.setattr(webhook, "get_dynamic_config", config)
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    return factory, submitted, quota, lifecycle


def automatic_pr(action="opened"):
    payload = pr_payload(action)
    payload["repository"]["owner"]["login"] = "owner1"
    payload["repository"]["full_name"] = "owner1/repo"
    return payload


def automatic_issue(action="opened", delivery="issue-delivery"):
    return {
        "action": action,
        "_sakura_delivery_id": delivery,
        "issue": {
            "id": 8,
            "number": 8,
            "title": "Issue",
            "body": "Description",
            "user": {"login": "owner1"},
            "state": "open",
            "html_url": "https://github.com/owner1/repo/issues/8",
        },
        "repository": {
            "name": "repo",
            "full_name": "owner1/repo",
            "owner": {"login": "owner1"},
        },
        "installation": {"id": 123},
        "sender": {"login": "owner1"},
    }


async def completed(factory, feature, delivery, number):
    info = {
        "user_id": 1,
        "delivery_id": delivery,
        "repo_full_name": "owner1/repo",
        "pr_number" if feature == "pr_review" else "issue_number": number,
    }
    context = context_for_payload(info, feature)
    async with factory() as db:
        service = BillingService(db)
        await service.grant(1, "20", "test:fund")
        await service.register_operation(
            1, context.operation_id, feature, source=dict(context.source)
        )
        await service.finish_operation(context.operation_id, "completed")
        await db.commit()
    return context.operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opened", "reopened"])
async def test_completed_pr_delivery_is_noop_before_quota_or_queue(
    webhook_runtime, action
):
    factory, submitted, quota, lifecycle = webhook_runtime
    operation_id = await completed(factory, "pr_review", "pr-delivery", 7)
    response = await webhook.handle_pull_request_event(
        automatic_pr(action), delivery_id="pr-delivery"
    )
    assert json.loads(response.body)["status"] == "deduplicated"
    assert submitted == quota == lifecycle == []
    async with factory() as db:
        assert (await db.get(BillingOperation, operation_id)).outcome == "completed"
    new = await webhook.handle_pull_request_event(
        automatic_pr(action), delivery_id="new-pr-delivery"
    )
    assert json.loads(new.body)["status"] == "accepted"
    assert len(submitted) == len(quota) == 1
    assert submitted[0]["billing_context"]["operation_id"] != operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opened", "reopened"])
async def test_completed_issue_delivery_is_noop_before_lifecycle_or_quota(
    webhook_runtime, monkeypatch, action
):
    factory, submitted, quota, _ = webhook_runtime
    await completed(factory, "issue_analysis", "issue-delivery", 8)

    async def no_reopen(*args, **kwargs):
        raise AssertionError("A completed replay must not reopen old analysis")

    monkeypatch.setattr(
        "backend.services.issue_service.issue_service.mark_issue_reopened", no_reopen
    )
    response = await webhook.handle_issue_event(automatic_issue(action))
    assert json.loads(response.body)["status"] == "deduplicated"
    assert submitted == quota == []


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_automatic_admin_keeps_language_identity_but_platform_pays(
    webhook_runtime, feature
):
    factory, submitted, _, _ = webhook_runtime
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        user.role = "super_admin"
        await db.commit()
    response = (
        await webhook.handle_pull_request_event(automatic_pr())
        if feature == "pr_review"
        else await webhook.handle_issue_event(automatic_issue(delivery=None))
    )
    assert json.loads(response.body)["status"] == "accepted"
    info = submitted[0]
    assert info["user_id"] == 1
    assert info["billing_user_id"] is None
    context = context_for_payload(info, feature)
    assert context.user_id is None
    assert context.platform_reason == f"administrator_automatic_{feature}"
    assert context.source["trigger_user_id"] == 1


class GitHubBoundary:
    def __init__(self):
        self.side_effects = []
        self.fail_cleanup = False
        self.pr = SimpleNamespace(
            id=7,
            number=7,
            user=SimpleNamespace(login="owner1"),
            title="PR",
            body="",
            head=SimpleNamespace(ref="feature", sha="head"),
            base=SimpleNamespace(ref="main"),
            diff_url="",
            patch_url="",
            html_url="https://github.com/owner1/repo/pull/7",
            state="open",
            draft=False,
            merged=False,
            create_issue_comment=lambda text: None,
        )

    def check_collaborator_permission(self, *args):
        return "write"

    def get_repo_client(self, *args):
        return self

    def get_repo(self, name):
        return self

    def get_pull(self, number):
        return self.pr

    def get_bot_username(self, *args):
        return "sakura-bot[bot]"

    def delete_all_bot_comments(self, *args):
        self.side_effects.append("delete_comments")
        if self.fail_cleanup:
            raise RuntimeError("Boundary cleanup failed")
        return {"issue_comments": 1, "review_comments": 1}

    def dismiss_bot_reviews(self, *args):
        self.side_effects.append("dismiss_reviews")
        return 1


def full_review_payload():
    payload = automatic_issue(delivery=None)
    payload["action"] = "created"
    payload["comment"] = {"body": "/full-review", "user": {"login": "owner1"}}
    payload["issue"]["number"] = 7
    payload["issue"]["pull_request"] = {"url": "https://github.com/owner1/repo/pull/7"}
    return payload


async def old_success(factory):
    async with factory() as db:
        review = PRReview(
            pr_id=7,
            pr_number=7,
            repo_owner="owner1",
            repo_name="repo",
            author="owner1",
            title="Saved success",
            status=PRStatus.COMPLETED,
            strategy="standard",
        )
        db.add(review)
        await db.commit()
        return review.id


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["balance", "concurrency"])
async def test_full_review_admission_rejection_preserves_old_success(
    webhook_runtime, monkeypatch, blocked
):
    factory, submitted, _, _ = webhook_runtime
    github = GitHubBoundary()
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: github)
    review_id = await old_success(factory)
    if blocked == "concurrency":
        async with factory() as db:
            service = BillingService(db)
            await service.grant(1, "20", "test:fund")
            db.add(
                LegacyEntitlement(
                    user_id=1,
                    source_key="plan:1",
                    snapshot={"version": 2, "concurrency_limit": 1},
                )
            )
            await service.register_operation(1, "other-feature", "agent")
            await db.commit()
    response = await webhook.handle_issue_comment_event(full_review_payload())
    body = json.loads(response.body)
    assert body["reason"] == (
        "insufficient_credits" if blocked == "balance" else "concurrency_limit"
    )
    assert github.side_effects == [] and submitted == []
    async with factory() as db:
        assert (await db.get(PRReview, review_id)).status == PRStatus.COMPLETED
        assert len((await db.execute(select(BillingOperation))).scalars().all()) == (
            1 if blocked == "concurrency" else 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cleanup", "queue"])
async def test_full_review_no_call_failure_releases_financial_admission(
    webhook_runtime, monkeypatch, failure
):
    factory, _, _, _ = webhook_runtime
    github = GitHubBoundary()
    github.fail_cleanup = failure == "cleanup"
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: github)
    async with factory() as db:
        await BillingService(db).grant(1, "20", "test:fund")
        await db.commit()

    async def broken_queue(info):
        from backend.services.database_reset_runtime_service import (
            DatabaseResetRuntimeAdmissionClosed,
        )

        raise DatabaseResetRuntimeAdmissionClosed("Boundary queue did not schedule")

    monkeypatch.setattr(webhook, "submit_review_task", broken_queue)
    response = await webhook.handle_issue_comment_event(full_review_payload())
    assert response.status_code == 500
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        assert operation.outcome == "failed" and operation.reserve_units == 0
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_unknown_full_review_queue_handoff_keeps_pending_admission(
    webhook_runtime, monkeypatch
):
    factory, _, _, _ = webhook_runtime
    monkeypatch.setattr(webhook, "GitHubAppClient", GitHubBoundary)
    async with factory() as db:
        await BillingService(db).grant(1, "20", "test:fund")
        await db.commit()

    async def unknown_queue(info):
        raise TimeoutError("Handoff acknowledgement lost")

    monkeypatch.setattr(webhook, "submit_review_task", unknown_queue)
    payload = full_review_payload()
    payload["_sakura_delivery_id"] = "unknown-manual-handoff"
    response = await webhook.handle_issue_comment_event(payload)
    assert response.status_code == 500
    replay = await webhook.handle_issue_comment_event(payload)
    assert replay.status_code == 503
    assert json.loads(replay.body)["reason"] == "delivery_pending_reconciliation"
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        assert operation.outcome is None and operation.reserve_units == 1_000_000
        assert (await db.get(BillingWallet, 1)).reserved_units == 1_000_000


@pytest.mark.asyncio
async def test_delivery_receipt_fences_parallel_handlers_and_replays_acknowledgement(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        calls.append(payload)
        entered.set()
        await release.wait()
        return webhook.JSONResponse(content={"status": "accepted", "task_key": "saved"})

    payload = automatic_pr()
    payload["_sakura_delivery_id"] = "parallel-delivery"
    first = asyncio.create_task(handler(payload))
    await entered.wait()
    concurrent = await handler(payload)
    assert concurrent.status_code == 503
    assert json.loads(concurrent.body)["reason"] == "delivery_processing"
    release.set()
    assert (await first).status_code == 200
    replay = await handler(payload)
    assert json.loads(replay.body) == {
        "status": "deduplicated",
        "original_status": "accepted",
        "task_key": "saved",
    }
    assert len(calls) == 1
    async with factory() as db:
        receipt = (await db.execute(select(WebhookExecutionReceipt))).scalar_one()
        assert receipt.status == "accepted"
        assert receipt.response["task_key"] == "saved"


@pytest.mark.asyncio
async def test_delivery_receipt_binds_body_and_feature_and_remembers_skips(sql_runtime):
    factory, _, _ = sql_runtime
    calls = []

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        calls.append(payload)
        return webhook.JSONResponse(content={"status": "skipped", "reason": "disabled"})

    payload = automatic_pr()
    payload["_sakura_delivery_id"] = "same-delivery"
    await handler(payload)
    replay = await handler(payload)
    assert json.loads(replay.body)["original_status"] == "skipped"
    changed = {**payload, "action": "reopened"}
    assert (await handler(changed)).status_code == 409
    assert len(calls) == 1

    @verified_delivery("issue_analysis", factory)
    async def issue(payload):
        return webhook.JSONResponse(content={"status": "accepted"})

    assert (await issue(automatic_issue(delivery="same-delivery"))).status_code == 200
    async with factory() as db:
        receipts = (await db.execute(select(WebhookExecutionReceipt))).scalars().all()
        assert {row.feature for row in receipts} == {"pr_review", "issue_analysis"}


@pytest.mark.asyncio
async def test_unknown_external_window_is_pending_until_durable_operation_ends(
    sql_runtime,
):
    factory, _, _ = sql_runtime
    calls = []

    @verified_delivery("pr_review", factory)
    async def handler(payload):
        calls.append(payload)
        await mark_delivery_side_effects()
        return webhook.JSONResponse(status_code=500, content={"status": "error"})

    payload = automatic_pr()
    payload["_sakura_delivery_id"] = "uncertain-delivery"
    assert (await handler(payload)).status_code == 500
    replay = await handler(payload)
    assert replay.status_code == 503
    assert json.loads(replay.body)["reason"] == "delivery_pending_reconciliation"
    assert len(calls) == 1
    await completed(factory, "pr_review", "uncertain-delivery", 7)
    assert json.loads((await handler(payload)).body)["status"] == "deduplicated"
    assert len(calls) == 1
    async with factory() as db:
        assert (
            await db.execute(select(WebhookExecutionReceipt))
        ).scalar_one().status == "accepted"


@pytest.mark.asyncio
async def test_legacy_active_operation_is_not_scheduled_again(webhook_runtime):
    factory, submitted, quota, _ = webhook_runtime
    info = {
        "user_id": 1,
        "delivery_id": "old-active",
        "repo_full_name": "owner1/repo",
        "pr_number": 7,
    }
    context = context_for_payload(info, "pr_review")
    async with factory() as db:
        service = BillingService(db)
        await service.grant(1, "20", "test:fund")
        await service.register_operation(
            1, context.operation_id, "pr_review", source=dict(context.source)
        )
        await db.commit()
    response = await webhook.handle_pull_request_event(
        automatic_pr(), delivery_id="old-active"
    )
    assert response.status_code == 503
    assert json.loads(response.body)["reason"] == "delivery_pending_reconciliation"
    assert submitted == quota == []


@pytest.mark.asyncio
@pytest.mark.parametrize("administrator", [False, True])
async def test_agent_carrier_commits_with_admission_even_if_caller_crashes(
    sql_runtime, monkeypatch, administrator
):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.services.telegram_service import TelegramService

    factory, _, _ = sql_runtime
    monkeypatch.setattr(webhook, "get_async_session", factory)
    monkeypatch.setattr(webhook, "TelegramService", TelegramService)

    async def payment_disabled():
        return False

    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled", payment_disabled
    )
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        user.role = "super_admin" if administrator else "user"
        user.agent_daily_quota = user.agent_weekly_quota = user.agent_monthly_quota = 10
        task = AgentTeamTask(
            title="Task",
            repo_owner="owner1",
            repo_name="repo",
            repo_full_name="owner1/repo",
            source_type="manual_issue",
            started_by="collaborator",
            status="queued",
        )
        db.add(task)
        await BillingService(db).grant(1, "20", "test:fund")
        await db.commit()
        task_id = task.id
    consume = TelegramService.check_and_consume_agent_quota

    async def stopped_after_commit(self, *args, **kwargs):
        result = await consume(self, *args, **kwargs)
        assert result[0]
        raise RuntimeError("Isolated crash after committed admission")

    monkeypatch.setattr(
        TelegramService, "check_and_consume_agent_quota", stopped_after_commit
    )
    with pytest.raises(RuntimeError, match="Isolated crash"):
        await webhook._consume_agent_quota_or_cleanup(
            GitHubBoundary(), "owner1", "repo", "owner1/repo", task_id, 8
        )
    async with factory() as db:
        task = await db.get(AgentTeamTask, task_id)
        operation = await db.get(BillingOperation, task.billing_operation_id)
        assert (
            operation.user_id == task.billing_user_id == (None if administrator else 1)
        )
        assert (
            operation.platform_reason
            == task.billing_platform_reason
            == ("administrator_webhook_agent" if administrator else None)
        )
        assert task.started_by == "collaborator"


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["pr_review", "issue_analysis"])
async def test_known_closed_queue_releases_automatic_admission(
    webhook_runtime, monkeypatch, feature
):
    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeAdmissionClosed,
    )
    from backend.services.telegram_service import TelegramService

    factory, _, _, _ = webhook_runtime
    monkeypatch.setattr(webhook, "TelegramService", TelegramService)

    async def payment_disabled():
        return False

    monkeypatch.setattr(
        "backend.services.telegram_service.is_payment_enabled", payment_disabled
    )
    async with factory() as db:
        user = await db.get(TelegramUser, 1)
        for prefix in ("", "issue_"):
            for period in ("daily", "weekly", "monthly"):
                setattr(user, f"{prefix}{period}_quota", 10)
        await BillingService(db).grant(1, "20", "test:fund")
        await db.commit()

    async def closed(info):
        raise DatabaseResetRuntimeAdmissionClosed(
            "Isolated queue closed before scheduling"
        )

    monkeypatch.setattr(webhook, "submit_review_task", closed)
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", closed
    )
    response = (
        await webhook.handle_pull_request_event(automatic_pr(), delivery_id="closed-pr")
        if feature == "pr_review"
        else await webhook.handle_issue_event(automatic_issue(delivery="closed-issue"))
    )
    assert response.status_code == 500
    async with factory() as db:
        operation = (await db.execute(select(BillingOperation))).scalar_one()
        assert operation.outcome == "failed" and operation.reserve_units == 0
        assert (await db.get(BillingWallet, 1)).reserved_units == 0


@pytest.mark.asyncio
async def test_known_unqueued_agent_fails_carrier_and_releases_reserve_atomically(
    sql_runtime,
):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.services.webhook_execution_service import compensate_unstarted_agent

    factory, _, _ = sql_runtime
    async with factory() as db:
        task = AgentTeamTask(
            title="Unqueued",
            repo_owner="owner1",
            repo_name="repo",
            repo_full_name="owner1/repo",
            source_type="manual_issue",
            status="queued",
            billing_operation_id="unqueued-agent",
            billing_user_id=1,
        )
        db.add(task)
        service = BillingService(db)
        await service.grant(1, "20", "test:fund")
        await service.register_operation(1, "unqueued-agent", "agent")
        await db.commit()
        task_id = task.id
    await compensate_unstarted_agent(task_id, factory)
    async with factory() as db:
        assert (await db.get(AgentTeamTask, task_id)).status == "failed"
        assert (await db.get(BillingOperation, "unqueued-agent")).outcome == "failed"
        assert (await db.get(BillingWallet, 1)).reserved_units == 0
