"""Active billing cannot be bypassed by partial AI configuration writes."""

import json

import pytest
from sqlalchemy import select, update

from backend.core.ai_protocol import account_store
from backend.models.database import AppConfig, WebUIConfig
from backend.services.billing_configuration_service import (
    validate_billing_configuration,
)
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_settlement import PRICE
from tests.test_billing_wallet import SQLSession
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


async def configure(db, monkeypatch):
    account = account_store.ProviderAccount(
        id="acc_paid", name="Paid", provider_id="openai", default_model="m1"
    )
    bindings = {"main": {"primary": {"account": account.id, "model": "m1"}}}
    for key, value in {
        "billing_enabled": "true",
        "ai_account.acc_paid": json.dumps(account.to_dict()),
        "ai_role_bindings": json.dumps(bindings),
        "enable_context_compression": "false",
        "embedding_provider": "local",
        "rerank_provider": "local",
    }.items():
        db.add(AppConfig(key_name=key, key_value=value))
    await BillingService(db).publish_price(
        "openai", "m1", "chat", PRICE, actor_id=1, account_id=account.id
    )
    db.session.commit()

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if args[0]:
                db.session.rollback()

        async def commit(self):
            db.session.commit()

        def __getattr__(self, name):
            return getattr(SQLSession(db.session), name)

    monkeypatch.setattr("backend.models.database.async_session", Context)
    return account, bindings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"embedding_provider": "openai", "embedding_model": "unpriced"},
        {"rerank_provider": "siliconflow", "rerank_model": "unpriced"},
        {"enable_context_compression": True},
    ],
)
async def test_enabled_billing_revalidates_partial_route_patch(
    db, monkeypatch, changes
):
    await configure(db, monkeypatch)
    with pytest.raises(BillingError, match="Missing exact pricing"):
        await validate_billing_configuration(db, changes)


@pytest.mark.asyncio
async def test_account_and_bindings_validate_proposed_state_before_commit(
    db, monkeypatch
):
    account, bindings = await configure(db, monkeypatch)
    account.provider_id = "deepseek"
    with pytest.raises(BillingError, match="Missing exact pricing"):
        await account_store.save_account(account)
    stored = await account_store.get_account(account.id)
    assert stored.provider_id == "openai"
    bindings["main"]["primary"]["model"] = "new-unpriced-model"
    with pytest.raises(BillingError, match="Missing exact pricing"):
        await account_store.save_role_bindings_raw(bindings)
    row = (
        await db.execute(
            select(AppConfig).where(AppConfig.key_name == "ai_role_bindings")
        )
    ).scalar_one()
    assert json.loads(row.key_value)["main"]["primary"]["model"] == "m1"
    # Unused accounts and edits retaining the priced route remain valid.
    account.provider_id = "openai"
    account.name = "Renamed"
    await account_store.save_account(account)
    assert (await account_store.get_account(account.id)).name == "Renamed"


@pytest.mark.asyncio
async def test_disabling_billing_allows_unpriced_configuration(db, monkeypatch):
    await configure(db, monkeypatch)
    await validate_billing_configuration(
        db,
        {
            "billing_enabled": False,
            "embedding_provider": "openai",
            "embedding_model": "new",
        },
    )


@pytest.mark.asyncio
async def test_account_endpoint_reports_localized_failure_and_retains_saved_route(
    db, monkeypatch
):
    from backend.api.v1.config import AccountSaveRequest, save_ai_account

    account, _ = await configure(db, monkeypatch)
    db.add(WebUIConfig(user_id=1, language="en"))
    db.session.commit()
    response = await save_ai_account(
        AccountSaveRequest(
            id=account.id,
            name="Changed",
            provider_id="deepseek",
            api_key="secret-input",
        ),
        {"user_id": 1, "sub": "review-admin"},
        db,
    )
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["code"] == "missing_price"
    assert payload["errors"][0]["help_url"] == "/billing/admin/pricing"
    assert "secret-input" not in response.body.decode()
    assert (await account_store.get_account(account.id)).provider_id == "openai"


@pytest.mark.asyncio
async def test_currency_general_api_rejects_non_cny_alipay_without_write(
    db, monkeypatch
):
    from backend.api.v1.config import update_general_config
    from backend.api.v1.schemas import ConfigGeneralUpdateRequest

    response = await update_general_config(
        ConfigGeneralUpdateRequest(configs={"alipay_currency": "JPY"}),
        db,
        {"user_id": 1, "sub": "review-admin"},
    )
    assert response.status_code == 400
    assert json.loads(response.body)["errors"][0]["field"] == "alipay_currency"
    assert (
        await db.execute(
            select(AppConfig).where(AppConfig.key_name == "alipay_currency")
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_guard_refreshes_previously_loaded_config_identity(db, monkeypatch):
    await configure(db, monkeypatch)
    gate = (
        await db.execute(
            select(AppConfig).where(AppConfig.key_name == "billing_enabled")
        )
    ).scalar_one()
    gate.key_value = "false"
    await db.flush()
    # SQL can change without refreshing an already loaded ORM identity.
    await db.execute(
        update(AppConfig)
        .where(AppConfig.id == gate.id)
        .values(key_value="true")
        .execution_options(synchronize_session=False)
    )
    assert gate.key_value == "false"
    with pytest.raises(BillingError, match="Missing exact pricing"):
        await validate_billing_configuration(
            db,
            {
                "embedding_provider": "openai",
                "embedding_model": "unpriced",
            },
        )
    assert gate.key_value == "true"


@pytest.mark.asyncio
async def test_new_fallback_is_rejected_until_its_exact_account_tariff_exists(
    db, monkeypatch
):
    account, bindings = await configure(db, monkeypatch)
    bindings["main"]["fallback"] = [{"account": account.id, "model": "fallback-m"}]
    with pytest.raises(BillingError, match="fallback-m/chat"):
        await account_store.save_role_bindings_raw(bindings)
    await BillingService(db).publish_price(
        "openai",
        "fallback-m",
        "chat",
        PRICE,
        actor_id=1,
        account_id=account.id,
    )
    db.session.commit()
    await account_store.save_role_bindings_raw(bindings)
    stored = await account_store.get_role_bindings()
    assert stored["main"].fallback[0].model == "fallback-m"
