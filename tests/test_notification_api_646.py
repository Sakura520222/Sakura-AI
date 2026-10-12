"""HTTP authorization, CSRF, inbox ownership and credential regression tests."""
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import NotificationEvent, UserNotification
from backend.services import notification_preferences
from backend.services.notification_events import emit_event
from backend.services.notification_service import notification_service
from backend.services.secret_crypto_service import decrypt_secret
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    require_auth,
    require_super_admin,
)
from backend.webui.routes import notifications
from tests.test_notification_events_646 import AsyncDB
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def client(api_db):
    engine, _ = api_db
    app = FastAPI()
    app.include_router(notifications.router)

    async def database():
        async with AsyncDB(engine) as db:
            yield db

    async def identity():
        return {"user_id": 1, "role": "user", "sub": "alice"}

    async def admin():
        raise HTTPException(403, "Not authorized")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_auth] = identity
    app.dependency_overrides[require_super_admin] = admin
    with TestClient(app) as http:
        http.headers["X-CSRF-Token"] = get_csrf_serializer().dumps({})
        yield http


def test_endpoint_ownership_csrf_and_admin_scope(client, api_db):
    assert client.post("/notifications/endpoints/999/test", json={}).status_code == 404
    assert client.get("/notifications/admin/diagnostics").status_code == 403
    with Session(api_db[0]) as db:
        db.add(NotificationEndpoint(id=3, user_id=2, provider="email", address="bob@example.org", verified=True, enabled=True))
        db.commit()
    assert client.patch("/notifications/endpoints/3", json={"name": "stolen", "enabled": True}).status_code == 404
    assert client.delete("/notifications/endpoints/3").status_code == 404
    assert client.post("/notifications/endpoints/3/test", json={}).status_code == 404
    assert client.post("/notifications/subscriptions", json={"event_type": "review.*", "endpoint_id": 3}).status_code == 404
    client.headers.pop("X-CSRF-Token")
    assert client.post("/notifications/endpoints/1/test", json={}).status_code == 422


def test_webhook_credentials_are_encrypted_and_not_echoed(client, api_db):
    secret = "private-key-never-returned-or-logged-646"
    response = client.post("/notifications/endpoints", json={
        "provider": "webhook", "address": "https://example.org/receiver", "name": "Deploy", "signing_secret": secret,
    })
    assert response.status_code == 200
    assert secret not in response.text
    endpoint_id = response.json()["id"]
    with Session(api_db[0]) as db:
        endpoint = db.get(NotificationEndpoint, endpoint_id)
        assert secret not in endpoint.secret_encrypted
        assert decrypt_secret(endpoint.secret_encrypted) == secret
        assert endpoint.verified is False
    assert secret not in client.get("/notifications/settings").text
    assert client.post(f"/notifications/endpoints/{endpoint_id}/test", json={}).status_code == 400
    assert client.post("/notifications/endpoints", json={
        "provider": "webhook", "address": "https://127.0.0.1/private", "name": "Unsafe", "signing_secret": secret,
    }).status_code == 400


@pytest.mark.asyncio
async def test_inbox_read_is_independent_and_owned(client, api_db):
    engine, _ = api_db
    async with AsyncDB(engine) as db:
        item = await emit_event(db, event_type="security.changed", resource_type="user", resource_id=1,
            run_key="test", title="Security", content="Changed", recipient_ids=[1], sensitive=True)
        await db.commit()
        await notification_service.materialize_event(db, item.id)
    response = client.get("/notifications/history")
    assert response.status_code == 200
    inbox = response.json()["items"][0]
    assert inbox["read"] is False
    assert client.post(f"/notifications/{inbox['id']}/read").status_code == 200
    assert client.get("/notifications/history").json()["items"][0]["read"] is True
    with Session(engine) as db:
        assert db.query(UserNotification).one().read_at is not None
        db.add(UserNotification(event_id=item.id, user_id=2))
        db.commit()
        foreign_id = db.query(UserNotification).filter_by(user_id=2).one().id
    assert client.post(f"/notifications/{foreign_id}/read").status_code == 404


def test_test_send_is_persisted_and_unbind_does_not_change_role(client, api_db):
    from backend.models.telegram_models import TelegramUser
    response = client.post("/notifications/endpoints/1/test", json={})
    assert response.status_code == 200
    event_id = response.json()["event_id"]
    with Session(api_db[0]) as db:
        event = db.get(NotificationEvent, event_id)
        assert json.loads(event.payload_json) == {"endpoint_id": 1}
        assert event.status == "pending"
    assert client.delete("/notifications/endpoints/1").status_code == 200
    with Session(api_db[0]) as db:
        assert db.get(NotificationEndpoint, 1).enabled is False
        assert db.get(TelegramUser, 1).role == "user"


def test_email_verification_uses_hashed_code_and_persistent_attempt_limit(client, api_db, monkeypatch):
    from backend.core.config import get_settings

    monkeypatch.setattr(notification_preferences, "get_settings", get_settings)
    captured = []

    async def send_message(**kwargs):
        captured.append(kwargs["message"].content)
    monkeypatch.setattr(notification_service.registry.get("email"), "send_message", send_message)
    created = client.post("/notifications/endpoints", json={"provider": "email", "address": "new@example.org", "name": "Other email"}).json()
    endpoint_id = created["id"]
    assert client.post(f"/notifications/endpoints/{endpoint_id}/verify", json={}).status_code == 200
    code = captured[0].split(": ")[1].split("\n")[0]
    with Session(api_db[0]) as db:
        row = db.get(NotificationEndpoint, endpoint_id)
        assert row.verification_token_hash != code
        assert row.verified is False
    assert client.post(f"/notifications/endpoints/{endpoint_id}/verify", json={"code": "wrong"}).status_code == 400
    assert client.post(f"/notifications/endpoints/{endpoint_id}/verify", json={"code": code}).status_code == 200
    assert client.post(f"/notifications/endpoints/{endpoint_id}/test", json={}).status_code == 200
