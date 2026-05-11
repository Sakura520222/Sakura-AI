"""Configuration live-update coverage for WebUI basic settings."""

from backend.core.config import (
    BASIC_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    Settings,
    get_all_db_config_keys,
    get_settings,
    update_settings_field,
)


def test_basic_review_config_fields_support_live_update():
    required_fields = {
        "max_concurrent_reviews",
        "review_timeout_seconds",
        "enable_auto_review",
    }
    assert required_fields.issubset(Settings.model_fields)

    settings = get_settings()
    old_values = {key: getattr(settings, key) for key in required_fields}
    try:
        update_settings_field("max_concurrent_reviews", "7")
        update_settings_field("review_timeout_seconds", "45")
        update_settings_field("enable_auto_review", "false")

        assert settings.max_concurrent_reviews == 7
        assert settings.review_timeout_seconds == 45
        assert settings.enable_auto_review is False
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def test_basic_config_keys_are_loaded_from_database_config_keys():
    assert BASIC_CONFIG_KEYS.issubset(set(get_all_db_config_keys()))


def test_ai_api_config_fields_support_live_update():
    required_fields = {
        "ai_api_timeout_seconds",
        "ai_api_max_retries",
        "ai_api_initial_retry_delay_seconds",
        "ai_api_total_timeout_seconds",
    }
    assert required_fields.issubset(Settings.model_fields)
    assert "ai_api" in DYNAMIC_CONFIG_GROUPS
    assert required_fields.issubset(set(DYNAMIC_CONFIG_GROUPS["ai_api"]["keys"]))
    assert required_fields.issubset(set(get_all_db_config_keys()))

    settings = get_settings()
    old_values = {key: getattr(settings, key) for key in required_fields}
    try:
        update_settings_field("ai_api_timeout_seconds", "42.5")
        update_settings_field("ai_api_max_retries", "3")
        update_settings_field("ai_api_initial_retry_delay_seconds", "1.5")
        update_settings_field("ai_api_total_timeout_seconds", "600.5")

        assert settings.ai_api_timeout_seconds == 42.5
        assert settings.ai_api_max_retries == 3
        assert settings.ai_api_initial_retry_delay_seconds == 1.5
        assert settings.ai_api_total_timeout_seconds == 600.5
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)
