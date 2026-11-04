"""关于页 /settings/about API 版本信息测试（Issue #534）。

覆盖：API 返回的构建身份字段（版本、频道、修订、仓库地址、构建日期）。
关于页 HTML 渲染已随 SPA 迁移移除，页面行为覆盖在 web/ 前端测试中。
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.api.v1.deps import require_api_auth
from backend.api.v1.settings import router as api_settings_router
from backend.core.branding import SAKURA_AI_REPO_URL

_REVISION = "a1b2c3d" + "0" * 33


def _build_api_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_settings_router)
    app.dependency_overrides[require_api_auth] = lambda: {
        "user_id": 1,
        "sub": "tester",
        "role": "super_admin",
    }
    return app


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
