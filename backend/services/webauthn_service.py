"""WebAuthn / Passkey service helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential

_WEBAUTHN_CHALLENGE_PREFIX = "webauthn:challenge:"
_webauthn_challenge_fallback: dict[str, dict] = {}
_MAX_FALLBACK_CHALLENGES = 1000


@dataclass(frozen=True)
class WebAuthnRpConfig:
    """Resolved WebAuthn relying-party configuration."""

    rp_id: str
    rp_name: str
    origin: str


class WebAuthnError(Exception):
    """Base WebAuthn service error."""


def b64url_encode(value: bytes) -> str:
    """Encode bytes as unpadded base64url text."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url text."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def credential_id_hash(credential_id: str) -> str:
    """Return a fixed-length hash for indexing WebAuthn credential IDs."""
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()


def _cleanup_fallback_challenges() -> None:
    now = time.time()
    expired = [
        key
        for key, value in _webauthn_challenge_fallback.items()
        if value.get("expires", 0) <= now
    ]
    for key in expired:
        _webauthn_challenge_fallback.pop(key, None)


def get_rp_config(request_origin: str | None = None) -> WebAuthnRpConfig:
    """Resolve RP ID and Origin from settings."""
    settings = get_settings()
    if settings.passkeys_origin:
        origin = settings.passkeys_origin.rstrip("/")
    elif request_origin:
        origin = request_origin.rstrip("/")
    else:
        origin = ""
    if not origin:
        app_domain = settings.app_domain or "localhost"
        scheme = (
            "http"
            if app_domain.split(":", 1)[0] in ("localhost", "127.0.0.1")
            else "https"
        )
        port = f":{settings.app_port}" if settings.app_port else ""
        origin = f"{scheme}://{app_domain}{port}".rstrip("/")

    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        raise WebAuthnError("Invalid passkeys_origin configuration")
    rp_id = settings.passkeys_rp_id or parsed.hostname or "localhost"
    return WebAuthnRpConfig(
        rp_id=rp_id, rp_name=settings.passkeys_rp_name, origin=origin
    )


async def save_challenge(challenge_id: str, challenge: bytes, context: dict) -> None:
    """Persist a WebAuthn challenge with Redis and memory fallback."""
    settings = get_settings()
    payload = {
        "challenge": b64url_encode(challenge),
        "context": context,
    }
    key = f"{_WEBAUTHN_CHALLENGE_PREFIX}{challenge_id}"
    try:
        redis = await get_async_redis()
        await redis.setex(
            key,
            settings.passkeys_challenge_ttl_seconds,
            json.dumps(payload),
        )
        return
    except Exception as exc:
        logger.warning("Redis 存储 WebAuthn challenge 失败，使用内存回退: {}", exc)

    if len(_webauthn_challenge_fallback) > _MAX_FALLBACK_CHALLENGES:
        _cleanup_fallback_challenges()
    if len(_webauthn_challenge_fallback) >= _MAX_FALLBACK_CHALLENGES:
        raise WebAuthnError("WebAuthn challenge fallback cache is full")
    _webauthn_challenge_fallback[key] = {
        **payload,
        "expires": time.time() + settings.passkeys_challenge_ttl_seconds,
    }


async def pop_challenge(challenge_id: str) -> tuple[bytes, dict] | None:
    """Read and remove a WebAuthn challenge."""
    key = f"{_WEBAUTHN_CHALLENGE_PREFIX}{challenge_id}"
    value = None
    try:
        redis = await get_async_redis()
        value = await redis.execute_command("GETDEL", key)
        if value:
            payload = json.loads(value)
            return b64url_decode(payload["challenge"]), payload.get("context", {})
    except Exception as exc:
        logger.warning("Redis 读取 WebAuthn challenge 失败，尝试内存回退: {}", exc)

    fallback = _webauthn_challenge_fallback.pop(key, None)
    if fallback and fallback.get("expires", 0) > time.time():
        return b64url_decode(fallback["challenge"]), fallback.get("context", {})
    return None


def _credential_descriptor(
    credential: UserWebAuthnCredential,
) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(id=b64url_decode(credential.credential_id))


def _user_handle(user_id: int) -> bytes:
    return str(user_id).encode("utf-8")


def _display_name(user: TelegramUser) -> str:
    return user.github_username or f"telegram-{user.telegram_id}"


async def begin_registration(
    session: AsyncSession,
    user: TelegramUser,
    request_origin: str | None = None,
) -> dict:
    """Create WebAuthn registration options."""
    rp = get_rp_config(request_origin)
    result = await session.execute(
        select(UserWebAuthnCredential).where(UserWebAuthnCredential.user_id == user.id)
    )
    existing_credentials = result.scalars().all()
    exclude_credentials = [_credential_descriptor(c) for c in existing_credentials]
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=rp.rp_name,
        user_name=_display_name(user),
        user_display_name=_display_name(user),
        user_id=_user_handle(user.id),
        timeout=get_settings().passkeys_challenge_ttl_seconds * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude_credentials,
    )
    challenge_id = b64url_encode(options.challenge)
    await save_challenge(
        challenge_id,
        options.challenge,
        {
            "type": "registration",
            "user_id": user.id,
            "rp_id": rp.rp_id,
            "origin": rp.origin,
        },
    )
    return {
        "challenge_id": challenge_id,
        "public_key": json.loads(options_to_json(options)),
        "rp_id": rp.rp_id,
        "origin": rp.origin,
    }


async def finish_registration(
    session: AsyncSession,
    user: TelegramUser,
    challenge_id: str,
    credential: dict,
    device_name: str | None = None,
) -> UserWebAuthnCredential:
    """Verify and store a WebAuthn registration response."""
    challenge_data = await pop_challenge(challenge_id)
    if not challenge_data:
        raise WebAuthnError("Registration challenge expired")
    challenge, context = challenge_data
    if context.get("type") != "registration" or int(context.get("user_id")) != user.id:
        raise WebAuthnError("Registration challenge context mismatch")

    rp = WebAuthnRpConfig(
        rp_id=context.get("rp_id") or get_rp_config().rp_id,
        rp_name=get_settings().passkeys_rp_name,
        origin=context.get("origin") or get_rp_config().origin,
    )
    verification = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp.rp_id,
        expected_origin=rp.origin,
        require_user_verification=False,
    )
    db_credential = UserWebAuthnCredential(
        user_id=user.id,
        credential_id=b64url_encode(verification.credential_id),
        credential_id_hash=credential_id_hash(
            b64url_encode(verification.credential_id)
        ),
        public_key=b64url_encode(verification.credential_public_key),
        sign_count=verification.sign_count,
        transports=",".join(credential.get("response", {}).get("transports", []) or []),
        device_name=(device_name or "Passkey")[:100],
        backed_up=bool(getattr(verification, "credential_backed_up", False)),
    )
    session.add(db_credential)
    await session.flush()
    logger.info(
        "Passkey registered: user_id={}, credential_id={}", user.id, db_credential.id
    )
    return db_credential


async def begin_authentication(
    session: AsyncSession,
    user_id: int | None = None,
    request_origin: str | None = None,
) -> dict:
    """Create WebAuthn authentication options.

    When *user_id* is ``None`` the options are generated WITHOUT
    ``allow_credentials``, enabling a **discoverable-credential**
    (passkey-only) login flow where the browser picks the credential.
    """
    rp = get_rp_config(request_origin)
    allow_credentials = None
    if user_id is not None:
        query = select(UserWebAuthnCredential).where(
            UserWebAuthnCredential.user_id == user_id
        )
        result = await session.execute(query)
        credentials = result.scalars().all()
        allow_credentials = [_credential_descriptor(c) for c in credentials]
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        timeout=get_settings().passkeys_challenge_ttl_seconds * 1000,
        allow_credentials=allow_credentials or None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = b64url_encode(options.challenge)
    await save_challenge(
        challenge_id,
        options.challenge,
        {
            "type": "authentication",
            "user_id": user_id,
            "rp_id": rp.rp_id,
            "origin": rp.origin,
        },
    )
    return {
        "challenge_id": challenge_id,
        "public_key": json.loads(options_to_json(options)),
        "rp_id": rp.rp_id,
        "origin": rp.origin,
    }


async def finish_authentication(
    session: AsyncSession,
    challenge_id: str,
    credential: dict,
    expected_user_id: int | None = None,
) -> UserWebAuthnCredential:
    """Verify an authentication response and update credential counter."""
    challenge_data = await pop_challenge(challenge_id)
    if not challenge_data:
        raise WebAuthnError("Authentication challenge expired")
    challenge, context = challenge_data
    if context.get("type") != "authentication":
        raise WebAuthnError("Authentication challenge context mismatch")
    context_user_id = context.get("user_id")
    if expected_user_id is not None and context_user_id is not None:
        if int(context_user_id) != expected_user_id:
            raise WebAuthnError("Authentication user mismatch")

    credential_id = credential.get("id") or credential.get("rawId")
    if not credential_id:
        raise WebAuthnError("Missing credential id")
    result = await session.execute(
        select(UserWebAuthnCredential).where(
            UserWebAuthnCredential.credential_id_hash
            == credential_id_hash(credential_id)
        )
    )
    db_credential = result.scalar_one_or_none()
    if not db_credential:
        raise WebAuthnError("Unknown passkey credential")
    if expected_user_id is not None and db_credential.user_id != expected_user_id:
        raise WebAuthnError("Passkey does not belong to this user")

    rp = WebAuthnRpConfig(
        rp_id=context.get("rp_id") or get_rp_config().rp_id,
        rp_name=get_settings().passkeys_rp_name,
        origin=context.get("origin") or get_rp_config().origin,
    )
    verification = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp.rp_id,
        expected_origin=rp.origin,
        credential_public_key=b64url_decode(db_credential.public_key),
        credential_current_sign_count=int(db_credential.sign_count or 0),
        require_user_verification=False,
    )
    db_credential.sign_count = verification.new_sign_count
    db_credential.last_used_at = datetime.now(timezone.utc)
    await session.flush()
    return db_credential
