"""Security boundaries for the generic and dedicated AI configuration APIs."""

import json

import pytest

from backend.api.v1.config import (
    RoleBindingSaveRequest,
    _mask_sensitive,
    save_ai_bindings,
    update_general_config,
    update_labels,
)
from backend.api.v1.schemas import ConfigGeneralUpdateRequest, ConfigLabelsUpdateRequest
from backend.core.ai_protocol import account_store
from backend.core.ai_protocol.account_store import ProviderAccount


def _response_json(response):
    return json.loads(response.body)


def test_generic_config_masks_api_key_inside_ai_account_json():
    stored = json.dumps(
        {
            "id": "acc_demo",
            "name": "Demo",
            "api_key": "sk-full-plaintext-secret",
            "api_base": "https://api.example.com/v1",
        }
    )

    masked = json.loads(_mask_sensitive(stored, "ai_account.acc_demo"))

    assert masked["api_key"] == "****"
    assert masked["has_key"] is True
    assert "sk-full-plaintext-secret" not in json.dumps(masked)


def test_generic_config_fails_closed_for_malformed_ai_account_json():
    assert _mask_sensitive("not-json", "ai_account.acc_demo") == "****"


@pytest.mark.asyncio
async def test_role_binding_rejects_unknown_account(monkeypatch):
    async def list_accounts():
        return []

    async def unexpected_save(_bindings):
        raise AssertionError("invalid bindings must not be persisted")

    monkeypatch.setattr(account_store, "list_accounts", list_accounts)
    monkeypatch.setattr(account_store, "save_role_bindings", unexpected_save)

    response = await save_ai_bindings(
        RoleBindingSaveRequest(
            bindings={
                "main": {
                    "primary": {"account": "missing", "model": "gpt-test"},
                    "fallback": [],
                }
            }
        ),
        user={"sub": "admin"},
    )

    assert response.status_code == 400
    assert "不存在的 AI 账号" in _response_json(response)["error"]


@pytest.mark.asyncio
async def test_role_binding_validates_and_persists_normalized_config(monkeypatch):
    saved = {}

    async def list_accounts():
        return [
            ProviderAccount(
                id="acc_main",
                name="Main",
                provider_id="openai",
            )
        ]

    async def save_role_bindings(bindings):
        saved.update(bindings)

    monkeypatch.setattr(account_store, "list_accounts", list_accounts)
    monkeypatch.setattr(account_store, "save_role_bindings", save_role_bindings)

    response = await save_ai_bindings(
        RoleBindingSaveRequest(
            bindings={
                "main": {
                    "primary": {"account": "acc_main", "model": "gpt-test"},
                    "fallback": [],
                },
                "summary": {
                    "primary": {"account": "main", "model": "follow"},
                    "fallback": [],
                },
            }
        ),
        user={"sub": "admin"},
    )

    assert response.status_code == 200
    assert set(saved) == {"main", "summary"}
    assert saved["main"].primary.account == "acc_main"


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved_key", ["ai_account.acc_demo", "ai_role_bindings"])
async def test_generic_config_rejects_ai_account_and_binding_writes(reserved_key):
    response = await update_general_config(
        ConfigGeneralUpdateRequest(configs={reserved_key: "{}"}),
        db=object(),
        user={"sub": "admin"},
    )

    assert response.status_code == 400
    assert "专用配置接口" in _response_json(response)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section_key", ["strategy.issue_analysis", "label.definitions"]
)
async def test_generic_config_rejects_section_writes(section_key):
    response = await update_general_config(
        ConfigGeneralUpdateRequest(configs={section_key: "{}"}),
        db=object(),
        user={"sub": "admin"},
    )

    assert response.status_code == 400
    assert "配置节" in _response_json(response)["error"]


@pytest.mark.asyncio
async def test_labels_update_converts_legacy_list_to_section_mapping(monkeypatch):
    saved = {}

    class SectionConfigRecorder:
        async def save_section(self, _db, section_key, data):
            saved["section"] = section_key
            saved["data"] = data
            return {"changed": True}

    class LabelServiceRecorder:
        def reload_labels(self):
            saved["reloaded"] = True

    monkeypatch.setattr(
        "backend.api.v1.config.section_config_service", SectionConfigRecorder()
    )
    monkeypatch.setattr("backend.api.v1.config.label_service", LabelServiceRecorder())

    response = await update_labels(
        ConfigLabelsUpdateRequest(
            labels=[
                {
                    "name": "bug",
                    "color": "d73a4a",
                    "description": "Something is broken",
                }
            ]
        ),
        db=object(),
        user={"sub": "admin"},
    )

    assert response.status_code == 200
    assert saved == {
        "section": "label.definitions",
        "data": {"bug": {"color": "d73a4a", "description": "Something is broken"}},
        "reloaded": True,
    }
