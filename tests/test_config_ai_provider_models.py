"""配置 API 账号模型发现硬切换回归测试。"""

import pytest
from pydantic import ValidationError

from backend.api.v1.config import AIModelsRequest


def test_model_discovery_request_uses_saved_account_id():
    request = AIModelsRequest.model_validate(
        {"account_id": "acc_main", "api_key": "legacy", "api_base": "legacy"}
    )
    assert request.account_id == "acc_main"
    assert request.model_dump() == {"account_id": "acc_main"}


def test_model_discovery_request_requires_account_id():
    with pytest.raises(ValidationError):
        AIModelsRequest.model_validate({"api_key": "legacy"})
