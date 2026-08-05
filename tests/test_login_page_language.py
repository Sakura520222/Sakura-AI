"""登录页语言切换与 i18n 渲染测试。"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.core.config import get_settings
from backend.webui.routes.auth import router as auth_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    return app


def test_login_default_language_is_chinese():
    """无语言参数/Cookie 时登录页默认渲染中文。"""
    settings = get_settings()
    settings.github_oauth_client_id = ""
    client = TestClient(_build_app())
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'lang="zh-CN"' in resp.text
    assert "登录" in resp.text


def test_login_lang_query_renders_english():
    """?lang=en 渲染英文登录页。"""
    settings = get_settings()
    settings.github_oauth_client_id = ""
    client = TestClient(_build_app())
    resp = client.get("/auth/login?lang=en")
    assert resp.status_code == 200
    assert 'lang="en"' in resp.text
    assert "AI-Powered GitHub PR Review Platform" in resp.text
    assert "GitHub OAuth is not configured" in resp.text
    # 语言切换器显示中文链接（当前英文时）
    assert "lang=zh-CN" in resp.text


def test_login_lang_switch_sets_cookie_and_redirects():
    """?lang= 切换时设置 preferred_language Cookie 并重定向。"""
    settings = get_settings()
    settings.github_oauth_client_id = ""
    client = TestClient(_build_app())
    resp = client.get("/auth/login?lang=zh-CN", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login"
    assert "preferred_language" in resp.headers.get("set-cookie", "")


def test_login_lang_cookie_persists():
    """preferred_language Cookie 在后续请求中生效。"""
    settings = get_settings()
    settings.github_oauth_client_id = ""
    client = TestClient(_build_app())
    client.get("/auth/login?lang=en")
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'lang="en"' in resp.text
    assert "AI-Powered GitHub PR Review Platform" in resp.text


def test_login_oauth_enabled_shows_english_button():
    """OAuth 已配置时英文页面显示英文 GitHub 登录按钮。"""
    settings = get_settings()
    settings.github_oauth_client_id = "test-client-id"
    client = TestClient(_build_app())
    resp = client.get("/auth/login?lang=en")
    assert resp.status_code == 200
    assert "Sign in with GitHub" in resp.text


def test_login_follows_configured_default_language():
    """无 Cookie/参数时登录页跟随默认界面语言配置（default_language）。"""
    settings = get_settings()
    settings.github_oauth_client_id = ""
    try:
        settings.default_language = "en"
        client = TestClient(_build_app())
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        assert 'lang="en"' in resp.text
        assert "AI-Powered GitHub PR Review Platform" in resp.text

        settings.default_language = "zh-CN"
        client2 = TestClient(_build_app())
        resp2 = client2.get("/auth/login")
        assert resp2.status_code == 200
        assert 'lang="zh-CN"' in resp2.text
        assert "登录" in resp2.text
    finally:
        settings.default_language = "zh-CN"
