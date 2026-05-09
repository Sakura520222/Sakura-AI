"""Main app helper tests"""

from unittest.mock import patch

from backend.core.config import Settings
from backend.main import _get_webui_error_user
from backend.main import _get_allowed_origins, _should_start_background_tasks


class RequestStub:
    def __init__(self, token: str | None):
        self.cookies = {"webui_token": token} if token else {}


def test_get_webui_error_user_decodes_cookie_payload():
    payload = {
        "sub": "octocat",
        "role": "super_admin",
        "user_id": 123,
        "github_id": 456,
        "avatar_url": "https://example.com/avatar.png",
    }

    with patch("backend.main.decode_access_token", return_value=payload):
        user = _get_webui_error_user(RequestStub("token"))

    assert user == payload


def test_get_webui_error_user_returns_none_without_valid_payload():
    with patch("backend.main.decode_access_token", return_value=None):
        assert _get_webui_error_user(RequestStub("token")) is None

    assert _get_webui_error_user(RequestStub(None)) is None


def test_get_webui_error_user_returns_none_when_decode_fails():
    with patch("backend.main.decode_access_token", side_effect=RuntimeError("boom")):
        assert _get_webui_error_user(RequestStub("token")) is None


def test_get_allowed_origins_includes_local_development_origins():
    settings = Settings(
        sakura_env="development",
        app_domain="example.com",
        app_port=9000,
    )

    origins = _get_allowed_origins(settings)

    assert "https://example.com" in origins
    assert "http://localhost:9000" in origins
    assert "http://127.0.0.1:9000" in origins


def test_get_allowed_origins_production_excludes_localhost():
    settings = Settings(app_domain="example.com", app_port=9000)

    origins = _get_allowed_origins(settings)

    assert "https://example.com" in origins
    assert "http://localhost:9000" not in origins
    assert "http://127.0.0.1:9000" not in origins


def test_background_tasks_can_be_skipped_for_local_development():
    settings = Settings(sakura_skip_background_tasks=True)

    assert _should_start_background_tasks(settings) is False
    assert _should_start_background_tasks(Settings(sakura_dev_bootstrap=True)) is False
    assert _should_start_background_tasks(Settings()) is True
