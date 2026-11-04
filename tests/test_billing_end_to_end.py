"""Redeem API → business AI boundary → exact ledger → own bill, including failure."""

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from backend.api.v1 import deps as api_deps
from backend.api.v1.billing import router
from backend.core.ai_protocol.errors import AIError, AllCandidatesFailedError
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ProtocolFamily,
    StopReason,
    UnifiedResponse,
    UnifiedUsage,
)
from backend.models.billing_models import BillingTransaction, BillingUsageCharge
from backend.models.database import PRReview
from backend.models.telegram_models import TelegramUser
from backend.services.ai_reviewer import unified_client
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient
from backend.services.billing_context import billable_payload, enrich_billing_source
from backend.services.billing_service import BillingService
from backend.webui.deps import get_db, require_payment_enabled
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_unified_client_fallback import _candidate

sql_runtime = runtime_fixture


@pytest.mark.asyncio
async def test_redeem_business_usage_pricing_settlement_bill_failure_and_refund(
    sql_runtime, monkeypatch
):
    factory, _, _ = sql_runtime
    async with factory() as db:
        db.add(TelegramUser(id=3, github_username="operator", role="super_admin"))
        db.add(
            PRReview(
                id=10,
                pr_id=100,
                pr_number=5,
                repo_owner="owner1",
                repo_name="project",
                author="owner1",
                strategy="test",
            )
        )
        await db.commit()

    async def identity(request: Request):
        admin = request.headers.get("x-test-role") == "admin"
        return {
            "user_id": 3 if admin else 1,
            "sub": "operator" if admin else "owner1",
            "role": "super_admin" if admin else "user",
        }

    async def no_mfa(*args, **kwargs):
        return False

    async def enabled():
        pass

    async def db_dependency():
        async with factory() as db:
            yield db

    monkeypatch.setattr(api_deps, "get_api_current_user", identity)
    monkeypatch.setattr(api_deps, "user_requires_mfa_enrollment", no_mfa)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_payment_enabled] = enabled
    candidate = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "billing-test-model")

    class ProviderBoundary:
        fail = False

        async def chat(self, client, endpoint, credential, request, **kwargs):
            usage = UnifiedUsage(
                input_tokens=1000,
                output_tokens=20,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                reasoning_tokens=0,
                reported_fields=frozenset(
                    {
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_creation_tokens",
                        "reasoning_tokens",
                    }
                ),
            )
            if self.fail:
                raise AIError(
                    AIErrorCategory.SERVER_ERROR,
                    "test failure after actual Usage",
                    usage=usage,
                )
            return UnifiedResponse(
                content="review result",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=usage,
            )

    boundary = ProviderBoundary()
    monkeypatch.setattr(unified_client, "_get_adapter", lambda family: boundary)

    class BusinessWorker:
        @billable_payload("pr_review")
        async def process(self, payload):
            enrich_billing_source(review_id=10)
            async with UnifiedAIClient(
                fallback_config=FallbackConfig(max_retries=0)
            ) as ai:
                return await ai.call_with_retry(
                    [candidate],
                    [{"role": "user", "content": "isolated PR fixture"}],
                    role="main",
                    model=candidate.model.model_id,
                )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        admin = {"x-test-role": "admin"}
        plan = await client.post(
            "/api/v1/billing/admin/plans",
            headers=admin,
            json={
                "name": "TEST Credits pack",
                "plan_type": "one_time",
                "price_cents": 100,
                "credit_grant": "5",
            },
        )
        assert plan.status_code == 200, plan.text
        codes = await client.post(
            "/api/v1/billing/admin/codes/generate",
            headers=admin,
            json={"plan_id": plan.json()["id"], "count": 1},
        )
        assert codes.status_code == 200, codes.text
        code = codes.json()["codes"][0]
        redeem = await client.post("/api/v1/billing/redeem", json={"code": code})
        assert redeem.status_code == 200, redeem.text
        await client.post("/api/v1/billing/redeem", json={"code": code})
        assert (await client.get("/api/v1/billing/wallet")).json()["balance"] == "5"
        price = await client.post(
            "/api/v1/billing/admin/pricing",
            headers=admin,
            json={
                "provider_id": candidate.provider.id,
                "model_id": candidate.model.model_id,
                "call_kind": "chat",
                "config": TEST_PRICE,
            },
        )
        assert price.status_code == 200, price.text
        payload = {"user_id": 1, "repo_full_name": "owner1/project", "pr_number": 5}
        await BusinessWorker().process(payload)
        wallet = (await client.get("/api/v1/billing/wallet")).json()
        assert wallet["balance"] == "4.9691" and wallet["reserved"] == "0"
        bill = (
            await client.get("/api/v1/billing/transactions?kind=consumption")
        ).json()
        assert bill["total"] == 1
        assert bill["items"][0]["credits"] == "-0.0309"
        assert bill["items"][0]["source"]["url"] == "/pr/10"
        usage = (await client.get("/api/v1/billing/usage?feature=pr_review")).json()
        assert usage["items"][0]["input_tokens"] == 1000
        boundary.fail = True
        with pytest.raises(AllCandidatesFailedError):
            await BusinessWorker().process(
                {**payload, "billing_context": None, "delivery_id": str(uuid4())}
            )
        assert (await client.get("/api/v1/billing/wallet")).json()[
            "balance"
        ] == "4.9691"
        async with factory() as db:
            quotes = (await db.execute(select(BillingUsageCharge))).scalars().all()
            assert len(quotes) == 2
            transaction = (
                await db.execute(
                    select(BillingTransaction).where(
                        BillingTransaction.kind == "consumption"
                    )
                )
            ).scalar_one()
            await BillingService(db).reverse(
                transaction.id, "test:refund", actor_id=3, reason="fixture correction"
            )
            await db.commit()
        assert (await client.get("/api/v1/billing/wallet")).json()["balance"] == "5"
        refunds = (await client.get("/api/v1/billing/transactions?kind=refund")).json()
        assert refunds["total"] == 1 and refunds["items"][0]["credits"] == "0.0309"
