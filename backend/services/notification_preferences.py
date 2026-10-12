"""Endpoint ownership, verification and preference operations for the WebUI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import NotificationAudit
from backend.services.notification_events import NotificationMessage
from backend.services.notification_service import (
    NotificationProviderPermanentError,
    notification_service,
)
from backend.services.notification_webhook import validate_webhook_url
from backend.services.secret_crypto_service import encrypt_secret


def endpoint_view(endpoint):
    return {"id": endpoint.id, "provider": endpoint.provider, "name": endpoint.name or endpoint.provider,
            "address": urlsplit(endpoint.address).hostname if endpoint.provider == "webhook" else endpoint.address,
            "enabled": endpoint.enabled, "verified": endpoint.verified,
            "credential_configured": bool(endpoint.secret_encrypted), "config_version": endpoint.config_version}


async def owned_endpoint(db, user_id, endpoint_id):
    row = (await db.execute(select(NotificationEndpoint).where(
        NotificationEndpoint.id == endpoint_id, NotificationEndpoint.user_id == user_id
    ))).scalar_one_or_none()
    if not row:
        raise ValueError("Notification endpoint not found")
    return row


async def create_endpoint(db, user_id, *, provider, address, name, signing_secret=""):
    if provider not in {"email", "webhook"}:
        raise ValueError("Use the existing Telegram private-chat binding")
    if provider == "email":
        address = str(address).strip().lower()
        if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", address) or len(address) > 320:
            raise ValueError("Invalid email address")
    else:
        address = validate_webhook_url(address)
        if not 32 <= len(signing_secret) <= 1024:
            raise ValueError("Webhook signing secret must have 32 to 1024 characters")
    row = NotificationEndpoint(user_id=user_id, provider=provider, address=address,
        name=name[:100], verified=False, enabled=True, config_version=1,
        metadata_json=json.dumps({"source": "user"}),
        secret_encrypted=encrypt_secret(signing_secret) if provider == "webhook" else None)
    db.add(row)
    await db.flush()
    db.add(NotificationAudit(actor_id=user_id, action="endpoint.created", target_id=str(row.id)))
    await db.commit()
    return row


async def verify_endpoint(db, user_id, endpoint_id, *, code=""):
    endpoint = await owned_endpoint(db, user_id, endpoint_id)
    if not endpoint.enabled:
        raise ValueError("Enable endpoint before verification")
    if endpoint.provider == "email" and code:
        digest = endpoint.verification_token_hash
        won = await db.execute(update(NotificationEndpoint).where(
            NotificationEndpoint.id == endpoint_id, NotificationEndpoint.user_id == user_id,
            NotificationEndpoint.enabled.is_(True), NotificationEndpoint.verification_token_hash == digest,
            NotificationEndpoint.verification_expires_at >= now_utc(),
            NotificationEndpoint.verification_attempts < get_settings().notification_verification_max_attempts,
        ).values(verification_attempts=NotificationEndpoint.verification_attempts + 1))
        await db.commit()
        if won.rowcount != 1 or not digest or not hmac.compare_digest(digest, hashlib.sha256(code.encode()).hexdigest()):
            raise ValueError("Verification code invalid or expired")
        endpoint.verified = True
        endpoint.verification_token_hash = None
        endpoint.verification_expires_at = None
    elif endpoint.provider in {"email", "webhook"}:
        available = await notification_service._reserve_provider_start(db, endpoint.provider)
        if available:
            raise ValueError("Provider is rate limited; retry later")
        provider = notification_service.registry.get(endpoint.provider)
        if endpoint.provider == "webhook":
            await provider.verify_endpoint(endpoint)
            endpoint.verified = True
        else:
            token = secrets.token_urlsafe(24)
            endpoint.verification_token_hash = hashlib.sha256(token.encode()).hexdigest()
            endpoint.verification_expires_at = now_utc() + timedelta(seconds=get_settings().notification_email_verification_ttl_seconds)
            endpoint.verification_attempts = 0
            await db.commit()
            await asyncio.wait_for(provider.send_message(endpoint=endpoint, message=NotificationMessage(
                "verification:" + secrets.token_urlsafe(16), "endpoint.verify", "Verify your notification email",
                f"Your Sakura-AI verification code is: {token}\nEnter it in your notification settings.", "info"
            )), get_settings().notification_send_timeout_seconds)
    else:
        raise NotificationProviderPermanentError("Use Telegram binding handshake", "verification")
    db.add(NotificationAudit(actor_id=user_id, action="endpoint.verification", target_id=str(endpoint_id)))
    await db.commit()
    return endpoint_view(endpoint)
