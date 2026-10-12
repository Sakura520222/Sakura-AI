"""SSRF/signature regression and real local TLS transport tests (no third-party claim)."""
import asyncio
import ipaddress
import json
import socket
import ssl
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import aiohttp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.core.time_service import now_utc
from backend.models.identity_models import NotificationEndpoint
from backend.services import notification_webhook as webhook
from backend.services.notification_events import NotificationMessage
from backend.services.notification_service import (
    NotificationProviderPermanentError,
    NotificationProviderRetryAfter,
)
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def webhook_settings(monkeypatch):
    settings = SimpleNamespace(notification_webhook_enabled=True,
        notification_webhook_allowed_hosts="", notification_webhook_timeout_seconds=1,
        notification_webhook_response_max_bytes=1024)
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("address", [
    "http://example.org/hook", "https://localhost/", "https://127.0.0.1/", "https://169.254.169.254/",
    "https://[::1]/", "https://[::ffff:127.0.0.1]/", "https://10.0.0.1/",
    "https://user:secret@example.org/", "https://example.org/?token=secret", "https://example.org:8443/",
    "https://example.org/#secret", "https://example.org\\@127.0.0.1/",
])
def test_webhook_address_policy(address, webhook_settings):
    with pytest.raises(NotificationProviderPermanentError):
        webhook.validate_webhook_url(address)


@pytest.mark.asyncio
async def test_dns_mixed_public_and_private_fails_closed(webhook_settings, monkeypatch):
    async def addresses(*_, **__):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    with pytest.raises(NotificationProviderPermanentError):
        await webhook.resolve_public_addresses("example.org")
    assert not webhook._public_address(ipaddress.ip_address("2002:7f00:1::"))


def test_hmac_verification_rejects_tampering_and_expired_replays():
    body, secret = b'{"type":"review.completed"}', "a-private-signing-key"
    timestamp = int(now_utc().timestamp())
    signature = webhook.webhook_signature(secret, timestamp, body)
    assert webhook.verify_webhook_signature(secret, timestamp, body, signature)
    assert not webhook.verify_webhook_signature(secret, timestamp, body + b"x", signature)
    assert not webhook.verify_webhook_signature("wrong", timestamp, body, signature)
    assert not webhook.verify_webhook_signature(secret, timestamp - 301, body, signature)


@pytest.fixture
def tls_receiver(tmp_path, monkeypatch, webhook_settings):
    # A test-only CA and DNS/port fixture let the full aiohttp/TLS stack reach
    # localhost. Production URL/DNS policy is exercised separately above.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.org")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now_utc()-timedelta(minutes=1))
        .not_valid_after(now_utc()+timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
            x509.DNSName("example.org")]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    secret = "test-only-local-signing-secret"
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert webhook.verify_webhook_signature(secret, self.headers["X-Sakura-Timestamp"], body, self.headers["X-Sakura-Signature"])
            payload = json.loads(body)
            seen.append((self.path, payload, self.headers["X-Sakura-Event-ID"]))
            code = {"/redirect": 302, "/limited": 429, "/credential": 401}.get(self.path, 200)
            self.send_response(code)
            if self.path == "/redirect":
                self.send_header("Location", "https://127.0.0.1/internal")
            if self.path == "/limited":
                self.send_header("Retry-After", "7")
            self.end_headers()
            if self.path == "/large":
                self.wfile.write(b"x" * 2048)
            elif payload.get("type") == "endpoint.verify":
                self.wfile.write(json.dumps({"challenge": payload["challenge"]}).encode())
            else:
                self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_connector = aiohttp.TCPConnector
    client_ssl = ssl.create_default_context(cafile=str(cert_path))

    def connector(**kwargs):
        return original_connector(ssl=client_ssl, **kwargs)

    async def resolve(_host):
        return [(socket.AF_INET, "127.0.0.1")]

    async def pin(self, host, port=443, family=socket.AF_UNSPEC):
        return [{"hostname": host, "host": "127.0.0.1", "port": server.server_port,
                 "family": socket.AF_INET, "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(webhook.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(webhook, "resolve_public_addresses", resolve)
    monkeypatch.setattr(webhook._PinnedResolver, "resolve", pin)
    monkeypatch.setattr(webhook, "decrypt_secret", lambda _: secret)
    yield seen
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_tls_send_and_ownership_challenge(tls_receiver):
    provider = webhook.WebhookNotificationProvider()
    endpoint = NotificationEndpoint(address="https://example.org/receiver", secret_encrypted="fixture")
    await provider.verify_endpoint(endpoint)
    await provider.send_message(endpoint=endpoint, message=NotificationMessage("event-123", "review.completed", "Review", "Done", "info"))
    assert tls_receiver[0][1]["type"] == "endpoint.verify"
    assert tls_receiver[1][2] == "event-123"
    assert tls_receiver[1][1]["type"] == "review.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,category", [("/redirect", "security"), ("/credential", "credential"), ("/large", "security")])
async def test_real_tls_transport_rejects_redirects_credentials_and_large_response(tls_receiver, path, category):
    endpoint = NotificationEndpoint(address="https://example.org" + path, secret_encrypted="fixture")
    with pytest.raises(NotificationProviderPermanentError) as exc:
        await webhook.WebhookNotificationProvider().send_message(endpoint=endpoint,
            message=NotificationMessage("id", "review.completed", "Title", "Body", "info"))
    assert exc.value.category == category
    assert len(tls_receiver) == 1


@pytest.mark.asyncio
async def test_real_tls_rate_limit_preserves_retry_after(tls_receiver):
    endpoint = NotificationEndpoint(address="https://example.org/limited", secret_encrypted="fixture")
    with pytest.raises(NotificationProviderRetryAfter) as exc:
        await webhook.WebhookNotificationProvider().send_message(endpoint=endpoint,
            message=NotificationMessage("id", "review.completed", "Title", "Body", "info"))
    assert exc.value.retry_after == 7


@pytest.mark.asyncio
async def test_registered_webhook_flows_from_event_to_real_tls_receiver(tls_receiver, event_db):
    from sqlalchemy.orm import Session

    from backend.models.announcement_models import NotificationDelivery
    from backend.models.notification_models import NotificationSubscription
    from backend.services.notification_service import (
        NotificationProviderRegistry,
        NotificationService,
        WebUINotificationProvider,
    )
    from tests.test_notification_events_646 import AsyncDB, make_event

    engine, _ = event_db
    with Session(engine) as db:
        db.add(NotificationEndpoint(id=3, user_id=1, provider="webhook", address="https://example.org/receiver",
                                    secret_encrypted="fixture", verified=True, enabled=True))
        db.add(NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=3))
        db.commit()
    dispatcher = NotificationService(NotificationProviderRegistry({
        "web": WebUINotificationProvider(), "webhook": webhook.WebhookNotificationProvider(),
    }))
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        event_id = item.id
        await db.commit()
    await dispatcher.recover_events_once()
    assert len(tls_receiver) == 1
    assert tls_receiver[0][2] == event_id
    with Session(engine) as db:
        rows = db.query(NotificationDelivery).all()
        assert {row.channel for row in rows} == {"web", "webhook"}
        assert all(row.status == "sent" for row in rows)
