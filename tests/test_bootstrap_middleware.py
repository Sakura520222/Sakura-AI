"""BootstrapMiddleware HTTP 行为契约测试。

锁住中间件的对外行为（放行/拦截/重定向/503），使得将其从
``BaseHTTPMiddleware`` 改写为纯 ASGI 中间件时能验证行为等价。
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient

from backend.core.bootstrap import BootstrapMiddleware


def _build_app() -> FastAPI:
    """构造挂载了 BootstrapMiddleware 的最小 FastAPI 应用。"""
    app = FastAPI()

    @app.get("/")
    async def root() -> PlainTextResponse:
        return PlainTextResponse("home")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/data")
    async def api_data():
        return {"data": "ok"}

    @app.get("/some/page")
    async def some_page() -> PlainTextResponse:
        return PlainTextResponse("page")

    @app.get("/static/app.css")
    async def static_css() -> PlainTextResponse:
        return PlainTextResponse("body{}", media_type="text/css")

    app.add_middleware(BootstrapMiddleware)
    return app


def test_not_bootstrap_mode_passes_all_through():
    """非 bootstrap 模式：所有请求直接放行，中间件不干预。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=False):
        client = TestClient(_build_app())
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.json() == {"data": "ok"}


def test_bootstrap_mode_redirects_root_to_setup():
    """bootstrap 模式：根路径重定向到 /setup（302）。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=True):
        client = TestClient(_build_app())
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/setup"


def test_bootstrap_mode_allowed_paths_pass_through():
    """bootstrap 模式：ALLOWED_PATHS（如 /health）放行。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=True):
        client = TestClient(_build_app())
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_bootstrap_mode_api_returns_503():
    """bootstrap 模式：API 路径返回 503 JSON 提示。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=True):
        client = TestClient(_build_app())
        resp = client.get("/api/data")
        assert resp.status_code == 503
        assert resp.json()["detail"]


def test_bootstrap_mode_page_redirects_to_setup():
    """bootstrap 模式：普通页面请求重定向到 /setup（302）。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=True):
        client = TestClient(_build_app())
        resp = client.get("/some/page", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/setup"


def test_bootstrap_mode_static_assets_pass_through():
    """bootstrap 模式：静态资源（/static、.css/.js/.ico）放行。"""
    with patch("backend.core.bootstrap.is_bootstrap_mode", return_value=True):
        client = TestClient(_build_app())
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
