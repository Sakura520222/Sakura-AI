from backend.core.ai_providers import (
    AI_PROVIDERS,
    AIProvider,
    build_model_detail_url,
    build_models_url,
    extract_context_window_k,
    get_ai_provider,
    get_provider_select_options,
    list_ai_providers,
    normalize_model_list_response,
)
from backend.core.config import (
    CORE_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
)


def test_ai_provider_registry_contains_common_providers():
    assert get_ai_provider("openai").base_url == "https://api.openai.com/v1"
    assert get_ai_provider("deepseek").default_model == "deepseek-v4-pro"
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


def test_ai_provider_dynamic_config_migrated_to_ai_config_page():
    # AI 模型/辅助模型/AI API/上下文管理已迁移到 /config/ai，
    # 不再出现在全局动态配置页，避免多账号配置与旧扁平字段双写。
    assert "ai_model" not in DYNAMIC_CONFIG_GROUPS
    assert "summary_model" not in DYNAMIC_CONFIG_GROUPS
    assert "ai_api" not in DYNAMIC_CONFIG_GROUPS
    assert "context" not in DYNAMIC_CONFIG_GROUPS
    assert "ai_provider" not in DYNAMIC_CONFIG_SELECT_OPTIONS
    assert "summary_provider" not in DYNAMIC_CONFIG_SELECT_OPTIONS
    assert "ai_provider" not in CORE_CONFIG_KEYS
    assert "summary_provider" not in CORE_CONFIG_KEYS


def test_list_ai_providers_default():
    providers = list_ai_providers()
    ids = {p["id"] for p in providers}
    assert "openai" in ids
    assert "deepseek" in ids
    assert "custom" in ids
    # every entry is a valid dict from AIProvider.to_public_dict()
    for p in providers:
        assert "id" in p
        assert "base_url" in p
        assert "supports_model_list" in p


def test_list_ai_providers_with_summary_follow():
    providers = list_ai_providers(include_summary_follow=True)
    assert providers[0]["id"] == ""
    assert providers[0]["label"] == "跟随主模型 / Follow main model"
    assert providers[0]["supports_model_list"] is False
    # remaining providers are normal
    ids = {p["id"] for p in providers[1:]}
    assert "openai" in ids


def test_build_model_detail_url():
    url = build_model_detail_url("openai", "gpt-4o")
    assert url == "https://api.openai.com/v1/models/gpt-4o"
    url_custom = build_model_detail_url("deepseek", "deepseek-chat", "https://proxy/v1")
    assert url_custom == "https://proxy/v1/models/deepseek-chat"


def test_build_models_url_strips_leading_slash():
    """Ensure models_endpoint starting with '/' is handled correctly."""
    AI_PROVIDERS["slash-test"] = AIProvider(
        id="slash-test",
        label="Slash Test",
        base_url="https://example.com/v1",
        default_model="test-model",
        models_endpoint="/models",
    )
    try:
        assert build_models_url("slash-test") == "https://example.com/v1/models"
    finally:
        AI_PROVIDERS.pop("slash-test", None)


def test_ai_provider_frozen_and_to_public_dict():
    p = AIProvider(
        id="test", label="Test", base_url="http://localhost/v1", default_model="t"
    )
    d = p.to_public_dict()
    assert d["id"] == "test"
    assert d["models_endpoint"] == "models"


def test_normalize_model_list_response_variants():
    # plain list of strings
    assert normalize_model_list_response(["a", "b"]) == ["a", "b"]
    # dict with 'models' key
    assert normalize_model_list_response({"models": ["x"]}) == ["x"]
    # dict with 'items' key
    assert normalize_model_list_response({"items": [{"id": "y"}]}) == ["y"]
    # empty / unknown
    assert normalize_model_list_response("not a dict") == []
