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
            client = TestClient(_build_app())
            resp = client.get(
                "/setup/verify",
                follow_redirects=False,
                cookies={_COOKIE_NAME: token},
            )
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
