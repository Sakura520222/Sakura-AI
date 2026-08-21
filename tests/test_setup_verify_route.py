"""Setup Wizard Token 验证路由测试。"""

from unittest.mock import patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.core.bootstrap import (
    _COOKIE_NAME,
    clear_setup_token,
    generate_setup_token,
    get_setup_token,
)
from backend.webui.routes.setup import router as setup_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(setup_router)
    return app


def test_verify_page_returns_200_in_bootstrap():
    """GET /setup/verify 在 bootstrap 模式返回 200。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.get("/setup/verify")
            assert resp.status_code == 200
            assert "Token" in resp.text or "token" in resp.text
    finally:
        clear_setup_token()


def test_verify_page_redirects_when_not_bootstrap():
    """GET /setup/verify 非 bootstrap 模式重定向到 /auth/login。"""
    clear_setup_token()
    with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=False):
        client = TestClient(_build_app())
        resp = client.get("/setup/verify", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"


def test_verify_page_redirects_if_already_verified():
    """已有有效 Cookie 时，GET /setup/verify 重定向到 /setup。"""
    clear_setup_token()
    generate_setup_token()
    token = get_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app(), cookies={_COOKIE_NAME: token})
            resp = client.get("/setup/verify", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/setup"
    finally:
        clear_setup_token()


def test_verify_post_correct_token_sets_cookie_and_redirects():
    """POST 正确 Token 后设置 Cookie 并重定向到 /setup。"""
    clear_setup_token()
    generate_setup_token()
    token = get_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.post(
                "/setup/verify",
                data={"token": token},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/setup"
            # Cookie 已设置
            set_cookie = resp.headers.get("set-cookie", "")
            assert _COOKIE_NAME in set_cookie
    finally:
        clear_setup_token()


def test_verify_post_wrong_token_re_renders_with_error():
    """POST 错误 Token 后重新渲染页面，显示错误信息。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.post(
                "/setup/verify",
                data={"token": "wrong-token"},
                follow_redirects=False,
            )
            assert resp.status_code == 200
            assert "无效" in resp.text or "invalid" in resp.text.lower()
    finally:
        clear_setup_token()


def test_setup_page_default_language_is_chinese():
    """无语言参数/Cookie 时默认渲染中文。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.get("/setup")
            assert resp.status_code == 200
            assert 'lang="zh-CN"' in resp.text
            assert "欢迎使用" in resp.text
    finally:
        clear_setup_token()


def test_setup_page_lang_query_renders_english():
    """?lang=en 渲染英文页面。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.get("/setup?lang=en")
            assert resp.status_code == 200
            assert 'lang="en"' in resp.text
            assert "Welcome to Sakura AI" in resp.text
            assert "Database Configuration" in resp.text
    finally:
        clear_setup_token()


def test_setup_page_lang_switch_sets_cookie_and_redirects():
    """?lang= 切换时设置 preferred_language Cookie 并重定向（去掉参数）。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.get("/setup?lang=zh-CN", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/setup"
            assert "preferred_language" in resp.headers.get("set-cookie", "")
    finally:
        clear_setup_token()


def test_setup_page_lang_cookie_persists():
    """preferred_language Cookie 在后续请求中生效。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            client.get("/setup?lang=en")
            resp = client.get("/setup")
            assert resp.status_code == 200
            assert 'lang="en"' in resp.text
            assert "Welcome to Sakura AI" in resp.text
    finally:
        clear_setup_token()


def test_verify_page_lang_switch():
    """verify 页 ?lang= 切换设置 Cookie 并保持英文渲染。"""
    clear_setup_token()
    generate_setup_token()
    try:
        with patch("backend.webui.routes.setup.is_bootstrap_mode", return_value=True):
            client = TestClient(_build_app())
            resp = client.get("/setup/verify?lang=en")
            assert resp.status_code == 200
            assert "Security Verification" in resp.text
            assert "preferred_language" in resp.headers.get("set-cookie", "")
    finally:
        clear_setup_token()
