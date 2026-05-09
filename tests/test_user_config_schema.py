"""User config API schema tests."""

import pytest
from pydantic import ValidationError

from backend.api.v1.schemas import UserConfigUpdateRequest


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
