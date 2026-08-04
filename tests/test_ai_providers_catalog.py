"""新内置提供商目录测试 / Tests for the rewritten provider catalog."""

from backend.core.ai_protocol.models import ProtocolFamily
from backend.core.ai_providers import (
    AI_PROVIDERS,
    BUILTIN_PROVIDERS,
    build_discovery_endpoint,
    get_ai_provider,
    get_builtin_provider,
    get_provider_select_options,
    list_ai_providers,
)


def test_builtin_catalog_covers_all_families_and_mainstream_vendors():
    ids = set(BUILTIN_PROVIDERS.keys())
    # 官方原生三家 / official native
    assert {"openai", "anthropic", "google"} <= ids
    # 国产自研模型 / vendor-native compatible
    assert {"deepseek", "qwen", "zai", "doubao", "moonshot", "minimax"} <= ids
    # 聚合器 / aggregators
    assert {
        "openrouter",
        "siliconflow",
        "together",
        "groq",
        "fireworks",
        "perplexity",
        "xai",
    } <= ids
    # 本地 / local
    assert {"ollama", "vllm", "lmstudio"} <= ids
    # 自定义 / custom
    assert "custom" in ids


def test_anthropic_and_google_use_native_protocols():
    assert BUILTIN_PROVIDERS["anthropic"].family == ProtocolFamily.ANTHROPIC_NATIVE
    assert BUILTIN_PROVIDERS["google"].family == ProtocolFamily.GEMINI_NATIVE
    assert BUILTIN_PROVIDERS["deepseek"].family == ProtocolFamily.OPENAI_COMPATIBLE


def test_legacy_ai_providers_dict_preserves_compat_keys():
    # 旧配置中的 key 必须仍可解析，避免历史配置失效
    for key in (
        "openai",
        "deepseek",
        "qwen",
        "zai",
        "doubao",
        "siliconflow",
        "anthropic",
        "gemini",
        "custom",
    ):
        assert key in AI_PROVIDERS
    assert get_ai_provider("unknown").id == "custom"
    assert get_ai_provider(None).id == "custom"


def test_provider_select_options_still_work():
    options = get_provider_select_options()
    values = {opt["value"] for opt in options}
    assert {"openai", "anthropic", "custom"} <= values

    summary_options = get_provider_select_options(include_summary_follow=True)
    assert summary_options[0]["value"] == ""

    main_options = get_provider_select_options(include_main_ai=True)
    assert main_options[0]["value"] == "main"


def test_list_ai_providers_keeps_summary_follow_compatibility():
    providers = list_ai_providers(include_summary_follow=True)
    assert providers[0]["id"] == ""
    assert providers[0]["label"].startswith("跟随主模型")


def test_build_discovery_endpoint_resolves_endpoint_object():
    decl, endpoint = build_discovery_endpoint("anthropic", None)
    assert decl.family == ProtocolFamily.ANTHROPIC_NATIVE
    assert endpoint.base_url.startswith("https://api.anthropic.com/v1")
    assert endpoint.auth_scheme.value == "x_api_key"


def test_get_builtin_provider_falls_back_to_custom():
    assert get_builtin_provider("does-not-exist").id == "custom"
    assert get_builtin_provider(None).id == "custom"
