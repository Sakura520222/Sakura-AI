"""React updater adapters must retain CSRF, target fencing and host job outcomes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1 import deps, version
from backend.webui.deps import generate_csrf_token, get_db

pytestmark = pytest.mark.asyncio
TARGET = {"channel": "development", "version": "3.2.6", "tag": "dev-fixture", "revision": "a" * 40, "digest": "sha256:" + "b" * 64}


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = {"user_id": 1, "sub": "admin", "role": "super_admin"}
    app = FastAPI()
    app.include_router(version.router, prefix="/api/v1")
    app.dependency_overrides[version.require_api_auth] = lambda: actor
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    monkeypatch.setattr(deps, "require_api_auth", AsyncMock(side_effect=lambda _: actor))
    client = SimpleNamespace(
        preflight=AsyncMock(return_value={"data": {"can_update": True, "checks": []}}),
        update=AsyncMock(return_value={"data": {"job_id": "fixture-job"}}),
        get_job=AsyncMock(return_value={"data": {"job_id": "fixture-job", "state": "success", "deployment_verified": False}}),
    )
    registry = SimpleNamespace(list_images=AsyncMock(return_value={"stale": False, "heads": {"development": TARGET}}))
    monkeypatch.setattr(version.legacy, "UpdaterClient", lambda: client)
    monkeypatch.setattr(version.legacy, "_get_registry_client", lambda: registry)
    monkeypatch.setattr(version.legacy, "log_admin_action", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as http:
        yield http, actor, client, registry


async def test_version_mutations_require_csrf_and_current_super_admin(api):
    http, actor, client, _ = api
    body = {"target": TARGET, "confirm_channel_switch": True}
    response = await http.post("/api/v1/version/update", json=body)
    assert response.status_code == 422
    client.update.assert_not_awaited()
    actor["role"] = "admin"
    response = await http.post("/api/v1/version/update", json=body, headers={"X-CSRF-Token": generate_csrf_token()})
    assert response.status_code == 403
    client.update.assert_not_awaited()


async def test_update_keeps_target_identity_confirmation_and_accepted_job(api):
    http, _, client, _ = api
    response = await http.post("/api/v1/version/update", json={"target": TARGET, "confirm_channel_switch": True}, headers={"X-CSRF-Token": generate_csrf_token()})
    assert response.status_code == 202
    assert response.json() == {"data": {"job_id": "fixture-job"}}
    client.update.assert_awaited_once_with(None, target=TARGET, confirm_channel_switch=True)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", ["digest", "revision", "stale"])
async def test_changed_or_stale_catalog_cannot_submit_a_previously_selected_target(api, change):
    http, _, client, registry = api
    catalog = registry.list_images.return_value
    if change == "stale":
        catalog["stale"] = True
    else:
        catalog["heads"] = {"development": {**TARGET, change: "changed"}}
    response = await http.post("/api/v1/version/update", json={"target": TARGET, "confirm_channel_switch": True}, headers={"X-CSRF-Token": generate_csrf_token()})
    assert response.status_code == 409
    client.update.assert_not_awaited()


async def test_unverified_job_outcome_is_preserved_for_browser_verification(api):
    http, _, _, _ = api
    response = await http.get("/api/v1/version/jobs/fixture-job")
    assert response.status_code == 200
    assert response.json()["data"]["deployment_verified"] is False
    assert response.headers["cache-control"] == "no-store"
