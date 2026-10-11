"""Configuration saves retain field failures and the authenticated user's language."""

import json
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
from backend.webui.routes import config as routes
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
    monkeypatch.setattr(routes, "log_admin_action", lambda *a, **k: _done())
    app = FastAPI()
    app.include_router(routes.router)
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


async def _done():
    return None


@pytest.mark.asyncio
async def test_save_all_missing_prices_reports_billing_field_and_user_language(
    config_app,
):
    app, engine = config_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        response = await client.post(
            "/config/save-all",
            json={
                "requests": [
                    {
                        "action": "/config/general/save",
                        "anchor": None,
                        "fields": {
                            "billing_enabled": "true",
                            "billing_initial_reserve_credits": "500",
                            "billing_reservation_ttl_seconds": "3600",
                        },
                    }
                ]
            },
        )
    body = response.json()
    assert body["ok"] is False
    assert "保存失败" in body["toast"]
    issue = body["results"][0]["errors"][0]
    assert issue["field"] == "billing_enabled"
    assert issue["code"] == "missing_price"
    assert "定价" in issue["message"]
    assert issue["help_url"] == "/billing/admin/pricing"
    assert any("provider/model/chat" in detail for detail in issue["details"])
    assert body["errors"] == body["results"][0]["errors"]
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "billing_enabled"
                )
            )
            == "false"
        )
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "billing_initial_reserve_credits"
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_direct_ajax_reports_all_bad_fields_without_echoing_raw_input(config_app):
    app, _ = config_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        response = await client.post(
            "/config/general/save",
            headers={"Accept": "application/json"},
            data={
                "billing_initial_reserve_credits": "private-invalid-value",
                "billing_reservation_ttl_seconds": "5",
            },
        )
    assert response.status_code == 400
    issues = response.json()["errors"]
    assert {item["field"] for item in issues} == {
        "billing_initial_reserve_credits",
        "billing_reservation_ttl_seconds",
    }
    assert "private-invalid-value" not in response.text


@pytest.mark.asyncio
async def test_bearer_config_api_uses_trusted_user_preference_without_cookie(
    config_app,
    monkeypatch,
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
async def test_successful_save_and_legacy_redirect_follow_personal_language(
    config_app, monkeypatch
):
    from urllib.parse import parse_qs, urlsplit

    app, engine = config_app
    monkeypatch.setattr("backend.core.config.update_settings_field", lambda *args: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        response = await client.post(
            "/config/general/save",
            headers={"Accept": "application/json"},
            data={"billing_enabled": "false", "billing_initial_reserve_credits": "500"},
        )
        assert response.status_code == 200
        assert response.json()["ok"]
        assert "配置" in response.json()["toast"]
        failed = await client.post(
            "/config/general/save", data={"billing_enabled": "true"}
        )
    assert failed.status_code == 302
    query = parse_qs(urlsplit(failed.headers["location"]).query)
    assert query["_toast_type"] == ["error"]
    assert "定价" in query["_toast"][0]
    issue = json.loads(query["_errors"][0])[0]
    assert issue["field"] == "billing_enabled"
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "billing_initial_reserve_credits"
                )
            )
            == "500"
        )


@pytest.mark.asyncio
async def test_strategy_numeric_and_placeholder_errors_are_localized_field_errors(
    config_app,
):
    app, _ = config_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        bad_number = await client.post(
            "/config/strategies/save",
            headers={"Accept": "application/json"},
            data={
                "section": "strategies",
                "strategy_quick_max_files": "private-secret-value",
            },
        )
        bad_template = await client.post(
            "/config/strategies/save",
            headers={"Accept": "application/json"},
            data={
                "section": "pr_summary",
                "pr_summary_user_template": "private prompt",
            },
        )
    assert bad_number.status_code == 400
    assert bad_number.json()["errors"][0]["field"] == "strategy_quick_max_files"
    assert "有效数值" in bad_number.json()["errors"][0]["message"]
    assert "private-secret-value" not in bad_number.text
    assert bad_template.status_code == 400
    assert bad_template.json()["errors"][0]["field"] == "pr_summary_user_template"
    assert "占位符" in bad_template.json()["errors"][0]["message"]
    assert "private prompt" not in bad_template.text


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_name", "second_color", "invalid_field", "code"),
    [
        (
            "PRIVATE_CUSTOM_LABEL",
            "PRIVATE_INVALID_COLOR",
            "label_color_2",
            "label_color",
        ),
        ("PRIVATE_INVALID_NAME!", "ff0000", "label_name_2", "label_name_characters"),
    ],
)
async def test_custom_label_errors_point_to_second_row_without_writing(
    config_app, second_name, second_color, invalid_field, code
):
    app, engine = config_app
    original = '{"bug":{"color":"123456","description":"stored"}}'
    with Session(engine) as db:
        db.add(AppConfig(key_name="label.definitions", key_value=original))
        db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        response = await client.post(
            "/config/labels/save-labels",
            headers={"Accept": "application/json"},
            data={
                "label_name_0": "bug",
                "label_color_0": "ff0000",
                "label_desc_0": "valid first row",
                "label_name_2": second_name,
                "label_color_2": second_color,
                "label_desc_2": "PRIVATE_DESCRIPTION",
            },
        )
    assert response.status_code == 400
    issue = response.json()["errors"][0]
    assert issue["field"] == invalid_field
    assert issue["code"] == code
    assert "PRIVATE_" not in response.text
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "label.definitions"
                )
            )
            == original
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_source", "second_blocked", "invalid_field"),
    [
        ("style", "PRIVATE_INVALID_TARGET!", "conflict_blocked_2"),
        ("PRIVATE_CUSTOM_SOURCE", "PRIVATE_INVALID_TARGET!", "conflict_blocked_2"),
        ("PRIVATE_INVALID_SOURCE!", "bug", "conflict_source_2"),
    ],
)
async def test_conflict_errors_point_to_second_row_without_writing(
    config_app, second_source, second_blocked, invalid_field
):
    app, engine = config_app
    original = '{"enhancement":["bug"]}'
    with Session(engine) as db:
        db.add(AppConfig(key_name="label.conflict_rules", key_value=original))
        db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "valid-test-session"},
    ) as client:
        response = await client.post(
            "/config/labels/save-conflict-rules",
            headers={"Accept": "application/json"},
            data={
                "conflict_source_0": "enhancement",
                "conflict_blocked_0": "bug",
                "conflict_source_2": second_source,
                "conflict_blocked_2": second_blocked,
            },
        )
    assert response.status_code == 400
    issue = response.json()["errors"][0]
    assert issue["field"] == invalid_field
    assert issue["code"] == "label_name_characters"
    assert "PRIVATE_" not in response.text
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "label.conflict_rules"
                )
            )
            == original
        )
