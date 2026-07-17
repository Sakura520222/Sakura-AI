"""单模型高级覆盖测试 / Per-model advanced override tests."""

import json

import pytest

from backend.api.v1.config import ModelOverrideRequest, put_model_override
from backend.core.ai_protocol.account_store import ProviderAccount
from backend.core.ai_protocol.models import ProtocolFamily
from backend.core.ai_protocol.role_config import (
    _build_candidate_from_account,
    _parse_metadata_overrides,
)


class _Result:
    """最小 SQLAlchemy 查询结果桩 / Minimal SQLAlchemy result stub."""

    def scalar_one_or_none(self):
        return None


class _DbStub:
    """保存模型覆盖所需的最小异步会话桩 / Minimal async session stub."""

    def __init__(self):
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        del statement
        return _Result()

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_model_override_api_persists_adaptive_thinking_mode():
    """思考模式应作为模型级请求参数持久化。"""
    db = _DbStub()
    body = ModelOverrideRequest(
        provider="anthropic",
        model="claude-opus-4-8",
        thinking=True,
        thinking_mode="adaptive",
    )

    response = await put_model_override(body, db, {"sub": "tester"})
    payload = json.loads(response.body)["data"]

    assert payload["reasoning_params"]["thinking"] == {"type": "adaptive"}
    assert db.commits == 1


def test_account_candidate_uses_model_override_metadata():
    """账号角色路径应使用单模型覆盖，而非静态目录元数据。"""
    metadata = _parse_metadata_overrides(
        {
            "ai_model_override.openai.gpt-5.6-sol": json.dumps(
                {
                    "context_window_tokens": 512000,
                    "max_output_tokens": 16384,
                    "capabilities": {"vision": True, "thinking": True},
                    "reasoning_params": {
                        "thinking": {"type": "adaptive"},
                        "max_output_tokens": 16384,
                    },
                }
            )
        }
    )[("openai", "gpt-5.6-sol")]
    account = ProviderAccount(
        id="acc_test",
        name="OpenAI Test",
        provider_id="openai",
        protocol=ProtocolFamily.OPENAI_COMPATIBLE.value,
        api_key="sk-test",
        default_model="gpt-5.6-sol",
    )

    candidate = _build_candidate_from_account(
        account,
        "gpt-5.6-sol",
        metadata_override=metadata,
    )

    assert candidate is not None
    assert candidate.model.context_window_tokens == 512000
    assert candidate.model.capabilities.vision is True
    assert candidate.model.reasoning_params.thinking == {"type": "adaptive"}


def test_model_override_parser_preserves_thinking_and_capabilities():
    """运行时解析应保留用户覆盖的窗口、能力与推理参数。"""
    overrides = _parse_metadata_overrides(
        {
            "ai_model_override.anthropic.claude-opus-4-8": json.dumps(
                {
                    "context_window_tokens": 512000,
                    "max_output_tokens": 16384,
                    "capabilities": {
                        "vision": True,
                        "thinking": True,
                        "effort": True,
                        "temperature": False,
                        "top_p": False,
                    },
                    "reasoning_params": {
                        "max_output_tokens": 16384,
                        "thinking": {"type": "adaptive"},
                        "effort": "xhigh",
                    },
                }
            )
        }
    )

    metadata = overrides[("anthropic", "claude-opus-4-8")]
    assert metadata.context_window_tokens == 512000
    assert metadata.max_output_tokens == 16384
    assert metadata.capabilities.vision is True
    assert metadata.capabilities.thinking is True
    assert metadata.capabilities.effort is True
    assert metadata.capabilities.temperature is False
    assert metadata.reasoning_params.thinking == {"type": "adaptive"}
    assert metadata.reasoning_params.effort == "xhigh"
