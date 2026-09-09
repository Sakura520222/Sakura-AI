"""关于页面与 /settings/about API 版本信息测试（Issue #534）。

覆盖：GitHub 仓库/文档/Issue 反馈外链渲染、更新频道徽章、
构建修订短哈希链接，以及 API 返回的构建身份字段。
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.api.v1.deps import require_api_auth
from backend.api.v1.settings import router as api_settings_router
from backend.core.branding import SAKURA_AI_REPO_URL
from backend.webui.deps import get_user_preferences, require_auth
from backend.webui.routes.settings import router as settings_router

_REVISION = "a1b2c3d" + "0" * 33


def _build_webui_app() -> FastAPI:
    app = FastAPI()
    app.include_router(settings_router)
    app.dependency_overrides[require_auth] = lambda: {
        "user_id": 1,
        "sub": "tester",
        "role": "super_admin",
    }
    app.dependency_overrides[get_user_preferences] = lambda: {
        "language": "zh-CN",
        "items_per_page": 20,
    }
    return app


def _build_api_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_settings_router)
    app.dependency_overrides[require_api_auth] = lambda: {
        "user_id": 1,
        "sub": "tester",
        "role": "super_admin",
    }
    return app


def test_about_page_renders_project_links():
    """关于页渲染 GitHub 仓库、文档与 Issue 反馈外链。"""
    client = TestClient(_build_webui_app())
    resp = client.get("/settings/about")
    assert resp.status_code == 200
    assert SAKURA_AI_REPO_URL in resp.text
    assert f"{SAKURA_AI_REPO_URL}/issues" in resp.text
    assert f"{SAKURA_AI_REPO_URL}#readme" in resp.text


def test_about_page_source_channel_badge():
    """无构建环境变量时（源码部署）渲染源码频道徽章。"""
    client = TestClient(_build_webui_app())
    resp = client.get("/settings/about")
    assert resp.status_code == 200
    assert "源码" in resp.text


def test_about_page_stable_channel_with_revision_link():
    """注入镜像构建环境变量时渲染稳定徽章、构建日期与 commit 短哈希链接。"""
    import os

    os.environ["SAKURA_BUILD_CHANNEL"] = "stable"
    os.environ["SAKURA_BUILD_REVISION"] = _REVISION
    os.environ["SAKURA_BUILD_CREATED"] = "2026-01-15T12:00:00Z"
    try:
        client = TestClient(_build_webui_app())
        resp = client.get("/settings/about")
    finally:
        for key in (
            "SAKURA_BUILD_CHANNEL",
            "SAKURA_BUILD_REVISION",
            "SAKURA_BUILD_CREATED",
        ):
            os.environ.pop(key, None)
    assert resp.status_code == 200
    assert "稳定版" in resp.text
    assert f"{SAKURA_AI_REPO_URL}/commit/{_REVISION}" in resp.text
    assert _REVISION[:7] in resp.text


def test_api_about_returns_build_identity():
    """GET /settings/about 返回版本、频道、修订与仓库地址。"""
    import os

    os.environ["SAKURA_BUILD_CHANNEL"] = "development"
    os.environ["SAKURA_BUILD_REVISION"] = _REVISION
    try:
        client = TestClient(_build_api_app())
        resp = client.get("/settings/about")
    finally:
        os.environ.pop("SAKURA_BUILD_CHANNEL", None)
        os.environ.pop("SAKURA_BUILD_REVISION", None)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["success"] is True
    data = payload["data"]
    assert data["version"]
    assert data["channel"] == "development"
    assert data["revision"] == _REVISION
    assert data["repo_url"] == SAKURA_AI_REPO_URL
    assert data["build_date"]
