"""Activation validates exact configured models and never probes paid APIs."""

from types import SimpleNamespace

import pytest

from backend.services.billing_configuration_service import (
    validate_billing_configuration,
)
from backend.services.billing_service import BillingError, BillingService
from tests.test_billing_settlement import PRICE
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


@pytest.mark.asyncio
async def test_activation_rejects_missing_fallback_price(db, monkeypatch):
    async def chain(role):
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    provider=SimpleNamespace(id="p"),
                    model=SimpleNamespace(model_id="m"),
                )
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
    with pytest.raises(BillingError, match="Missing exact pricing"):
        await validate_billing_configuration(db, {"billing_enabled": "true"})
    service = BillingService(db)
    for kind in ("chat", "chat_stream"):
        await service.publish_price("p", "m", kind, PRICE, actor_id=1)
    await validate_billing_configuration(db, {"billing_enabled": "true"})


@pytest.mark.asyncio
async def test_invalid_billing_numbers_and_policies_rejected_before_writes(db):
    for changes in (
        {"billing_initial_reserve_credits": "-1"},
        {"billing_initial_reserve_credits": 1.1},
        {"billing_enabled": "guess"},
        {"billing_reservation_ttl_seconds": "5"},
        {"payment_partial_refund_policy": "anything"},
    ):
        with pytest.raises((BillingError, ValueError)):
            await validate_billing_configuration(db, changes)
