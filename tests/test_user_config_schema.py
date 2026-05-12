"""User config API schema tests."""

import pytest
from pydantic import ValidationError

from backend.api.v1.schemas import UserConfigUpdateRequest
from backend.core import config as config_module
from backend.core.config import (
    get_user_dynamic_config,
    validate_user_dynamic_config_value,
)


def test_user_config_update_requires_configs_wrapper():
    with pytest.raises(ValidationError):
        UserConfigUpdateRequest.model_validate({"output_language": "en"})

    request = UserConfigUpdateRequest.model_validate(
        {"configs": {"output_language": "en"}}
    )

    assert request.configs == {"output_language": "en"}


def test_user_config_update_rejects_non_scalar_values():
    with pytest.raises(ValidationError):
        UserConfigUpdateRequest.model_validate(
            {"configs": {"output_language": ["zh-CN", "en"]}}
        )


def test_validate_user_dynamic_config_value_only_allows_safe_keys():
    with pytest.raises(ValueError, match="不允许用户覆盖"):
        validate_user_dynamic_config_value("openai_api_key", "secret")


@pytest.mark.parametrize("value", ["", "zh-CN", "en", None])
def test_validate_user_dynamic_config_value_accepts_output_language(value):
    expected = "" if value is None else value

    assert validate_user_dynamic_config_value("output_language", value) == expected


def test_validate_user_dynamic_config_value_rejects_invalid_output_language():
    with pytest.raises(ValueError, match="output_language"):
        validate_user_dynamic_config_value("output_language", "ja")


@pytest.mark.asyncio
async def test_get_user_dynamic_config_prefers_user_value(monkeypatch):
    async def fake_read_user_config_from_db(user_id: int, key: str):
        assert user_id == 42
        assert key == "output_language"
        return "en"

    async def fake_get_dynamic_config(key: str):
        return "zh-CN"

    monkeypatch.setattr(
        config_module, "_read_user_config_from_db", fake_read_user_config_from_db
    )
    monkeypatch.setattr(config_module, "get_dynamic_config", fake_get_dynamic_config)
    config_module.invalidate_user_dynamic_config_cache()

    assert await get_user_dynamic_config("output_language", 42) == "en"

    config_module.invalidate_user_dynamic_config_cache()


@pytest.mark.asyncio
async def test_get_user_dynamic_config_falls_back_to_global(monkeypatch):
    async def fake_read_user_config_from_db(user_id: int, key: str):
        return None

    async def fake_get_dynamic_config(key: str):
        assert key == "output_language"
        return "zh-CN"

    monkeypatch.setattr(
        config_module, "_read_user_config_from_db", fake_read_user_config_from_db
    )
    monkeypatch.setattr(config_module, "get_dynamic_config", fake_get_dynamic_config)
    config_module.invalidate_user_dynamic_config_cache()

    assert await get_user_dynamic_config("output_language", 42) == "zh-CN"

    config_module.invalidate_user_dynamic_config_cache()
