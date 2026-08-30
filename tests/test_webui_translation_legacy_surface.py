"""动态配置翻译不得暴露已硬切换的旧 LLM 供应键。"""

from pathlib import Path

import pytest
import yaml

TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[1] / "backend" / "webui" / "translations"
)
LEGACY_SUPPLIER_KEYS = {
    "ai_provider",
    "openai_api_base",
    "openai_api_key",
    "openai_model",
    "openai_temperature",
    "openai_max_tokens",
    "summary_provider",
    "summary_api_base",
    "summary_api_key",
    "summary_model",
    "agent_team_model_provider",
    "agent_team_api_base",
    "agent_team_api_key",
    "agent_team_model",
    "agent_team_review_model",
    "agent_team_summary_model",
    "scan_model",
}
LEGACY_SAKURA_ROLE_KEYS = {
    "sakura_extraction_provider",
    "sakura_extraction_api_base",
    "sakura_extraction_api_key",
    "sakura_extraction_model",
    "sakura_use_summary_model",
}
LEGACY_DYNAMIC_KEYS = LEGACY_SUPPLIER_KEYS | LEGACY_SAKURA_ROLE_KEYS


@pytest.mark.parametrize("filename", ["zh-CN.yaml", "en.yaml"])
def test_dynamic_config_translations_drop_legacy_llm_supplier_keys(filename):
    """Labels/descriptions/options must not advertise removed dynamic fields."""
    with (TRANSLATIONS_DIR / filename).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)["config"]

    assert LEGACY_DYNAMIC_KEYS.isdisjoint(config.get("label", {}))
    assert LEGACY_DYNAMIC_KEYS.isdisjoint(config.get("desc", {}))
    assert "summary_model" not in config.get("group", {})
    assert not any(
        key.startswith("summary_provider_") for key in config.get("option", {})
    )
    assert "agent_team_model_provider_main" not in config.get("option", {})


@pytest.mark.parametrize("filename", ["zh-CN.yaml", "en.yaml"])
def test_provider_catalog_and_role_binding_translation_surface_remains(filename):
    """Account/catalog and main/summary role-binding copy remains available."""
    with (TRANSLATIONS_DIR / filename).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)["config"]

    assert config["ai_provider"]
    assert config["ai_api_base"]
    assert config["ai_api_key"]
    assert config["option"]["ai_provider_openai"]
    assert config["label"]["embedding_provider"]
    assert config["label"]["embedding_api_key"]
    assert config["label"]["rerank_provider"]
    assert config["label"]["rerank_api_key"]
    assert "role binding" in config["ai_role_bindings_desc"].lower()
    assert "summary" in config["ai_account_editor_desc"].lower()


@pytest.mark.parametrize("filename", ["zh-CN.yaml", "en.yaml"])
def test_issue_vision_dynamic_config_has_localized_label_and_description(filename):
    with (TRANSLATIONS_DIR / filename).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)["config"]

    assert config["label"]["issue_vision_enabled"]
    assert config["desc"]["issue_vision_enabled"]
