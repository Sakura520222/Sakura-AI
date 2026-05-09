"""WebAuthn service helper tests."""

import json

import pytest

from backend.services import webauthn_service
from backend.services.webauthn_service import b64url_decode, b64url_encode


def test_base64url_round_trip_without_padding():
    raw = b"hello-passkey-challenge"
    encoded = b64url_encode(raw)

    assert "=" not in encoded
    assert b64url_decode(encoded) == raw


@pytest.mark.asyncio
async def test_pop_challenge_uses_atomic_redis_getdel(monkeypatch):
    raw = b"single-use-challenge"
    payload = json.dumps(
        {
            "challenge": b64url_encode(raw),
            "context": {"type": "authentication"},
        }
    )

    class FakeRedis:
        def __init__(self, value):
            self.value = value
            self.commands: list[tuple[str, str]] = []

        async def execute_command(self, command: str, key: str):
            self.commands.append((command, key))
            assert command == "GETDEL"
            value = self.value
            self.value = None
            return value

    fake_redis = FakeRedis(payload)

    async def fake_get_async_redis():
        return fake_redis

    monkeypatch.setattr(webauthn_service, "get_async_redis", fake_get_async_redis)

    first = await webauthn_service.pop_challenge("challenge-id")
    second = await webauthn_service.pop_challenge("challenge-id")

    assert first == (raw, {"type": "authentication"})
    assert second is None
    assert fake_redis.commands == [
        ("GETDEL", "webauthn:challenge:challenge-id"),
        ("GETDEL", "webauthn:challenge:challenge-id"),
    ]
