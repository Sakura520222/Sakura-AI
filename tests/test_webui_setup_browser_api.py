"""Setup's React API keeps the deployment credential at the HTTP boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api.v1 import setup
from backend.core import bootstrap
from backend.webui.routes import setup as setup_actions

TOKEN = "fixture-setup-credential"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(setup.limiter, "enabled", False)
    monkeypatch.setattr(setup, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(bootstrap, "validate_setup_token", lambda value: value == TOKEN)
    app = FastAPI()
    app.include_router(setup.router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.parametrize("credential", [None, "wrong-credential"])
@pytest.mark.parametrize("operation", ["verify", "backup", "complete"])
def test_browser_mutations_require_setup_credential_before_delegating(
    client, monkeypatch, credential, operation
):
    inspect = AsyncMock()
    complete = AsyncMock()
    monkeypatch.setattr(setup_actions, "inspect_config_backup", inspect)
    monkeypatch.setattr(setup_actions, "complete_setup", complete)
    response = client.post(
        f"/api/v1/setup/browser/{operation}",
        headers={"X-Setup-Token": credential} if credential else {},
        json={"content": "fixture-backup"},
    )
    assert response.status_code == 403
    assert response.json()["success"] is False
    inspect.assert_not_awaited()
    complete.assert_not_awaited()


def test_browser_verification_accepts_header_without_echoing_or_persisting_token(
    client,
):
    response = client.post(
        "/api/v1/setup/browser/verify", headers={"X-Setup-Token": TOKEN}
    )
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    assert "set-cookie" not in response.headers


def test_browser_state_exposes_presence_not_connection_credentials(client, monkeypatch):
    monkeypatch.setattr(
        setup, "get_missing_fields", AsyncMock(return_value=["redis_url"])
    )
    monkeypatch.setattr(
        "backend.core.config.get_settings",
        lambda: SimpleNamespace(
            database_url="mysql+asyncmy://user:private-password@db/sakura",
            redis_url="",
            github_private_key="private-key-bytes",
        ),
    )
    assert client.get("/api/v1/setup/browser").status_code == 403
    response = client.get("/api/v1/setup/browser", headers={"X-Setup-Token": TOKEN})
    assert response.status_code == 200
    assert response.json()["state"] == "in_progress"
    assert response.json()["configured"]["DATABASE_URL"] is True
    assert response.json()["configured"]["REDIS_URL"] is False
    assert response.json()["configured"]["GITHUB_PRIVATE_KEY"] is True
    assert response.json()["missing_fields"] == ["redis_url"]
    assert "private-password" not in response.text
    assert "private-key-bytes" not in response.text
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    ("operation", "target", "body"),
    [
        ("backup", "inspect_config_backup", {"content": "fixture-backup"}),
        ("complete", "complete_setup", {"ADMIN_GITHUB_USERNAME": "alice"}),
    ],
)
def test_authorized_browser_operations_reuse_existing_setup_authority(
    client, monkeypatch, operation, target, body
):
    captured = []

    async def authority(request):
        captured.append(await request.json())
        return JSONResponse({"success": True, "fixture": operation})

    action = AsyncMock(side_effect=authority)
    monkeypatch.setattr(setup_actions, target, action)
    response = client.post(
        f"/api/v1/setup/browser/{operation}",
        headers={"X-Setup-Token": TOKEN},
        json=body,
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "fixture": operation}
    assert captured == [body]
    action.assert_awaited_once()


def test_completed_setup_cannot_be_reopened_with_a_previously_valid_credential(
    client, monkeypatch
):
    monkeypatch.setattr(setup, "is_bootstrap_mode", lambda: False)
    assert client.get("/api/v1/setup/browser").json() == {"state": "completed"}
    for operation in ("verify", "backup", "complete"):
        response = client.post(
            f"/api/v1/setup/browser/{operation}",
            headers={"X-Setup-Token": TOKEN},
            json={},
        )
        assert response.status_code == 403
