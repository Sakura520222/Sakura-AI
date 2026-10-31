"""Shared HTTP/SQL fixtures for billing and Agent admission. Importing them requires no UI assets."""

import pytest
from fastapi import FastAPI, Request
from itsdangerous import URLSafeTimedSerializer

from backend.api.v1 import billing, billing_webui
from backend.api.v1 import deps as api_deps
from backend.core import config as core_config
from backend.models.database import WebUIConfig
from backend.webui import deps
from backend.webui.deps import get_db
from backend.webui.routes import billing as routes
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture


@pytest.fixture
def pricing_app(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime

    async def db_dependency():
        async with factory() as db:
            yield db

    async def identity(request: Request):
        return {
            "user_id": 1,
            "sub": "owner1",
            "role": request.headers.get("x-test-role", "super_admin"),
        }

    monkeypatch.setattr(deps, "get_current_user", identity)
    monkeypatch.setattr(api_deps, "get_api_current_user", identity)
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": 1, "token_type": "access"},
    )
    monkeypatch.setattr(deps, "is_access_token_payload", lambda payload: True)
    monkeypatch.setattr(
        deps,
        "_csrf_serializer",
        URLSafeTimedSerializer("isolated-pricing-csrf", salt="webui-csrf"),
    )
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: "zh-CN")
    deps._USER_PREFS_CACHE.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(billing.router, prefix="/api/v1")
    app.include_router(billing_webui.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = db_dependency
    yield app, factory, deps.generate_csrf_token()
    deps._USER_PREFS_CACHE.clear()


def pricing_fields(csrf_token, **overrides):
    return {
        "csrf_token": csrf_token,
        "config_mode": "fields",
        "provider_id": "openai",
        "model_id": "test-priced-model",
        "call_kind": "chat",
        "unit": "tokens",
        "token_scale": "1000",
        "currency": "USD",
        "settlement_currency": "CNY",
        "fx_rate": "7",
        "markup": "1.25",
        "credits_per_currency_unit": "10",
        "input_price": "0.003",
        "output_price": "0.009",
        "cache_read_supported": "false",
        "cache_creation_supported": "false",
        "reasoning_supported": "false",
        **overrides,
    }


async def seed_language(factory, language="en"):
    async with factory() as db:
        db.add(WebUIConfig(user_id=1, language=language))
        await db.commit()
