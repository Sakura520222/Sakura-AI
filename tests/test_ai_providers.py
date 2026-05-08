from backend.core.ai_providers import (
    build_models_url,
    extract_context_window_k,
    get_ai_provider,
    get_provider_select_options,
    normalize_model_list_response,
)
from backend.core.config import (
    CORE_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
)


def test_ai_provider_registry_contains_common_providers():
    assert get_ai_provider("openai").base_url == "https://api.openai.com/v1"
    assert get_ai_provider("deepseek").default_model == "deepseek-chat"
    assert get_ai_provider("unknown").id == "custom"


def test_provider_select_options_support_summary_follow():
    options = get_provider_select_options(include_summary_follow=True)
    assert options[0] == {"value": "", "label": "跟随主模型"}
    assert {opt["value"] for opt in options} >= {"openai", "deepseek", "custom"}


def test_build_models_url_uses_provider_default_or_override():
    assert build_models_url("openai") == "https://api.openai.com/v1/models"
    assert build_models_url("deepseek", "https://proxy.example/v1") == (
        "https://proxy.example/v1/models"
    )


def test_normalize_model_list_response_openai_format():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
    assert normalize_model_list_response(payload) == ["gpt-4o", "gpt-4o-mini"]


def test_extract_context_window_k_from_common_fields():
    assert extract_context_window_k({"context_length": 128000}) == 128
    assert extract_context_window_k({"metadata": {"max_model_len": 64000}}) == 64
    assert extract_context_window_k({"context_window": 200}) == 200


def test_ai_provider_dynamic_config_registered():
    assert "ai_provider" in DYNAMIC_CONFIG_GROUPS["ai_model"]["keys"]
    assert "summary_provider" in DYNAMIC_CONFIG_GROUPS["summary_model"]["keys"]
    assert DYNAMIC_CONFIG_LABELS["ai_provider"] == "AI 厂商"
    assert DYNAMIC_CONFIG_LABELS["summary_provider"] == "辅助模型厂商"
    assert "ai_provider" in DYNAMIC_CONFIG_SELECT_OPTIONS
    assert "summary_provider" in DYNAMIC_CONFIG_SELECT_OPTIONS
    assert "ai_provider" in CORE_CONFIG_KEYS
    assert "summary_provider" in CORE_CONFIG_KEYS
