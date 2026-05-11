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


def test_fetch_url_dynamic_config_fields_support_live_update():
    required_fields = {
        "fetch_url_allowed_content_types",
        "fetch_url_max_redirects",
    }
    assert required_fields.issubset(Settings.model_fields)
    assert "fetch_url" in DYNAMIC_CONFIG_GROUPS
    assert required_fields.issubset(set(DYNAMIC_CONFIG_GROUPS["fetch_url"]["keys"]))
    assert required_fields.issubset(set(get_all_db_config_keys()))

    settings = get_settings()
    old_values = {key: getattr(settings, key) for key in required_fields}
    try:
        update_settings_field("fetch_url_allowed_content_types", "text/plain,text/html")
        update_settings_field("fetch_url_max_redirects", "12")

        assert settings.fetch_url_allowed_content_types == "text/plain,text/html"
        assert settings.fetch_url_max_redirects == 12
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def test_web_search_and_fetch_url_configs_have_no_range_limits():
    from backend.core.config import DYNAMIC_CONFIG_RANGES

    unrestricted_keys = {
        "web_search_max_results",
        "web_search_max_content_length",
        "web_search_timeout",
        "fetch_url_timeout",
        "fetch_url_max_content_length",
        "fetch_url_max_download_size",
        "fetch_url_max_calls_per_session",
        "fetch_url_max_redirects",
    }

    assert unrestricted_keys.isdisjoint(DYNAMIC_CONFIG_RANGES)
