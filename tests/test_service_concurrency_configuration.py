"""Service execution capacity and per-user admission remain separate contracts."""

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from backend.api.v1.billing import PlanCreateRequest, PlanUpdateRequest
from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    Settings,
    get_all_dynamic_config_keys,
)
from backend.models.payment_models import Plan
from backend.services.billing_service import BillingError, BillingService
from backend.services.legacy_entitlement_service import LegacyEntitlementService
from backend.webui import deps
from backend.webui.routes.config import (
    _DynamicConfigValidationError,
    _validate_dynamic_config_value,
    unified_config_page,
)
from tests.test_billing_pricing_editor import pricing_app as pricing_fixture
from tests.test_billing_pricing_editor import seed_language
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture
pricing_app = pricing_fixture


@pytest.mark.parametrize(
    "key",
    ["max_concurrent_reviews", "max_concurrent_issues", "agent_team_max_concurrent"],
)
@pytest.mark.parametrize("value", [0, -1])
def test_service_capacity_rejects_nonpositive_settings_and_form_values(key, value):
    with pytest.raises(ValidationError):
        Settings(**{key: value})
    with pytest.raises(_DynamicConfigValidationError):
        _validate_dynamic_config_value(
            key,
            str(value),
            expected_type=int,
            ranges=DYNAMIC_CONFIG_RANGES,
            select_options=DYNAMIC_CONFIG_SELECT_OPTIONS,
        )


@pytest.mark.parametrize(
    "key,expected_type,invalid",
    [
        ("service_execution_lease_seconds", int, 29),
        ("service_execution_lease_seconds", int, 3601),
        ("service_execution_poll_seconds", float, 0.01),
        ("service_execution_poll_seconds", float, 61),
    ],
)
def test_shared_capacity_infrastructure_settings_have_matching_form_bounds(
    key, expected_type, invalid
):
    with pytest.raises(ValidationError):
        Settings(**{key: invalid})
    with pytest.raises(_DynamicConfigValidationError):
        _validate_dynamic_config_value(
            key,
            str(invalid),
            expected_type=expected_type,
            ranges=DYNAMIC_CONFIG_RANGES,
            select_options=DYNAMIC_CONFIG_SELECT_OPTIONS,
        )


def test_shared_capacity_settings_registered_once_without_agent_budget():
    keys = get_all_dynamic_config_keys()
    defaults = Settings()
    assert defaults.service_execution_lease_seconds == 300
    assert defaults.service_execution_poll_seconds == 0.25
    for key in ("service_execution_lease_seconds", "service_execution_poll_seconds"):
        assert keys.count(key) == 1
        assert key in DYNAMIC_CONFIG_GROUPS["review_basic"]["keys"]
    assert "agent_team_max_iterations" not in keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lang,labels,scope,queued",
    [
        (
            "zh-CN",
            ["PR 服务执行并发", "Issue 服务执行并发", "Agent 服务执行并发"],
            "共享同一数据库的 API/Worker 实例合计",
            "超出排队等待",
        ),
        (
            "en",
            [
                "PR service execution concurrency",
                "Issue service execution concurrency",
                "Agent service execution concurrency",
            ],
            "API/Worker instances sharing the same database",
            "excess tasks queue",
        ),
    ],
)
async def test_config_page_renders_service_scope_in_user_language(
    pricing_app, lang, labels, scope, queued
):
    _app, factory, _token = pricing_app
    request = Request(
        {"type": "http", "method": "GET", "path": "/config", "headers": []}
    )
    async with factory() as db:
        response = await unified_config_page(
            request,
            db=db,
            user={"sub": "owner1", "role": "super_admin", "user_id": 1},
            user_prefs={"language": lang},
        )
    html = response.body.decode()
    for label in labels:
        assert label in html
    assert html.count(scope) >= 3
    assert html.count(queued) >= 3
    assert 'name="agent_team_max_concurrent"' in html
    assert 'name="service_execution_lease_seconds"' in html
    assert 'name="service_execution_poll_seconds"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lang,label,help_text",
    [
        ("zh-CN", "每用户业务执行上限", "排队和运行均计入"),
        (
            "en",
            "Per-user business execution limit",
            "Queued and running executions count",
        ),
    ],
)
async def test_plan_pages_render_user_scope_and_lifecycle_help(
    pricing_app, monkeypatch, lang, label, help_text
):
    app, factory, _token = pricing_app
    await seed_language(factory, lang)

    async def enabled():
        return True

    monkeypatch.setattr(deps, "is_payment_enabled", enabled)
    async with factory() as db:
        db.add(
            Plan(
                name="Concurrency example",
                plan_type="one_time",
                price_cents=100,
                concurrency_limit=3,
            )
        )
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        admin = await client.get("/billing/admin/plans")
        assert admin.status_code == 200, admin.text
        assert label in admin.text
        assert help_text in admin.text
        assert "Repo Scan" in admin.text or "仓库扫描" in admin.text
        public = await client.get("/billing/")
        assert public.status_code == 200, public.text
        assert label not in public.text
        assert help_text not in public.text
        public_limit = (
            "同时任务上限：3" if lang == "zh-CN" else "Concurrent task limit: 3"
        )
        assert public_limit in public.text


def test_plan_api_schema_describes_per_user_cross_feature_scope():
    for schema in (PlanCreateRequest, PlanUpdateRequest):
        description = schema.model_json_schema()["properties"]["concurrency_limit"].get(
            "description", ""
        )
        assert "Per-user" in description
        assert "queued" in description and "terminal outcome" in description


@pytest.mark.asyncio
async def test_plan_counts_queued_running_and_unfinished_pending_across_features(
    sql_runtime,
):
    factory, _engine, _ = sql_runtime
    async with factory() as db:
        await LegacyEntitlementService(db).grant(
            1, {"version": 2, "concurrency_limit": 3}, "test:capacity:1"
        )
        await LegacyEntitlementService(db).grant(
            2, {"version": 2, "concurrency_limit": 3}, "test:capacity:2"
        )
        svc = BillingService(db, policy={"billing_enabled": False})
        queued = await svc.register_operation(1, "queued-pr", "pr_review")
        running = await svc.register_operation(1, "running-issue", "issue_analysis")
        await svc.start_call("running-issue", "actual-issue", "test", "model", "chat")
        pending = await svc.register_operation(1, "pending-agent", "agent")
        await svc.start_call("pending-agent", "actual-agent", "test", "model", "chat")
        pending = await svc.settle_operation("pending-agent")
        assert queued.status == "not_started"
        assert running.status == "running"
        assert pending.status == "pending_reconciliation" and pending.outcome is None
        await db.commit()
    async with factory() as db:
        svc = BillingService(db, policy={"billing_enabled": False})
        with pytest.raises(BillingError) as error:
            await svc.register_operation(1, "fourth-scan", "repo_scan")
        assert error.value.code == "concurrency_limit"
        await svc.register_operation(2, "other-user-agent", "agent")
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_terminal_outcome_releases_user_slot_while_bill_remains_pending(
    sql_runtime, outcome
):
    factory, _engine, _ = sql_runtime
    async with factory() as db:
        await LegacyEntitlementService(db).grant(
            1, {"version": 2, "concurrency_limit": 1}, "test:terminal-capacity"
        )
        svc = BillingService(db, policy={"billing_enabled": False})
        await svc.register_operation(1, "ended-agent", "agent")
        await svc.start_call("ended-agent", "unresolved-call", "test", "model", "chat")
        ended = await svc.finish_operation("ended-agent", outcome)
        assert ended.status == "pending_reconciliation" and ended.outcome == outcome
        await db.commit()
    async with factory() as db:
        svc = BillingService(db, policy={"billing_enabled": False})
        admitted = await svc.register_operation(1, "new-pr", "pr_review")
        assert admitted.operation_id == "new-pr"
        await db.commit()


@pytest.mark.asyncio
async def test_resuming_completed_execution_rechecks_user_capacity(sql_runtime):
    factory, _engine, _ = sql_runtime
    async with factory() as db:
        await LegacyEntitlementService(db).grant(
            1, {"version": 2, "concurrency_limit": 1}, "test:resume-capacity"
        )
        svc = BillingService(db, policy={"billing_enabled": False})
        await svc.register_operation(1, "previous-agent", "agent")
        await svc.finish_operation("previous-agent", "completed")
        await svc.register_operation(1, "current-pr", "pr_review")
        await db.commit()
    async with factory() as db:
        svc = BillingService(db, policy={"billing_enabled": False})
        with pytest.raises(BillingError) as error:
            await svc.resume_operation("previous-agent")
        assert error.value.code == "concurrency_limit"
