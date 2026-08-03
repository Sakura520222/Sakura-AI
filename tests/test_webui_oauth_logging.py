"""OAuth logging must not expose credentials or callback secrets."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse

from backend.api.v1 import auth as api_auth
from backend.api.v1.schemas import OAuthCallbackRequest
from backend.webui.routes import auth as webui_auth


SENSITIVE_VALUES = (
    "state-value",
    "authorization-code",
    "access-token",
    "query-token",
)


class RecordingLogger:
    """Capture logger arguments so tests can inspect the logging contract."""

    def __init__(self):
        self.calls = []
        self.exception_calls = []

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    def debug(self, *args, **kwargs):
        self._record("debug", *args, **kwargs)

    def info(self, *args, **kwargs):
        self._record("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._record("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._record("error", *args, **kwargs)

    def exception(self, *args, **kwargs):
        exception_text = str(sys.exception())
        self.exception_calls.append(exception_text)
        self._record("exception", *args, exception_text, **kwargs)


class FakeAsyncClient:
    def __init__(self, *, post_response=None, post_error=None, get_response=None):
        self.post_response = post_response
        self.post_error = post_error
        self.get_response = get_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def post(self, *args, **kwargs):
        if self.post_error is not None:
            raise self.post_error
        return self.post_response

    async def get(self, *args, **kwargs):
        return self.get_response


class FakeResponse:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        return self._json_data


def _settings():
    return SimpleNamespace(
        github_oauth_client_id="client-id",
        github_oauth_client_secret="client-secret",
        github_oauth_redirect_uri="https://example.test/auth/callback",
        github_oauth_auth_url="https://github.com/login/oauth/authorize",
        github_oauth_token_url="https://github.com/login/oauth/access_token",
        github_oauth_user_url="https://api.github.com/user",
    )


def _oauth_error_response(_request, error_message, **kwargs):
    return JSONResponse(
        {"error": error_message}, status_code=kwargs.get("status_code", 400)
    )


def _response_error(response):
    return json.loads(response.body)["error"]


def _assert_no_sensitive_logs(logger):
    rendered_calls = repr(logger.calls) + repr(logger.exception_calls)
    for value in SENSITIVE_VALUES:
        assert value not in rendered_calls


def _webui_callback(monkeypatch, *, client_factory, logger):
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(webui_auth, "_get_oauth_state", lambda _state: None)
    monkeypatch.setattr(webui_auth, "httpx", httpx)
    monkeypatch.setattr(webui_auth.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)


@pytest.mark.anyio
async def test_github_login_redirect_does_not_log_state(monkeypatch):
    logger = RecordingLogger()
    saved = {}

    async def save_state(state, redirect):
        saved.update(state=state, redirect=redirect)

    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(webui_auth.secrets, "token_urlsafe", lambda _length: "state-value")
    monkeypatch.setattr(webui_auth, "_save_oauth_state", save_state)
    monkeypatch.setattr(webui_auth, "logger", logger)

    response = await webui_auth.github_login(None)

    assert response.status_code == 302
    assert "state=state-value" in response.headers["location"]
    assert saved == {"state": "state-value", "redirect": "/"}
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_github_callback_state_failure_does_not_log_state(monkeypatch):
    logger = RecordingLogger()

    async def get_state(_state):
        return None

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 400
    assert _response_error(response) == "无效的授权请求，请重新登录"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_github_callback_non_200_token_response_does_not_log_body(
    monkeypatch,
):
    logger = RecordingLogger()
    token_response = FakeResponse(
        401,
        text="error=access-token&state=state-value",
    )

    async def get_state(state):
        assert state == "state-value"
        return {"redirect": "/"}

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(
        webui_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_response=token_response),
    )
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 400
    assert _response_error(response) == "获取访问令牌失败，请重试"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_github_callback_missing_access_token_does_not_log_token_data(
    monkeypatch,
):
    logger = RecordingLogger()
    token_response = FakeResponse(
        200,
        text='{"error": "access-token"}',
        json_data={"error": "access-token"},
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(
        webui_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_response=token_response),
    )
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 400
    assert _response_error(response) == "获取访问令牌失败，请重试"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_github_callback_request_error_does_not_log_exception_text(monkeypatch):
    logger = RecordingLogger()
    request_error = httpx.RequestError(
        "request URL contains state-value authorization-code access-token"
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(
        webui_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_error=request_error),
    )
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 502
    assert _response_error(response) == "网络连接失败，请重试"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_api_github_callback_non_200_does_not_log_token_response_body(
    monkeypatch,
):
    logger = RecordingLogger()
    token_response = FakeResponse(
        401,
        text="access-token=token-response-body&state=state-value",
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(api_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(api_auth, "get_settings", _settings)
    monkeypatch.setattr(
        api_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_response=token_response),
    )
    monkeypatch.setattr(api_auth, "logger", logger)

    endpoint = getattr(api_auth.github_callback, "__wrapped__", api_auth.github_callback)
    response = await endpoint(
        None,
        OAuthCallbackRequest(code="authorization-code", state="state-value"),
    )

    assert response.status_code == 502
    assert _response_error(response) == "获取访问令牌失败"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_webui_callback_unexpected_exception_does_not_render_exception(
    monkeypatch,
):
    logger = RecordingLogger()
    unexpected = RuntimeError(
        "GET https://github.example/callback?state=state-value"
        "&code=authorization-code&access_token=access-token&query=query-token"
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(
        webui_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_error=unexpected),
    )
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 502
    assert _response_error(response) == "登录过程中发生错误，请重试"
    assert all(method != "exception" for method, _, _ in logger.calls)
    assert logger.calls
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_webui_callback_missing_login_does_not_log_user_response(
    monkeypatch,
):
    logger = RecordingLogger()
    token_response = FakeResponse(
        200,
        json_data={"access_token": "access-token"},
    )
    user_response = FakeResponse(
        200,
        json_data={
            "access_token": "access-token",
            "state": "state-value",
            "code": "authorization-code",
        },
    )
    client = FakeAsyncClient(
        post_response=token_response,
        get_response=user_response,
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(webui_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(webui_auth, "get_settings", _settings)
    monkeypatch.setattr(webui_auth.httpx, "AsyncClient", lambda: client)
    monkeypatch.setattr(webui_auth, "logger", logger)
    monkeypatch.setattr(webui_auth, "_oauth_error", _oauth_error_response)

    response = await webui_auth.github_callback(
        None,
        code="authorization-code",
        state="state-value",
        error=None,
        error_description=None,
    )

    assert response.status_code == 400
    assert _response_error(response) == "无法获取 GitHub 用户信息"
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_api_mobile_authorize_rejection_does_not_log_redirect_query(
    monkeypatch,
):
    logger = RecordingLogger()
    settings = _settings()
    settings.mobile_oauth_allowed_redirect_uris = "https://safe.invalid/callback"
    redirect_uri = (
        "https://evil.invalid/callback?state=state-value"
        "&code=authorization-code&access_token=access-token&query=query-token"
    )

    monkeypatch.setattr(api_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(api_auth, "logger", logger)

    endpoint = getattr(
        api_auth.github_mobile_authorize,
        "__wrapped__",
        api_auth.github_mobile_authorize,
    )
    response = await endpoint(None, redirect_uri=redirect_uri)

    assert response.status_code == 400
    assert _response_error(response) == "不支持的回调地址"
    assert redirect_uri not in repr(logger.calls)
    _assert_no_sensitive_logs(logger)


@pytest.mark.anyio
async def test_api_github_callback_unexpected_exception_does_not_render_exception(
    monkeypatch,
):
    logger = RecordingLogger()
    unexpected = RuntimeError(
        "GET https://github.example/callback?state=state-value"
        "&code=authorization-code&access_token=access-token&query=query-token"
    )

    async def get_state(_state):
        return {"redirect": "/"}

    monkeypatch.setattr(api_auth, "_get_oauth_state", get_state)
    monkeypatch.setattr(api_auth, "get_settings", _settings)
    monkeypatch.setattr(
        api_auth.httpx,
        "AsyncClient",
        lambda: FakeAsyncClient(post_error=unexpected),
    )
    monkeypatch.setattr(api_auth, "logger", logger)

    endpoint = getattr(api_auth.github_callback, "__wrapped__", api_auth.github_callback)
    response = await endpoint(
        None,
        OAuthCallbackRequest(code="authorization-code", state="state-value"),
    )

    assert response.status_code == 500
    assert _response_error(response) == "登录过程中发生错误"
    assert all(method != "exception" for method, _, _ in logger.calls)
    assert logger.calls
    _assert_no_sensitive_logs(logger)
