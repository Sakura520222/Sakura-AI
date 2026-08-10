"""AI 账号持久化、目录与角色解析测试。"""

import pytest

from backend.api.v1.config import AccountSaveRequest, save_ai_account
from backend.core.ai_protocol.account_store import ProviderAccount
from backend.core.ai_protocol.models import ProtocolFamily
from backend.core.ai_protocol.registry import resolve_account_endpoint
from backend.core.ai_protocol.role_config import _build_candidate_from_account
from backend.core.ai_providers import get_builtin_provider, list_provider_catalog


@pytest.mark.asyncio
async def test_account_save_rejects_unknown_provider_id():
    """账号保存不应将拼错的 provider 静默降级为 custom。"""
    response = await save_ai_account(
        AccountSaveRequest(
            name="Typo Provider",
            provider_id="opneai",
            api_base="http://localhost:8080/v1",
        ),
        {"sub": "tester"},
    )

    assert response.status_code == 400
    assert "unknown AI provider" in response.body.decode()


def test_provider_catalog_uses_latest_2026_baseline_models():
    catalog = {p["id"]: p for p in list_provider_catalog()}

    assert catalog["openai"]["default_models"][0] == "gpt-5.6-sol"
    assert "openai_responses" in catalog["openai"]["supported_families"]
    assert "openai-compatible" in catalog["openai"]["supported_families"]
    assert catalog["anthropic"]["default_models"][0] == "claude-fable-5"
    assert catalog["google"]["default_models"] == ["gemini-3.5-flash"]
    assert catalog["deepseek"]["default_models"] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
    assert catalog["qwen"]["default_models"][0] == "qwen3.7-plus"
    assert catalog["glm"]["default_models"][0] == "glm-5.2"
    assert catalog["minimax"]["default_models"] == ["MiniMax-M3"]
    assert catalog["moonshot"]["default_models"][0] == "kimi-k2.7-code"


def test_coding_plan_entries_mark_restricted_usage_scope():
    catalog = {p["id"]: p for p in list_provider_catalog()}

    assert catalog["qwen-coding-plan"]["billing_mode"] == "coding_plan"
    assert catalog["qwen-coding-plan"]["usage_scope"] == "interactive_coding_only"
    assert "禁止用于" in catalog["qwen-coding-plan"]["usage_scope_note"]
    assert catalog["glm-coding-plan"]["billing_mode"] == "coding_plan"
    assert catalog["minimax-token-plan"]["billing_mode"] == "token_plan"


def test_account_endpoint_resolution_uses_account_protocol_family():
    deepseek = get_builtin_provider("deepseek")

    anth_ep = resolve_account_endpoint(
        deepseek,
        family=ProtocolFamily.ANTHROPIC_NATIVE,
        base_url="",
    )
    assert anth_ep.base_url == "https://api.deepseek.com/anthropic/"
    assert anth_ep.chat_path == "messages"
    assert anth_ep.auth_scheme.value == "x_api_key"

    openai_ep = resolve_account_endpoint(
        deepseek,
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="",
    )
    assert openai_ep.base_url == "https://api.deepseek.com/"
    assert openai_ep.chat_path == "chat/completions"
    assert openai_ep.auth_scheme.value == "bearer"


def test_build_candidate_from_account_uses_builtin_model_metadata():
    account = ProviderAccount(
        id="acc_test",
        name="OpenAI Test",
        provider_id="openai",
        protocol=ProtocolFamily.OPENAI_COMPATIBLE.value,
        api_key="sk-test",
        default_model="gpt-5.6-sol",
    )

    candidate = _build_candidate_from_account(account, "gpt-5.6-sol")

    assert candidate is not None
    assert candidate.provider.id == "openai"
    assert candidate.model.model_id == "gpt-5.6-sol"
    assert candidate.model.context_window_tokens == 1_050_000
    assert candidate.model.max_output_tokens == 131_072
    assert candidate.credential == "sk-test"


def test_build_candidate_from_account_preserves_effective_protocol():
    """账号协议覆盖 provider 默认族时，candidate 必须保留该协议。"""
    account = ProviderAccount(
        id="acc_deepseek_anthropic",
        name="DeepSeek Anthropic",
        provider_id="deepseek",
        protocol=ProtocolFamily.ANTHROPIC_NATIVE.value,
        api_key="sk-test",
        default_model="deepseek-v4-pro",
    )

    candidate = _build_candidate_from_account(account, "deepseek-v4-pro")

    assert candidate is not None
    assert candidate.provider.family == ProtocolFamily.OPENAI_COMPATIBLE
    assert candidate.protocol == ProtocolFamily.ANTHROPIC_NATIVE
    assert candidate.effective_protocol == ProtocolFamily.ANTHROPIC_NATIVE
    assert candidate.endpoint.chat_path == "messages"
    assert candidate.endpoint.auth_scheme.value == "x_api_key"


def test_provider_catalog_serializes_model_capabilities():
    catalog = {p["id"]: p for p in list_provider_catalog()}
    claude = catalog["anthropic"]["models"][0]
    gemini = catalog["google"]["models"][0]

    assert claude["capabilities"]["thinking"] is True
    assert claude["capabilities"]["vision"] is True
    assert gemini["capabilities"]["top_k"] is True
    assert gemini["context_window_tokens"] == 1_048_576
