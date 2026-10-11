"""Unified model tariffs retain real stream attempts and immutable old fees."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingPriceProfile,
    BillingUsageCharge,
    BillingWallet,
)
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import (
    ProviderUsageMeter,
    finish_billing_operation,
)
from backend.services.billing_configuration_service import (
    validate_billing_configuration,
)
from backend.services.billing_context import BillingContext, bind_billing_context
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_credits_api import billing_client as client_fixture
from tests.test_billing_settlement import POLICY, PRICE
from tests.test_billing_usage_attribution import complete_usage, fund_and_price
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_billing_wallet import db as wallet_fixture

db = wallet_fixture
sql_runtime = runtime_fixture
billing_client = client_fixture


@pytest.mark.asyncio
async def test_stream_and_nonstream_meter_share_one_quote_but_keep_real_call_kind(
    sql_runtime,
):
    factory, engine, _ = sql_runtime
    await fund_and_price(factory)
    context = BillingContext(1, str(uuid4()), "agent", {"task_id": 4})
    with bind_billing_context(context):
        for kind in ("chat", "chat_stream"):
            async with ProviderUsageMeter(
                provider_id="provider",
                model_id="model",
                protocol_family="openai_compatible",
                call_kind=kind,
                role="agent",
                logical_call_id="same-operation-model-call",
            ) as meter:
                meter.usage = complete_usage()
        await finish_billing_operation("completed")
    with Session(engine) as session:
        usages = session.scalars(select(AIUsageRecord).order_by(AIUsageRecord.id)).all()
        attempts = session.scalars(select(BillingCallAttempt)).all()
        charges = session.scalars(select(BillingUsageCharge)).all()
        assert [row.call_kind for row in usages] == ["chat", "chat_stream"]
        assert {row.call_kind for row in attempts} == {"chat", "chat_stream"}
        assert len(charges) == 2
        assert len({row.price_profile_id for row in charges}) == 1
        assert {row.snapshot["call_kind"] for row in charges} == {"chat", "chat_stream"}
        assert {row.snapshot["pricing_call_kind"] for row in charges} == {"chat"}
        assert session.get(BillingWallet, 1).balance_units == 99_688_000


@pytest.mark.asyncio
async def test_old_api_stream_kind_publishes_next_unified_version(billing_client):
    client, sessions = billing_client
    headers = {"x-test-identity": "operator"}
    for kind in ("chat", "chat_stream"):
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            headers=headers,
            json={
                "provider_id": "p",
                "model_id": "m",
                "call_kind": kind,
                "config": TEST_PRICE,
            },
        )
        assert response.status_code == 200
    async with sessions() as session:
        profiles = (await session.execute(select(BillingPriceProfile))).scalars().all()
        assert [(row.call_kind, row.version) for row in profiles] == [
            ("chat", 1),
            ("chat", 2),
        ]


@pytest.mark.asyncio
async def test_activation_requires_only_one_model_tariff_per_account(db, monkeypatch):
    async def chain(role):
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    provider=SimpleNamespace(id="p"),
                    model=SimpleNamespace(model_id="m"),
                    account_id=account,
                )
                for account in ("primary", "fallback")
            ]
        )

    async def setting(key, **kwargs):
        return False if key == "enable_context_compression" else "none"

    monkeypatch.setattr(
        "backend.core.ai_protocol.role_config.resolve_role_from_config", chain
    )
    monkeypatch.setattr(
        "backend.services.billing_configuration_service.get_dynamic_config", setting
    )
    for account in ("primary", "fallback"):
        await BillingService(db).publish_price(
            "p", "m", "chat", PRICE, actor_id=1, account_id=account
        )
    await validate_billing_configuration(db, {"billing_enabled": True})


async def historic_stream(db, *, reported=True, kind="chat_stream", currency="USD"):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, "10", "historic:fund")
    await service.register_operation(1, "historic", "agent", {})
    profile = BillingPriceProfile(
        provider_id="p",
        model_id="m",
        call_kind=kind,
        version=1,
        config={**PRICE, "input_price": "3", "currency": currency},
        actor_id=1,
        account_id="account-a",
        scope_key="account:account-a",
    )
    db.add(profile)
    await db.flush()
    attempt = BillingCallAttempt(
        call_id="historic-stream",
        operation_id="historic",
        provider_id="p",
        model_id="m",
        call_kind="chat_stream",
        account_id="account-a",
        protocol_family="openai_compatible",
        price_profile_id=profile.id,
        state="usage_known" if reported else "pending_usage",
        usage_record_key="historic-usage",
    )
    db.add(attempt)
    db.add(
        AIUsageRecord(
            record_key="historic-usage",
            actual_call_id=attempt.call_id,
            operation_id="historic",
            user_id=1,
            feature="agent",
            role="agent_team",
            provider_id="p",
            model_id="m",
            account_id="account-a",
            call_kind="chat_stream",
            protocol_family="openai_compatible",
            input_tokens=1_000_000 if reported else None,
            output_tokens=0 if reported else None,
            usage_reported=reported,
            usage_semantics={"output_includes_reasoning": True},
            outcome="completed",
        )
    )
    await db.flush()
    return service, profile, attempt


@pytest.mark.asyncio
async def test_historic_stream_price_is_not_used_for_new_unified_calls(db):
    service, legacy, _ = await historic_stream(db)
    assert await service._price("p", "m", "chat", account_id="account-a") is None
    assert await service._price("p", "m", "chat_stream", account_id="account-a") is None
    with pytest.raises(BillingError) as error:
        await service.start_call(
            "historic", "new-stream", "p", "m", "chat_stream", account_id="account-a"
        )
    assert error.value.code == "missing_price"
    assert legacy.call_kind == "chat_stream" and legacy.config["input_price"] == "3"


@pytest.mark.asyncio
async def test_pinned_historic_stream_price_survives_unified_quote_and_reentry(db):
    service, legacy, attempt = await historic_stream(db)
    await service.publish_price(
        "p", "m", "chat", PRICE, actor_id=1, account_id="account-a"
    )
    result = await service.finish_operation("historic", "completed")
    assert result.settled_units == 3_000_000
    charge = (await db.execute(select(BillingUsageCharge))).scalar_one()
    original = dict(charge.snapshot)
    assert charge.price_profile_id == attempt.price_profile_id == legacy.id
    assert charge.provider_cost == "3"
    await service.settle_operation("historic")
    assert charge.snapshot == original
    actor = await db.get(TelegramUser, 1)
    actor.role = "super_admin"
    with pytest.raises(BillingError, match="immutable"):
        await resolve_billing_call(
            db,
            call_id=attempt.call_id,
            event_key="overwrite",
            actor_id=1,
            reason="reviewed invoice",
            usage=complete_usage(),
        )
    assert charge.snapshot == original and legacy.call_kind == "chat_stream"


@pytest.mark.asyncio
async def test_pending_stream_reconciliation_accepts_unified_quote_and_appends_usage(
    db,
):
    service, legacy, attempt = await historic_stream(db, reported=False)
    current = await service.publish_price(
        "p", "m", "chat", PRICE, actor_id=1, account_id="account-a"
    )
    actor = await db.get(TelegramUser, 1)
    actor.role = "super_admin"
    await service.finish_operation("historic", "completed")
    event = await resolve_billing_call(
        db,
        call_id=attempt.call_id,
        event_key="stream-invoice",
        actor_id=1,
        reason="reviewed invoice",
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
        price_profile_id=current.id,
    )
    charge = (await db.execute(select(BillingUsageCharge))).scalar_one()
    usages = (
        (await db.execute(select(AIUsageRecord).order_by(AIUsageRecord.id)))
        .scalars()
        .all()
    )
    assert (
        len(usages) == 2 and not usages[0].usage_reported and usages[1].usage_reported
    )
    assert all(row.call_kind == "chat_stream" for row in usages)
    assert charge.price_profile_id == current.id and charge.provider_cost == "1"
    assert event.evidence["original_price_profile_id"] == legacy.id
    assert legacy.call_kind == "chat_stream" and legacy.config["input_price"] == "3"


@pytest.mark.asyncio
async def test_pending_historic_stream_reconciliation_retains_original_price(db):
    service, legacy, attempt = await historic_stream(db, reported=False)
    await service.publish_price(
        "p", "m", "chat", PRICE, actor_id=1, account_id="account-a"
    )
    actor = await db.get(TelegramUser, 1)
    actor.role = "super_admin"
    await service.finish_operation("historic", "completed")
    evidence = {
        "call_id": attempt.call_id,
        "event_key": "historic-stream-invoice",
        "actor_id": 1,
        "reason": "reviewed invoice retaining originally agreed tariff",
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
    }
    first = await resolve_billing_call(db, **evidence)
    repeated = await resolve_billing_call(db, **evidence)
    charge = (await db.execute(select(BillingUsageCharge))).scalar_one()
    records = (await db.execute(select(AIUsageRecord))).scalars().all()
    assert first.id == repeated.id
    assert len(records) == 2
    assert (
        next(
            row for row in records if row.record_key == "historic-usage"
        ).usage_reported
        is False
    )
    assert charge.price_profile_id == legacy.id and charge.provider_cost == "3"
    assert charge.snapshot["price_profile_call_kind"] == "chat_stream"
    assert (await service.get_wallet(1)).balance_units == 7_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,account", [("embedding", "account-a"), ("chat", "account-b")]
)
async def test_unified_stream_reconciliation_still_rejects_wrong_kind_or_account(
    db, kind, account
):
    _, _, attempt = await historic_stream(db, reported=False)
    profile = await BillingService(db).publish_price(
        "p", "m", kind, PRICE, actor_id=1, account_id=account
    )
    actor = await db.get(TelegramUser, 1)
    actor.role = "super_admin"
    with pytest.raises(BillingError, match="exact matching"):
        await resolve_billing_call(
            db,
            call_id=attempt.call_id,
            event_key="wrong-scope",
            actor_id=1,
            reason="reviewed invoice",
            usage=complete_usage(),
            price_profile_id=profile.id,
        )
    assert attempt.usage_record_key == "historic-usage"


@pytest.mark.asyncio
async def test_pending_new_call_does_not_adopt_unpinned_legacy_stream_tariff(db):
    _, legacy, attempt = await historic_stream(db, reported=False)
    attempt.price_profile_id = None
    actor = await db.get(TelegramUser, 1)
    actor.role = "super_admin"
    with pytest.raises(BillingError, match="exact matching"):
        await resolve_billing_call(
            db,
            call_id=attempt.call_id,
            event_key="old-stream-quote",
            actor_id=1,
            reason="reviewed invoice",
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
            price_profile_id=legacy.id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["ZZZ", "BTC"])
async def test_unsupported_historic_currency_retains_usage_and_enters_pending_pricing(
    db, currency
):
    service, legacy, attempt = await historic_stream(db, currency=currency)
    original_config = dict(legacy.config)
    result = await service.finish_operation("historic", "completed")
    assert result.status == "pending_pricing"
    assert result.pending_reason == "Price configuration requires review"
    assert result.settled_units == 0 and result.reserve_units == 0
    assert (await service.get_wallet(1)).balance_units == 10_000_000
    assert (await db.execute(select(BillingUsageCharge))).scalar_one_or_none() is None
    original_usage = (
        await db.execute(
            select(AIUsageRecord).where(AIUsageRecord.record_key == "historic-usage")
        )
    ).scalar_one()
    assert original_usage.usage_reported and original_usage.input_tokens == 1_000_000
    assert attempt.price_profile_id == legacy.id and legacy.config == original_config
    with pytest.raises(ValueError, match="currency must be a supported currency code"):
        await service.publish_price(
            "p", "m", "chat", original_config, actor_id=1, account_id="account-a"
        )
