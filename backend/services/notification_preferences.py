"""Endpoint ownership, verification and preference operations for the WebUI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select, update

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models.announcement_models import NotificationDelivery
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationAudit,
    NotificationSubscription,
)
from backend.models.telegram_models import TelegramUser
from backend.services.notification_events import NotificationMessage
from backend.services.notification_service import (
    NotificationProviderPermanentError,
    notification_service,
)
from backend.services.notification_webhook import validate_webhook_destination
from backend.services.secret_crypto_service import encrypt_secret


def endpoint_view(endpoint):
    return {"id": endpoint.id, "provider": str(endpoint.provider).lower(), "name": endpoint.name or endpoint.provider,
            "address": urlsplit(endpoint.address).hostname if endpoint.provider == "webhook" else endpoint.address,
            "enabled": endpoint.enabled, "verified": endpoint.verified,
            "credential_configured": bool(endpoint.secret_encrypted), "config_version": endpoint.config_version}


async def owned_endpoint(db, user_id, endpoint_id, *, for_update=False):
    query = select(NotificationEndpoint).where(
        NotificationEndpoint.id == endpoint_id, NotificationEndpoint.user_id == user_id
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await db.execute(query)).scalar_one_or_none()
    if not row:
        raise ValueError("Notification endpoint not found")
    return row


async def cancel_endpoint_deliveries(db, endpoint_id):
    """Fence queued/claimed rows in the same transaction as endpoint disable."""
    await db.execute(update(NotificationDelivery).where(
        NotificationDelivery.endpoint_id == endpoint_id, NotificationDelivery.status == "pending"
    ).values(status="cancelled", error_category="ineligible", next_retry_at=None,
             claim_token=None, claim_until=None, updated_at=now_utc()))


async def save_subscription(db, user_id, values):
    """Serialize per-user upserts so repeated preferences cannot accumulate."""
    values = dict(values)
    for key in ("repo_name", "resource_type", "resource_id"):
        values[key] = values.get(key) or None
    user = (await db.execute(select(TelegramUser.id).where(
        TelegramUser.id == user_id
    ).with_for_update())).scalar_one_or_none()
    if user is None:
        raise ValueError("Notification user not found")
    conditions = [NotificationSubscription.user_id == user_id,
        NotificationSubscription.event_type == values["event_type"],
        NotificationSubscription.endpoint_id == values.get("endpoint_id")]
    for key in ("repo_name", "resource_type", "resource_id"):
        column = getattr(NotificationSubscription, key)
        if values[key] is None:
            conditions.append(or_(column.is_(None), column == ""))
        else:
            conditions.append((func.lower(column) if key == "repo_name" else column) ==
                (values[key].casefold() if key == "repo_name" else values[key]))
    rows = (await db.execute(select(NotificationSubscription).where(*conditions)
        .order_by(NotificationSubscription.id).execution_options(populate_existing=True))).scalars().all()
    if rows:
        row = rows[0]
        for key, value in values.items():
            setattr(row, key, value)
        for duplicate in rows[1:]:
            await db.delete(duplicate)
    else:
        row = NotificationSubscription(user_id=user_id, **values)
        db.add(row)
    await db.flush()
    db.add(NotificationAudit(actor_id=user_id, action="subscription.updated" if rows else "subscription.created", target_id=str(row.id)))
    await db.commit()
    return row


async def create_endpoint(db, user_id, *, provider, address, name, signing_secret=""):
    if provider not in {"email", "webhook"}:
        raise ValueError("Use the existing Telegram private-chat binding")
    if provider == "email":
        address = str(address).strip().lower()
        if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", address) or len(address) > 320:
            raise ValueError("Invalid email address")
    else:
        if not 32 <= len(signing_secret) <= 1024:
            raise ValueError("Webhook signing secret must have 32 to 1024 characters")
        address = await validate_webhook_destination(address)
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
    channel = str(endpoint.provider).lower()
    if not endpoint.enabled:
        raise ValueError("Enable endpoint before verification")
    # Provider I/O and budget commits must not retain a mutable/expired ORM
    # snapshot. Admit the result only for the exact configuration challenged.
    snapshot = SimpleNamespace(**{key: getattr(endpoint, key) for key in (
        "id", "user_id", "provider", "address", "secret_encrypted", "config_version",
    )})
    scope = (
        NotificationEndpoint.id == endpoint_id, NotificationEndpoint.user_id == user_id,
        NotificationEndpoint.provider == snapshot.provider,
        NotificationEndpoint.address == snapshot.address,
        NotificationEndpoint.config_version == snapshot.config_version,
        NotificationEndpoint.secret_encrypted == snapshot.secret_encrypted,
        NotificationEndpoint.enabled.is_(True),
    )
    if channel == "email" and code:
        digest = endpoint.verification_token_hash
        if not digest:
            raise ValueError("Verification code invalid or expired")
        valid = hmac.compare_digest(digest, hashlib.sha256(code.encode()).hexdigest())
        values = {"verification_attempts": NotificationEndpoint.verification_attempts + 1}
        if valid:
            values.update(verified=True, verification_token_hash=None, verification_expires_at=None)
        won = await db.execute(update(NotificationEndpoint).where(
            *scope, NotificationEndpoint.verification_token_hash == digest,
            NotificationEndpoint.verification_expires_at >= now_utc(),
            NotificationEndpoint.verification_attempts < get_settings().notification_verification_max_attempts,
        ).values(**values).execution_options(synchronize_session=False))
        if won.rowcount != 1:
            await db.rollback()
            raise ValueError("Verification code invalid or expired")
        if not valid:
            await db.commit()
            raise ValueError("Verification code invalid or expired")
    elif channel in {"email", "webhook"}:
        available = await notification_service._reserve_provider_start(db, channel)
        if available:
            raise ValueError("Provider is rate limited; retry later")
        provider = notification_service.registry.get(channel)
        if channel == "webhook":
            await provider.verify_endpoint(snapshot)
            won = await db.execute(update(NotificationEndpoint).where(*scope).values(
                verified=True
            ).execution_options(synchronize_session=False))
            if won.rowcount != 1:
                await db.rollback()
                raise ValueError("Endpoint configuration changed; verify again")
        else:
            token = secrets.token_urlsafe(24)
            won = await db.execute(update(NotificationEndpoint).where(*scope).values(
                verification_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                verification_expires_at=now_utc() + timedelta(seconds=get_settings().notification_email_verification_ttl_seconds),
                verification_attempts=0,
            ).execution_options(synchronize_session=False))
            if won.rowcount != 1:
                await db.rollback()
                raise ValueError("Endpoint configuration changed; verify again")
            await db.commit()
            await asyncio.wait_for(provider.send_message(endpoint=snapshot, message=NotificationMessage(
                "verification:" + secrets.token_urlsafe(16), "endpoint.verify", "Verify your notification email",
                f"Your Sakura-AI verification code is: {token}\nEnter it in your notification settings.", "info"
            )), get_settings().notification_send_timeout_seconds)
    else:
        raise NotificationProviderPermanentError("Use Telegram binding handshake", "verification")
    db.add(NotificationAudit(actor_id=user_id, action="endpoint.verification", target_id=str(endpoint_id)))
    await db.commit()
    endpoint = (await db.execute(select(NotificationEndpoint).where(
        NotificationEndpoint.id == endpoint_id, NotificationEndpoint.user_id == user_id,
    ).execution_options(populate_existing=True))).scalar_one()
    return endpoint_view(endpoint)
