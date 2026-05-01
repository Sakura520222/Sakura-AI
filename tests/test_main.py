"""Main app helper tests"""

from unittest.mock import patch

from backend.main import _get_webui_error_user


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
