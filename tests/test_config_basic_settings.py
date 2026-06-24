"""Configuration live-update coverage for WebUI basic settings."""

from backend.core.config import (
    BASIC_CONFIG_KEYS,
    CORE_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    Settings,
    get_all_db_config_keys,
    get_settings,
    sanitize_domain,
    update_settings_field,
)


def test_basic_review_config_fields_support_live_update():
    required_fields = {
        "max_concurrent_reviews",
        "review_timeout_seconds",
        "enable_auto_review",
        "enable_check_runs",
    }
    assert required_fields.issubset(Settings.model_fields)

    settings = get_settings()
    old_values = {key: getattr(settings, key) for key in required_fields}
    try:
        update_settings_field("max_concurrent_reviews", "7")
        update_settings_field("review_timeout_seconds", "45")
        update_settings_field("enable_auto_review", "false")
        update_settings_field("enable_check_runs", "false")

        assert settings.max_concurrent_reviews == 7
        assert settings.review_timeout_seconds == 45
        assert settings.enable_auto_review is False
        assert settings.enable_check_runs is False
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def test_basic_config_keys_are_loaded_from_database_config_keys():
    assert BASIC_CONFIG_KEYS.issubset(set(get_all_db_config_keys()))


def test_mobile_oauth_allowed_redirect_uris_is_available_in_core_config_paths():
    required_key = "mobile_oauth_allowed_redirect_uris"

    assert required_key in Settings.model_fields
    assert required_key in CORE_CONFIG_KEYS
    assert required_key in get_all_db_config_keys()
    assert DYNAMIC_CONFIG_LABELS[required_key] == "移动端 OAuth 允许回调 URI"


def test_mobile_oauth_allowed_redirect_uris_env_is_accepted_by_setup_service():
    from backend.core import setup_service

    assert (
        setup_service._ENV_TO_SETTINGS_KEY["MOBILE_OAUTH_ALLOWED_REDIRECT_URIS"]
        == "mobile_oauth_allowed_redirect_uris"
    )


def test_mobile_oauth_allowed_redirect_uris_is_exposed_in_system_config_group():
    from backend.services.system_config_service import SYSTEM_CONFIG_GROUPS

    github_oauth_group = next(
        group for group in SYSTEM_CONFIG_GROUPS if group["id"] == "github_oauth"
    )

    assert "mobile_oauth_allowed_redirect_uris" in github_oauth_group["keys"]


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


def test_registration_quota_multiplier_has_own_dynamic_group():
    assert "registration_quota" in DYNAMIC_CONFIG_GROUPS
    assert DYNAMIC_CONFIG_GROUPS["registration_quota"]["keys"] == [
        "register_quota_multiplier"
    ]
    assert "register_quota_multiplier" not in DYNAMIC_CONFIG_GROUPS["init_quota"][
        "keys"
    ]
    assert "register_quota_multiplier" in get_all_db_config_keys()


def test_web_search_configs_have_range_limits_and_fetch_url_configs_do_not():
    from backend.core.config import DYNAMIC_CONFIG_RANGES

    assert DYNAMIC_CONFIG_RANGES["web_search_max_results"] == (1, 100)
    assert DYNAMIC_CONFIG_RANGES["web_search_max_content_length"] == (100, 50000)
    assert DYNAMIC_CONFIG_RANGES["web_search_timeout"] == (5, 600)

    unrestricted_fetch_url_keys = {
        "fetch_url_timeout",
        "fetch_url_max_content_length",
        "fetch_url_max_download_size",
        "fetch_url_max_calls_per_session",
        "fetch_url_max_redirects",
    }

    assert unrestricted_fetch_url_keys.isdisjoint(DYNAMIC_CONFIG_RANGES)


class TestSanitizeDomain:
    """Cover sanitize_domain edge cases."""

    def test_plain_domain(self):
        assert sanitize_domain("example.com") == "example.com"

    def test_empty_string(self):
        assert sanitize_domain("") == ""

    def test_none_input(self):
        assert sanitize_domain(None) == ""

    def test_strip_whitespace(self):
        assert sanitize_domain("  example.com  ") == "example.com"

    def test_remove_https_prefix(self):
        assert sanitize_domain("https://example.com") == "example.com"

    def test_remove_http_prefix(self):
        assert sanitize_domain("http://example.com") == "example.com"

    def test_remove_trailing_slash(self):
        assert sanitize_domain("example.com/") == "example.com"

    def test_https_prefix_and_trailing_slash(self):
        assert sanitize_domain("https://example.com/") == "example.com"

    def test_http_prefix_and_trailing_slash(self):
        assert sanitize_domain("http://example.com/") == "example.com"

    def test_whitespace_and_prefix_and_slash(self):
        assert sanitize_domain("  https://example.com/  ") == "example.com"

    def test_multiple_trailing_slashes(self):
        assert sanitize_domain("example.com//") == "example.com"

    def test_https_prefix_and_multiple_trailing_slashes(self):
        assert sanitize_domain("https://example.com///") == "example.com"
