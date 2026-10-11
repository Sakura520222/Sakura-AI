"""Paddle-specific payment currencies must not inherit all billing asset codes."""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select

from backend.api.v1 import config as config_api
from backend.api.v1.deps import require_api_super_admin
from backend.core.config import (
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    Settings,
    get_settings,
    validate_currency_config_value,
)
from backend.models.database import AppConfig
from backend.services.payment.paddle_gateway import PaddleGateway
from backend.webui.deps import get_db
from backend.webui.routes.config import (
    _DynamicConfigValidationError,
    _validate_dynamic_config_value,
)
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


def test_payment_selector_excludes_unsupported_billing_assets():
    codes = {
        entry["value"] for entry in DYNAMIC_CONFIG_SELECT_OPTIONS["paddle_currency"]
    }
    assert "USDT" not in codes and "AED" not in codes
    assert {"USD", "CNY", "EUR", "JPY", "ZAR"} <= codes
    assert "USDT" in {
        entry["value"]
        for entry in DYNAMIC_CONFIG_SELECT_OPTIONS["payment_default_currency"]
    }


@pytest.mark.parametrize("code", ["USDT", "AED"])
def test_config_validation_rejects_paddle_unsupported_codes(code):
    with pytest.raises(ValueError):
        validate_currency_config_value("paddle_currency", code)
    with pytest.raises(ValidationError):
        Settings(paddle_currency=code)
    with pytest.raises(_DynamicConfigValidationError):
        _validate_dynamic_config_value(
            "paddle_currency",
            code,
            expected_type=str,
            ranges=DYNAMIC_CONFIG_RANGES,
            select_options=DYNAMIC_CONFIG_SELECT_OPTIONS,
        )
    assert validate_currency_config_value("payment_default_currency", code) == code


@pytest.mark.asyncio
async def test_gateway_rejects_unsupported_before_sdk_client_or_transaction():
    with patch("paddle_billing.Client") as client:
        result = await PaddleGateway("TEST-api-key", "TEST-secret").create_payment(
            "TEST-order",
            100,
            "USDT",
            "TEST-plan",
            1,
            "https://example.test/success",
            "https://example.test/cancel",
        )
    assert result.success is False
    client.assert_not_called()


@pytest.mark.asyncio
async def test_global_api_cannot_bypass_currency_selector(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "paddle_currency", settings.paddle_currency)
    app = FastAPI()
    app.include_router(config_api.router, prefix="/api/v1")

    async def db_dependency():
        async with factory() as db:
            yield db

    async def admin():
        return {"user_id": 1, "sub": "owner1", "role": "super_admin"}

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_api_super_admin] = admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/config/general", json={"configs": {"paddle_currency": "USDT"}}
        )
        assert response.status_code == 400
        assert response.json()["errors"][0]["field"] == "paddle_currency"
        async with factory() as db:
            assert (
                await db.execute(
                    select(AppConfig).where(AppConfig.key_name == "paddle_currency")
                )
            ).scalar_one_or_none() is None
        valid = await client.patch(
            "/api/v1/config/general", json={"configs": {"paddle_currency": "usd"}}
        )
        assert valid.status_code == 200
    async with factory() as db:
        assert (
            await db.execute(
                select(AppConfig).where(AppConfig.key_name == "paddle_currency")
            )
        ).scalar_one().key_value == "USD"
