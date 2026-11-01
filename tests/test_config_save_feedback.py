"""Configuration saves retain field failures and the authenticated user's language."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.api.v1 import config as api_routes
from backend.api.v1.deps import require_api_super_admin
from backend.core.config import get_settings
from backend.models.database import AppConfig, Base, WebUIConfig
from backend.models.telegram_models import TelegramUser
from backend.services.billing_configuration_service import (
    validate_billing_configuration,
)
from backend.services.billing_service import BillingError
from backend.webui import deps
from tests.test_billing_credits_api import APISQLSession


@pytest.fixture
def config_app(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'config.db'}", connect_args={"autocommit": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(TelegramUser(id=1, telegram_id=111, role="super_admin"))
        db.add(WebUIConfig(user_id=1, language="zh-CN"))
        db.add(AppConfig(key_name="billing_enabled", key_value="false"))
        db.commit()
    deps.invalidate_user_prefs_cache(1)
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": 1, "token_type": "access", "sub": "admin"},
    )
    monkeypatch.setattr(get_settings(), "default_language", "en")

    async def chain(role):
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    provider=SimpleNamespace(id="provider"),
                    model=SimpleNamespace(model_id="model"),
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
    app = FastAPI()
    app.include_router(api_routes.router, prefix="/api/v1")

    async def db_dependency():
        with Session(engine, expire_on_commit=False) as db:
            yield APISQLSession(db)

    app.dependency_overrides[deps.get_db] = db_dependency
    app.dependency_overrides[deps.require_super_admin] = lambda: {
        "user_id": 1,
        "sub": "admin",
        "role": "super_admin",
    }
    app.dependency_overrides[require_api_super_admin] = app.dependency_overrides[
        deps.require_super_admin
    ]
    app.dependency_overrides[deps.require_csrf] = lambda: "verified"
    app.dependency_overrides[deps.require_csrf_header] = lambda: "verified"
    yield app, engine
    deps.invalidate_user_prefs_cache(1)
    engine.dispose()


@pytest.mark.asyncio
async def test_bearer_config_api_uses_trusted_user_preference_without_cookie(
    config_app, monkeypatch
):
    app, engine = config_app
    from backend.core.config import invalidate_dynamic_config_cache

    monkeypatch.setattr(get_settings(), "default_language", "zh-CN")
    invalidate_dynamic_config_cache(["default_language"])
    with Session(engine) as db:
        preference = db.scalar(select(WebUIConfig))
        preference.language = "en"
        db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/config/general", json={"configs": {"billing_enabled": "true"}}
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "missing_price"
    assert "pricing" in body["errors"][0]["message"]
    assert "定价" not in body["error"]
    assert body["errors"][0]["field"] == "billing_enabled"
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "billing_enabled"
                )
            )
            == "false"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("saved", "proposed"), [(False, "true"), (True, "false")])
async def test_pricing_validation_uses_proposed_compression_state(
    config_app, monkeypatch, saved, proposed
):
    _, engine = config_app
    from backend.services.billing_service import BillingService
    from tests.test_billing_settlement import PRICE

    async def setting(key, **kwargs):
        return saved if key == "enable_context_compression" else "none"

    monkeypatch.setattr(
        "backend.services.billing_configuration_service.get_dynamic_config", setting
    )
    with Session(engine, expire_on_commit=False) as session:
        db = APISQLSession(session)
        service = BillingService(db)
        for kind in ("chat", "chat_stream"):
            await service.publish_price("provider", "model", kind, PRICE, actor_id=1)
        await db.commit()
        changes = {"billing_enabled": "true", "enable_context_compression": proposed}
        if proposed == "true":
            with pytest.raises(BillingError) as failed:
                await validate_billing_configuration(db, changes)
            assert failed.value.code == "missing_price"
        else:
            await validate_billing_configuration(db, changes)
