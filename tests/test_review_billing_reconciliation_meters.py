"""Reviewed input-only usage uses the same semantics as live metering."""

import pytest
from sqlalchemy import select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.telegram_models import TelegramUser
from backend.services.billing_reconciliation_service import resolve_billing_call
from tests.test_billing_settlement import PRICE, prepare
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["embedding", "rerank"])
async def test_reviewed_input_only_meter_does_not_require_chat_cache_price(db, kind):
    service = await prepare(db)
    price = {
        key: value for key, value in PRICE.items() if key != "cache_read_supported"
    }
    await service.publish_price("openai", "input-model", kind, price, actor_id=1)
    attempt = await service.start_call(
        "op-1",
        "pending-input",
        "openai",
        "input-model",
        kind,
        protocol_family="openai_compatible",
    )
    attempt.state = "pending_usage"
    await service.finish_operation("op-1", "completed")
    (await db.get(TelegramUser, 1)).role = "super_admin"
    evidence = {
        "call_id": "pending-input",
        "event_key": "reviewed-input",
        "actor_id": 1,
        "reason": "Verified provider meter",
        "usage": {"total_tokens": 1000},
    }
    first = await resolve_billing_call(db, **evidence)
    second = await resolve_billing_call(db, **evidence)
    assert first.id == second.id
    record = (await db.execute(select(AIUsageRecord))).scalar_one()
    assert record.call_kind == kind
    assert record.input_tokens == 1000 and record.output_tokens is None
    assert record.cached_input_tokens is None
    assert record.usage_semantics["cache_read_supported"] is False
    assert (await service._operation("op-1")).status == "settled"
    assert (await service.get_wallet(1)).balance_units == 9_999_000
    assert (await service.reconcile_wallet(1))["consistent"]
