"""Actual stream requests opt into measured Usage, with an explicit compatibility flag."""

import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.ai_protocol.models import ProtocolFamily, UnifiedMessage
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingOperation
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient
from backend.services.ai_usage_service import finish_billing_operation
from backend.services.billing_context import BillingContext, bind_billing_context
from tests.test_billing_usage_attribution import fund_and_price
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_unified_client_fallback import _candidate

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "include_usage,native_usage", [(False, False), (False, True), (True, False)]
)
async def test_stream_usage_capability_reaches_wire_and_settlement(
    sql_runtime, include_usage, native_usage
):
    factory, engine, _ = sql_runtime
    candidate = _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "usage-model")
    candidate = replace(
        candidate,
        model=replace(
            candidate.model,
            capabilities=replace(
                candidate.model.capabilities, stream_usage=include_usage
            ),
        ),
    )
    await fund_and_price(
        factory,
        kinds=("chat",),
        provider=candidate.provider.id,
        model=candidate.model.model_id,
    )
    bodies = []

    def response(request):
        body = json.loads(request.content)
        bodies.append(body)
        chunks = ["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]})]
        if not include_usage:
            assert "stream_options" not in body
        if body.get("stream_options", {}).get("include_usage") or native_usage:
            chunks.append(
                "data: "
                + json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 100,
                            "total_tokens": 1100,
                            "prompt_tokens_details": {"cached_tokens": 0},
                            "completion_tokens_details": {"reasoning_tokens": 0},
                        },
                    }
                )
            )
        chunks.append("data: [DONE]")
        return httpx.Response(
            200, text="\n\n".join(chunks), headers={"content-type": "text/event-stream"}
        )

    client = UnifiedAIClient(
        fallback_config=FallbackConfig(max_retries=0), compressor=None
    )
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    context = BillingContext(1, str(uuid4()), "pr_review")
    try:
        with bind_billing_context(context):
            events = [
                event
                async for event in client.stream_with_retry(
                    [candidate], [UnifiedMessage(role="user", content="hello")]
                )
            ]
            await finish_billing_operation("completed")
    finally:
        await client.aclose()
    assert any(event.type == "text_delta" for event in events)
    assert bodies[0]["stream"] is True
    assert ("stream_options" in bodies[0]) is include_usage
    with Session(engine) as db:
        usage = db.scalar(select(AIUsageRecord))
        reported = include_usage or native_usage
        assert usage.usage_reported is reported
        operation = db.get(BillingOperation, context.operation_id)
        assert operation.settled_units == (300_000 if reported else 0)
        assert (operation.pending_reason is None) is reported
