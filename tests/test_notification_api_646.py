"""HTTP authorization, CSRF, inbox ownership and credential regression tests."""
import asyncio
import json
import socket
from unittest.mock import AsyncMock

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
    get_user_preferences,
    require_auth,
    require_super_admin,
)
from backend.webui.routes import agent_team, notifications
from tests.test_notification_events_646 import AsyncDB
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def client(api_db, monkeypatch):
    async def public_dns(_loop, _host, _port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", public_dns)
    engine, _ = api_db
    app = FastAPI()
    app.include_router(notifications.router)
    app.include_router(agent_team.router)

    async def database():
        async with AsyncDB(engine, outbox=True) as db:
            yield db

    async def identity():
        return {"user_id": 1, "role": "user", "sub": "alice"}

    async def admin():
        raise HTTPException(403, "Not authorized")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_auth] = identity
    app.dependency_overrides[require_super_admin] = admin
    app.dependency_overrides[get_user_preferences] = lambda: {"language": "en", "theme": "light", "items_per_page": 20}
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


def test_webhook_credentials_are_encrypted_and_not_echoed(client, api_db, monkeypatch):
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
    replacement = "replacement-signing-secret-never-echoed-646"

    async def rotate_during_challenge(endpoint):
        assert decrypt_secret(endpoint.secret_encrypted) == secret
        response = await asyncio.to_thread(client.patch, f"/notifications/endpoints/{endpoint_id}", json={
            "name": "Deploy", "enabled": True, "signing_secret": replacement,
        })
        assert response.status_code == 200

    provider = notification_service.registry.get("webhook")
    monkeypatch.setattr(provider, "verify_endpoint", rotate_during_challenge)
    assert client.post(f"/notifications/endpoints/{endpoint_id}/verify", json={}).status_code == 400
    with Session(api_db[0]) as db:
        endpoint = db.get(NotificationEndpoint, endpoint_id)
        assert endpoint.verified is False and endpoint.config_version == 2
        assert decrypt_secret(endpoint.secret_encrypted) == replacement
    monkeypatch.setattr(provider, "verify_endpoint", AsyncMock())
    assert client.post(f"/notifications/endpoints/{endpoint_id}/verify", json={}).json()["verified"] is True
    assert client.post("/notifications/endpoints", json={
        "provider": "webhook", "address": "https://127.0.0.1/private", "name": "Unsafe", "signing_secret": secret,
    }).status_code == 400


@pytest.mark.parametrize("addresses", [
    ["10.0.0.1"], ["127.0.0.1"], ["192.0.2.1"], ["169.254.169.254"],
    ["93.184.216.34", "10.0.0.1"], ["::1"], ["::ffff:93.184.216.34"],
])
def test_webhook_creation_rejects_all_unsafe_dns_without_persisting(client, api_db, monkeypatch, addresses):
    async def dns(_loop, _host, _port, **_kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
            socket.IPPROTO_TCP, "", (ip, 443)) for ip in addresses]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", dns)
    with Session(api_db[0]) as db:
        before = db.query(NotificationEndpoint).count()
    response = client.post("/notifications/endpoints", json={"provider": "webhook",
        "address": "https://example.org/hook", "name": "Unsafe", "signing_secret": "s" * 32})
    assert response.status_code == 400
    with Session(api_db[0]) as db:
        assert db.query(NotificationEndpoint).count() == before


def test_webhook_creation_dns_failure_is_retryable_and_bounded(client, api_db, monkeypatch):
    from backend.services import notification_webhook

    async def blocked(_host):
        await asyncio.sleep(10)

    monkeypatch.setattr(notification_webhook, "resolve_public_addresses", blocked)
    monkeypatch.setattr(notification_webhook.get_settings(), "notification_webhook_timeout_seconds", 0.01)
    response = client.post("/notifications/endpoints", json={"provider": "webhook",
        "address": "https://example.org/hook", "name": "DNS unavailable", "signing_secret": "s" * 32})
    assert response.status_code == 503
    with Session(api_db[0]) as db:
        assert db.query(NotificationEndpoint).count() == 2


@pytest.mark.asyncio
async def test_disable_then_enable_does_not_reactivate_pending_or_claimed_deliveries(client, api_db):
    from backend.core.time_service import now_utc
    from backend.models.announcement_models import NotificationDelivery
    from tests.test_notification_events_646 import Provider, make_event, service

    engine, _ = api_db
    provider = Provider()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        event = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, event.id)
    with Session(engine) as db:
        pending = db.query(NotificationDelivery).filter_by(endpoint_id=1).one()
        pending.claim_token, pending.claim_until = "old-lease", now_utc()
        pending.next_retry_at = now_utc()
        pending_id = pending.id
        sent_event = NotificationEvent(id="already-sent", dedup_key="f" * 64,
            event_type="review.completed", resource_type="review", resource_id="100",
            run_key="2", title="Sent", content="Done")
        db.add(sent_event)
        db.flush()
        db.add(NotificationDelivery(event_id=sent_event.id, user_id=1, endpoint_id=1,
            endpoint_key="endpoint:1", channel="email", status="sent", attempts=1))
        db.commit()
    assert client.patch("/notifications/endpoints/1", json={"name": "Off", "enabled": False}).status_code == 200
    assert client.patch("/notifications/endpoints/1", json={"name": "On", "enabled": True}).status_code == 200
    with Session(engine) as db:
        row = db.get(NotificationDelivery, pending_id)
        assert row.status == "cancelled" and row.error_category == "ineligible"
        assert row.claim_token is row.claim_until is row.next_retry_at is None
        assert db.query(NotificationDelivery).filter_by(event_id="already-sent").one().status == "sent"
        other_id = db.query(NotificationDelivery).filter_by(endpoint_id=2).one().id
    async with AsyncDB(engine) as db:
        assert await dispatcher.deliver_event_row(db, pending_id) is False
        assert await dispatcher.deliver_event_row(db, other_id) is True
    assert provider.sent == [2]


@pytest.mark.asyncio
async def test_retired_telegram_requires_binding_handshake_not_generic_toggle(client, api_db):
    from backend.services.identity_service import stage_notification_endpoint

    engine, _ = api_db
    with Session(engine) as db:
        db.add_all([NotificationEndpoint(id=3, user_id=1, provider="telegram", address="10001", verified=True, enabled=True),
            NotificationEndpoint(id=4, user_id=1, provider="telegram", address="10002", verified=True, enabled=False)])
        db.commit()
    assert client.patch("/notifications/endpoints/4", json={"name": "Old", "enabled": True}).status_code == 409
    assert client.patch("/notifications/endpoints/3", json={"name": "Current", "enabled": True}).status_code == 200
    async with AsyncDB(engine) as db:
        await stage_notification_endpoint(db, 1, "telegram", "10002", verified=True)
        await db.commit()
    with Session(engine) as db:
        assert [row.id for row in db.query(NotificationEndpoint).filter_by(provider="telegram", enabled=True)] == [4]


def test_subscription_repeated_posts_upsert_and_repair_existing_duplicates(client, api_db):
    from backend.models.notification_models import NotificationSubscription

    body = {"event_type": "announcement.*", "repo_name": "ALICE/Repo", "endpoint_id": 1}
    first = client.post("/notifications/subscriptions", json=body)
    assert first.status_code == 200
    first_id = first.json()["id"]
    assert {client.post("/notifications/subscriptions", json=body).json()["id"] for _ in range(5)} == {first_id}
    with Session(api_db[0]) as db:
        db.add(NotificationSubscription(user_id=1, event_type="announcement.*", repo_name="alice/repo",
            resource_type="", resource_id="", endpoint_id=1))
        db.commit()
    changed = client.post("/notifications/subscriptions", json={**body, "enabled": False, "min_severity": "error"})
    assert changed.status_code == 200 and changed.json()["id"] == first_id
    with Session(api_db[0]) as db:
        row = db.query(NotificationSubscription).filter_by(event_type="announcement.*").one()
        assert row.enabled is False and row.min_severity == "error" and row.repo_name == "ALICE/Repo"


@pytest.mark.asyncio
async def test_user_backup_upload_uses_import_scope_in_an_enabled_application_session(client, api_db):
    from backend.services.user_backup_service import serialize_user_backup
    from backend.webui.routes.config import router as config_router
    from tests.test_notification_import_routing_646 import changed_backup

    client.app.include_router(config_router)
    client.app.dependency_overrides[require_super_admin] = lambda: {"user_id": 1, "role": "super_admin"}
    async with AsyncDB(api_db[0]) as db:
        document = await changed_backup(db)
    response = client.post("/config/backup/users/import", follow_redirects=False,
        data={"csrf_token": get_csrf_serializer().dumps({})},
        files={"backup_file": ("users.json", serialize_user_backup(document), "application/json")})
    assert response.status_code == 302
    with Session(api_db[0]) as db:
        from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential

        assert db.get(TelegramUser, 1).role == "admin"
        assert db.query(UserWebAuthnCredential).count() == 1
        assert db.query(NotificationEvent).count() == 0


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


def test_business_history_excludes_announcements_before_pagination(client, api_db):
    with Session(api_db[0]) as db:
        # Older business notifications must fill both pages even when recent
        # announcement fanout has inserted more than a whole page of inbox rows.
        for number in range(1, 166):
            announcement = number > 55
            event = NotificationEvent(id=f"event-{number}", dedup_key=f"{number:064x}",
                event_type="announcement.published" if announcement else "review.completed",
                resource_type="announcement" if announcement else "review", resource_id=str(number),
                repo_name=None if announcement else "alice/repo", run_key="1", title=f"Item {number}", content="Done")
            db.add(event)
            db.flush()
            db.add(UserNotification(id=number, user_id=1, event_id=event.id))
        db.commit()
    first = client.get("/notifications/history").json()
    second = client.get("/notifications/history?page=2").json()
    third = client.get("/notifications/history?page=3").json()
    assert [item["id"] for item in first["items"]] == list(range(55, 5, -1))
    assert first["has_next"] is True
    assert [item["id"] for item in second["items"]] == list(range(5, 0, -1))
    assert second["has_next"] is False
    assert third["items"] == [] and third["has_next"] is False
    assert first["unread"] == second["unread"] == 55


def test_scan_history_and_unread_use_current_admin_scope(client, api_db):
    from backend.models.telegram_models import TelegramUser

    with Session(api_db[0]) as db:
        event = NotificationEvent(id="scan-event", dedup_key="f" * 64,
            event_type="scan.completed", resource_type="scan", resource_id="10", repo_name="alice/repo",
            run_key="1", title="Private scan", content="Scan summary")
        db.add(event)
        db.flush()
        db.add(UserNotification(user_id=1, event_id=event.id))  # historical delivery before the ACL fix
        db.commit()
    assert client.get("/notifications/history").json() == {"items": [], "has_next": False, "page": 1, "unread": 0}
    with Session(api_db[0]) as db:
        db.get(TelegramUser, 1).role = "admin"
        db.commit()
    response = client.get("/notifications/history").json()
    assert [item["title"] for item in response["items"]] == ["Private scan"]
    assert response["unread"] == 1


def test_agent_notification_page_selects_only_an_authorized_task(client, api_db, monkeypatch):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.services.telegram_service import TelegramService

    monkeypatch.setattr(TelegramService, "get_user_quota_info", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_team, "_load_workspace_summary", lambda: {
        "root": "test-workspace", "count": 0, "total_size_bytes": 0, "total_size_label": "0 B",
    })
    with Session(api_db[0]) as db:
        db.add_all([AgentTeamTask(id=number, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title=f"Agent task {number}", status="completed", started_by=initiator)
            for number, initiator in [(10, "ALIce"), (11, "bob")]])
        db.commit()
    response = client.get("/agent-team/?task_id=10")
    assert response.status_code == 200
    assert "this.selectTask(10)" in response.text
    assert "Agent task 10" in client.get("/agent-team/tasks/10/detail-fragment").text
    listing = client.get("/agent-team/list-fragment").text
    assert "Agent task 10" in listing and "Agent task 11" not in listing
    assert client.get("/agent-team/tasks/11/detail-fragment").status_code == 403
    assert client.get("/agent-team/api/tasks/10/prompts").status_code == 200
    assert client.get("/agent-team/api/tasks/11/prompts").status_code == 403
    assert client.get("/agent-team/?task_id=11").status_code == 403
    assert client.get("/agent-team/?task_id=999").status_code == 404
    assert client.get("/agent-team/?task_id=0").status_code == 422
    assert client.get("/agent-team/").status_code == 200


@pytest.mark.asyncio
async def test_agent_retry_creates_a_new_persisted_round_before_cloning(client, api_db, monkeypatch):
    from backend.models import database as db_module
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.telegram_models import TelegramUser
    from backend.services import telegram_service
    from backend.workers.agent_team_worker import AgentTeamWorker

    engine, _ = api_db
    monkeypatch.setattr(telegram_service, "is_payment_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_team, "_run_agent_task_background", AsyncMock())
    monkeypatch.setattr(db_module, "async_session", lambda: AsyncDB(engine, outbox=True))
    monkeypatch.setattr("backend.webui.sse.publish_event", AsyncMock())
    with Session(engine) as db:
        user = db.get(TelegramUser, 1)
        user.agent_daily_quota = user.agent_weekly_quota = user.agent_monthly_quota = 10
        db.add(AgentTeamTask(id=10, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title="Retry task", status="failed", started_by="ALIce"))
        db.commit()
    token = get_csrf_serializer().dumps({})
    worker = AgentTeamWorker.__new__(AgentTeamWorker)
    for number in (1, 2):
        assert client.post("/agent-team/tasks/10/retry", data={"csrf_token": token}).json()["success"] is True
        # Repeated queued admission is the same pending execution, not a new
        # run. The normal retry route and worker status writer use real SQL.
        assert client.post("/agent-team/tasks/10/retry", data={"csrf_token": token}).json()["success"] is True
        with Session(engine) as db:
            assert db.get(AgentTeamTask, 10).execution_round == number
        if number == 1:
            await worker._update_task(10, status="cloning")
            await worker._update_task(10, status="cloning")  # duplicate start callback
        await worker._update_task(10, status="failed", error_message="Failure before iterations")
        await worker._update_task(10, status="failed")  # duplicate terminal callback
    with Session(engine) as db:
        events = db.query(NotificationEvent).filter_by(resource_type="agent").all()
        assert {(item.event_type, item.run_key) for item in events} == {
            ("agent.started", "1:0:0"), ("agent.failed", "1:0:0"), ("agent.failed", "2:0:0"),
        }
        assert len(events) == 3
        task = db.get(AgentTeamTask, 10)
        assert (task.execution_round, task.resume_count, task.iteration_count) == (2, 0, 0)


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
