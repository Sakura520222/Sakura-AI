"""Pricing UI reads configured account models and prices their actual scope."""

import json

import httpx
import pytest
from sqlalchemy import select

from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingPriceProfile
from backend.models.database import AppConfig
from backend.services import billing_account_pricing_service as sources
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_credits_api import billing_client as billing_client_fixture
from tests.test_billing_pricing_editor import pricing_app as pricing_app_fixture
from tests.test_billing_pricing_editor import pricing_fields, seed_language
from tests.test_billing_usage_attribution import sql_runtime as sql_runtime_fixture

pricing_app = pricing_app_fixture
sql_runtime = sql_runtime_fixture
billing_client = billing_client_fixture


async def accounts(factory):
    async with factory() as db:
        for identity, name, models, enabled in (
            ("acc_a", "Channel A", ["shared", "only-a"], True),
            ("acc_b", "Channel B", ["shared", "only-b"], True),
            ("acc_off", "Disabled", ["shared"], False),
        ):
            db.add(
                AppConfig(
                    key_name="ai_account." + identity,
                    key_value=json.dumps(
                        {
                            "id": identity,
                            "name": name,
                            "provider_id": "custom",
                            "protocol": "openai_compatible",
                            "api_base": "https://private.invalid/v1",
                            "api_key": "SECRET_" + identity,
                            "models": models,
                            "default_model": models[0],
                            "enabled": enabled,
                        }
                    ),
                )
            )
        await db.commit()
    sources._MODEL_CACHE.clear()


@pytest.mark.asyncio
async def test_editor_and_discovery_use_saved_accounts_and_do_not_expose_credentials(
    pricing_app, monkeypatch
):
    app, factory, _ = pricing_app
    await accounts(factory)
    await seed_language(factory, "zh-CN")
    calls = []

    async def discover(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "models": ["fresh-a", "shared"],
            "message": "must-not-return-upstream-message",
        }

    monkeypatch.setattr(sources, "probe_account", discover)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"access_token": "isolated-token"},
    ) as client:
        page = await client.get("/billing/admin/pricing")
        assert page.status_code == 200
        assert 'name="account_id"' in page.text
        assert "Channel A" in page.text and "Channel B" in page.text
        assert 'name="provider_id"' not in page.text
        assert "SECRET_" not in page.text and "private.invalid" not in page.text
        response = await client.get("/billing/admin/pricing/accounts/acc_a/models")
        assert response.status_code == 200
        assert response.json()["data"]["models"] == ["shared", "only-a", "fresh-a"]
        assert response.json()["data"]["account_id"] == "acc_a"
        assert (
            "SECRET_" not in response.text and "upstream-message" not in response.text
        )
        assert "private.invalid" not in response.text
        await client.get("/billing/admin/pricing/accounts/acc_a/models")
        assert len(calls) == 1
        await client.get("/billing/admin/pricing/accounts/acc_a/models?refresh=true")
    assert len(calls) == 2
    assert calls[0]["api_key"] == "SECRET_acc_a"
    assert calls[0]["model"] == "shared"


@pytest.mark.asyncio
async def test_discovery_failure_preserves_only_selected_saved_models(
    pricing_app, monkeypatch
):
    app, factory, _ = pricing_app
    await accounts(factory)

    async def failed(**kwargs):
        return {"success": False, "message": "SECRET_acc_b private endpoint failed"}

    monkeypatch.setattr(sources, "probe_account", failed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/billing/admin/pricing/accounts/acc_b/models")
        assert response.json()["data"]["models"] == ["shared", "only-b"]
        assert response.json()["data"]["discovery_failed"] is True
        assert "SECRET_" not in response.text
        assert "only-a" not in response.text
        missing = await client.get("/billing/admin/pricing/accounts/unknown/models")
        disabled = await client.get("/billing/admin/pricing/accounts/acc_off/models")
        assert missing.status_code == disabled.status_code == 404


@pytest.mark.asyncio
async def test_publication_derives_provider_from_selected_account_and_scopes_version(
    pricing_app,
):
    app, factory, csrf = pricing_app
    await accounts(factory)
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"access_token": "isolated-token"},
    ) as client:
        for identity in ("acc_a", "acc_b"):
            response = await client.post(
                "/billing/admin/pricing",
                data=pricing_fields(
                    csrf,
                    source_scope="account",
                    account_id=identity,
                    provider_id="spoofed",
                    model_id="shared",
                ),
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 200, response.text
        for identity in ("", "unknown", "acc_off"):
            response = await client.post(
                "/billing/admin/pricing",
                data=pricing_fields(csrf, source_scope="account", account_id=identity),
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 400
            assert any(
                issue["field"] == "account_id" for issue in response.json()["errors"]
            )
    async with factory() as db:
        profiles = (
            (
                await db.execute(
                    select(BillingPriceProfile).order_by(BillingPriceProfile.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(profiles) == 2
        assert {profile.account_id for profile in profiles} == {"acc_a", "acc_b"}
        assert {profile.provider_id for profile in profiles} == {"custom"}
        assert [profile.version for profile in profiles] == [1, 2]


@pytest.mark.asyncio
async def test_independent_rag_source_uses_actual_configuration_and_keeps_no_account(
    pricing_app, monkeypatch
):
    app, factory, csrf = pricing_app

    async def setting(key, **kwargs):
        return {
            "embedding_provider": "siliconflow",
            "embedding_model": "configured-embed",
            "rerank_provider": "none",
            "rerank_model": "",
        }[key]

    monkeypatch.setattr(sources, "get_dynamic_config", setting)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(
                csrf,
                source_scope="embedding",
                account_id="",
                model_id="spoof-model",
                provider_id="spoof-provider",
                call_kind="chat",
            ),
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200, response.text
    async with factory() as db:
        profile = (await db.execute(select(BillingPriceProfile))).scalar_one()
        assert (
            profile.provider_id,
            profile.model_id,
            profile.call_kind,
            profile.account_id,
        ) == ("siliconflow", "configured-embed", "embedding", None)


@pytest.mark.asyncio
async def test_models_endpoint_retains_superadmin_gate(pricing_app):
    app, factory, _ = pricing_app
    await accounts(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for role in ("user", "admin"):
            response = await client.get(
                "/billing/admin/pricing/accounts/acc_a/models",
                headers={"x-test-role": role},
            )
            assert response.status_code == 403


@pytest.mark.asyncio
async def test_json_price_contract_derives_account_provider_and_audits_identity(
    billing_client,
):
    client, factory = billing_client
    await accounts(factory)
    payload = {
        "account_id": "acc_a",
        "model_id": "shared",
        "call_kind": "chat",
        "config": TEST_PRICE,
    }
    assert (
        await client.post("/api/v1/billing/admin/pricing", json=payload)
    ).status_code == 403
    headers = {"x-test-identity": "operator"}
    for identity in ("acc_a", "acc_b"):
        # Account-only clients need no provider parameter; an explicit spoofed
        # provider cannot redirect the second account's price identity either.
        body = {**payload, "account_id": identity}
        if identity == "acc_b":
            body["provider_id"] = "spoofed-provider"
        response = await client.post(
            "/api/v1/billing/admin/pricing", json=body, headers=headers
        )
        assert response.status_code == 200, response.text
    for identity in ("unknown", "acc_off"):
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            json={**payload, "account_id": identity},
            headers=headers,
        )
        assert response.status_code == 400
    history = (
        await client.get("/api/v1/billing/admin/pricing", headers=headers)
    ).json()["items"]
    assert {item["account_id"] for item in history} == {"acc_a", "acc_b"}
    assert {item["provider_id"] for item in history} == {"custom"}
    assert {item["scope_key"] for item in history} == {
        "account:acc_a",
        "account:acc_b",
    }
    async with factory() as db:
        logs = (await db.execute(select(AdminActionLog))).scalars().all()
        assert len(logs) == 2
        assert {json.loads(log.detail)["account_id"] for log in logs} == {
            "acc_a",
            "acc_b",
        }
