"""WebAuthn service helper tests."""

from backend.services.webauthn_service import b64url_decode, b64url_encode


def test_base64url_round_trip_without_padding():
    raw = b"hello-passkey-challenge"
    encoded = b64url_encode(raw)

    assert "=" not in encoded
    assert b64url_decode(encoded) == raw
