"""Account-scoped tariffs against real persisted SQL, without paid probes."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DatabaseError

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingPriceProfile, BillingUsageCharge
from backend.models.billing_schema import ensure_billing_schema
from backend.models.database import _build_add_column_sql
from backend.services.billing_configuration_service import (
    validate_billing_configuration,
)
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_settlement import POLICY, PRICE
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


@pytest.mark.asyncio
async def test_same_provider_model_prices_are_isolated_by_actual_account(db):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, 10, "account-pricing:fund")
    first = await service.publish_price(
        "openai", "same-model", "chat", PRICE, actor_id=1, account_id="direct"
    )
    second = await service.publish_price(
        "openai",
        "same-model",
        "chat",
        {**PRICE, "input_price": "3"},
        actor_id=1,
        account_id="reseller",
    )
    await service.register_operation(1, "account-op", "pr_review", {})
    for account, profile in (("direct", first), ("reseller", second)):
        call = await service.start_call(
            "account-op",
            f"call-{account}",
            "openai",
            "same-model",
            "chat",
            account_id=account,
        )
        assert call.price_profile_id == profile.id and call.account_id == account
        db.add(
            AIUsageRecord(
                record_key=f"usage-{account}",
                actual_call_id=call.call_id,
                operation_id="account-op",
                user_id=1,
                feature="pr_review",
                role="main",
                provider_id="openai",
                account_id=account,
                model_id="same-model",
                call_kind="chat",
                protocol_family="openai_compatible",
                input_tokens=1_000_000,
                output_tokens=0,
                usage_reported=True,
                usage_semantics={"output_includes_reasoning": True},
            )
        )
        call.state = "usage_known"
        call.usage_record_key = f"usage-{account}"
    await db.flush()
    await service.publish_price(
        "openai",
        "same-model",
        "chat",
        {**PRICE, "input_price": "9"},
        actor_id=1,
        account_id="direct",
    )
    operation = await service.finish_operation("account-op", "completed")
    assert operation.settled_units == 4_000_000
    charges = (await db.execute(select(BillingUsageCharge))).scalars().all()
    assert [row.provider_cost for row in charges] == ["1", "3"]
    assert [row.snapshot["account_id"] for row in charges] == ["direct", "reseller"]
    assert first.config["input_price"] == "1" and second.config["input_price"] == "3"


@pytest.mark.asyncio
async def test_account_missing_price_cannot_fall_back_to_provider_tariff(db):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, 10, "account-missing:fund")
    legacy = await service.publish_price("openai", "m", "chat", PRICE, actor_id=1)
    await service.register_operation(1, "account-missing", "agent", {})
    with pytest.raises(BillingError, match="account") as error:
        await service.start_call(
            "account-missing", "missing", "openai", "m", "chat", account_id="new"
        )
    assert error.value.code == "missing_price"
    assert await service._price("openai", "m", "chat", account_id="new") is None
    assert (await service._price("openai", "m", "chat")).id == legacy.id
    assert legacy.account_id is None and legacy.scope_key == "provider"


@pytest.mark.asyncio
async def test_provider_scope_usage_keeps_original_quote_after_account_price(db):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, 10, "legacy-history:fund")
    legacy = await service.publish_price("p", "m", "chat", PRICE, actor_id=1)
    await service.register_operation(1, "legacy-history", "pr_review", {})
    call = await service.start_call("legacy-history", "legacy-call", "p", "m", "chat")
    assert call.price_profile_id == legacy.id and call.account_id is None
    await service.publish_price(
        "p",
        "m",
        "chat",
        {**PRICE, "input_price": "99"},
        actor_id=1,
        account_id="current-account",
    )
    db.add(
        AIUsageRecord(
            record_key="legacy-usage",
            actual_call_id="legacy-call",
            operation_id="legacy-history",
            user_id=1,
            feature="pr_review",
            role="main",
            provider_id="p",
            model_id="m",
            call_kind="chat",
            protocol_family="openai_compatible",
            input_tokens=1_000_000,
            output_tokens=0,
            usage_reported=True,
            usage_semantics={"output_includes_reasoning": True},
        )
    )
    call.state = "usage_known"
    call.usage_record_key = "legacy-usage"
    await db.flush()
    result = await service.finish_operation("legacy-history", "completed")
    assert result.settled_units == 1_000_000
    frozen = (await db.execute(select(BillingUsageCharge))).scalar_one()
    assert frozen.price_profile_id == legacy.id
    assert frozen.snapshot["account_id"] is None


@pytest.mark.asyncio
async def test_account_result_mismatch_remains_pending_without_a_wrong_quote(db):
    service = BillingService(db, policy=POLICY)
    await service.grant(1, 10, "account-mismatch:fund")
    await service.publish_price(
        "p", "m", "chat", PRICE, actor_id=1, account_id="actual"
    )
    await service.register_operation(1, "account-mismatch", "agent", {})
    call = await service.start_call(
        "account-mismatch",
        "account-mismatch-call",
        "p",
        "m",
        "chat",
        account_id="actual",
    )
    db.add(
        AIUsageRecord(
            record_key="mismatched-result",
            actual_call_id=call.call_id,
            operation_id="account-mismatch",
            user_id=1,
            feature="agent",
            role="agent_team",
            provider_id="p",
            account_id="other",
            model_id="m",
            call_kind="chat",
            protocol_family="openai_compatible",
            input_tokens=1_000_000,
            output_tokens=0,
            usage_reported=True,
            usage_semantics={"output_includes_reasoning": True},
        )
    )
    call.state = "usage_known"
    call.usage_record_key = "mismatched-result"
    await db.flush()
    result = await service.finish_operation("account-mismatch", "completed")
    assert result.pending_reason == "price_identity_mismatch"
    assert result.settled_units == 0
    assert (await db.execute(select(BillingUsageCharge))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_activation_requires_every_actual_fallback_account(db, monkeypatch):
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
    service = BillingService(db)
    for kind in ("chat", "chat_stream"):
        await service.publish_price(
            "p", "m", kind, PRICE, actor_id=1, account_id="primary"
        )
    with pytest.raises(BillingError) as error:
        await validate_billing_configuration(db, {"billing_enabled": True})
    assert all("fallback" in detail for detail in error.value.issues[0]["details"])
    for kind in ("chat", "chat_stream"):
        await service.publish_price(
            "p", "m", kind, PRICE, actor_id=1, account_id="fallback"
        )
    await validate_billing_configuration(db, {"billing_enabled": True})
    rows = (await db.execute(select(BillingPriceProfile))).scalars().all()
    assert {row.account_id for row in rows} == {"primary", "fallback"}


def test_additive_scope_migration_preserves_immutable_legacy_prices():
    engine = create_engine("sqlite:///:memory:", connect_args={"autocommit": False})
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE billing_price_profiles (
                id INTEGER PRIMARY KEY, provider_id VARCHAR(128) NOT NULL,
                model_id VARCHAR(255) NOT NULL, call_kind VARCHAR(32) NOT NULL,
                version INTEGER NOT NULL, config JSON NOT NULL,
                actor_id INTEGER NOT NULL, created_at DATETIME NOT NULL,
                CONSTRAINT uq_billing_price_version UNIQUE
                    (provider_id, model_id, call_kind, version)
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO billing_price_profiles
            (id,provider_id,model_id,call_kind,version,config,actor_id,created_at)
            VALUES(7,'p','m','chat',4,'{"input_price":"17"}',1,'2026-10-07')
        """)
        )
        connection.execute(
            text("""
            CREATE TRIGGER billing_price_profiles_no_update BEFORE UPDATE
            ON billing_price_profiles
            BEGIN SELECT RAISE(ABORT, 'Billing ledger is append-only'); END
        """)
        )
        before = connection.execute(
            text("SELECT config,version FROM billing_price_profiles")
        ).one()
        for name in ("account_id", "scope_key"):
            connection.execute(
                text(
                    _build_add_column_sql(
                        connection.dialect,
                        "billing_price_profiles",
                        BillingPriceProfile.__table__.c[name],
                    )
                )
            )
        assert ensure_billing_schema(connection)
        assert not ensure_billing_schema(connection)
        row = connection.execute(
            text(
                "SELECT config,version,account_id,scope_key FROM billing_price_profiles"
            )
        ).one()
        assert row[:2] == before and row[2:] == (None, "provider")
        index = next(
            item
            for item in inspect(connection).get_indexes("billing_price_profiles")
            if item["name"] == "uq_billing_price_scope_version"
        )
        assert index["unique"] and "scope_key" in index["column_names"]
        with pytest.raises(DatabaseError, match="append-only"):
            connection.execute(text("UPDATE billing_price_profiles SET version=0"))
    engine.dispose()
