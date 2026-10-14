"""Authenticated notification-center APIs used inside existing pages."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.core.time_service import format_rfc3339, now_utc
from backend.models.announcement_models import NotificationDelivery
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationAudit,
    NotificationEvent,
    NotificationSubscription,
    UserNotification,
)
from backend.models.telegram_models import TelegramUser
from backend.services.notification_events import (
    EVENT_TYPES,
    SEVERITIES,
    emit_event,
    resource_allowed,
)
from backend.services.notification_preferences import (
    cancel_endpoint_deliveries,
    create_endpoint,
    endpoint_view,
    owned_endpoint,
    save_subscription,
    verify_endpoint,
)
from backend.services.notification_service import (
    NotificationProviderError,
    NotificationProviderPermanentError,
    notification_service,
)
from backend.services.secret_crypto_service import encrypt_secret
from backend.webui.deps import (
    get_db,
    require_auth,
    require_csrf_header,
    require_super_admin,
)

router = APIRouter(prefix="/notifications", tags=["WebUI Notifications"])


class EndpointCreate(BaseModel):
    provider: str = Field(max_length=50)
    address: str = Field(max_length=320)
    name: str = Field(default="", max_length=100)
    signing_secret: SecretStr = SecretStr("")


class EndpointUpdate(BaseModel):
    name: str = Field(max_length=100)
    enabled: bool
    signing_secret: SecretStr | None = None


class Verification(BaseModel):
    code: str = Field(default="", max_length=200, repr=False)


class Preference(BaseModel):
    event_type: str = Field(max_length=100)
    repo_name: str | None = Field(default=None, max_length=255)
    resource_type: str | None = Field(default=None, max_length=50)
    resource_id: str | None = Field(default=None, max_length=255)
    min_severity: str = "info"
    endpoint_id: int | None = None
    enabled: bool = True


@router.get("/settings")
async def settings(user=Depends(require_auth), db=Depends(get_db)):
    endpoints = (await db.execute(select(NotificationEndpoint).where(NotificationEndpoint.user_id == user["user_id"]).order_by(NotificationEndpoint.id))).scalars().all()
    rules = (await db.execute(select(NotificationSubscription).where(NotificationSubscription.user_id == user["user_id"]).order_by(NotificationSubscription.id))).scalars().all()
    return {"providers": notification_service.registry.descriptors(), "event_types": EVENT_TYPES,
            "endpoints": [endpoint_view(row) for row in endpoints],
            "subscriptions": [{"id": row.id, **{key: getattr(row, key) for key in Preference.model_fields}} for row in rules]}


@router.post("/endpoints", dependencies=[Depends(require_csrf_header)])
async def add_endpoint(body: EndpointCreate, user=Depends(require_auth), db=Depends(get_db)):
    try:
        values = body.model_dump()
        values["signing_secret"] = body.signing_secret.get_secret_value()
        row = await create_endpoint(db, int(user["user_id"]), **values)
        return endpoint_view(row)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Notification address is already bound") from None
    except (ValueError, NotificationProviderPermanentError):
        raise HTTPException(400, "Invalid notification endpoint configuration") from None
    except NotificationProviderError:
        raise HTTPException(503, "Notification provider unavailable") from None


@router.patch("/endpoints/{endpoint_id}", dependencies=[Depends(require_csrf_header)])
async def update_endpoint(endpoint_id: int, body: EndpointUpdate, user=Depends(require_auth), db=Depends(get_db)):
    try:
        row = await owned_endpoint(db, int(user["user_id"]), endpoint_id, for_update=True)
    except ValueError:
        raise HTTPException(404, "Notification endpoint not found") from None
    if str(row.provider).lower() == "telegram" and body.enabled and not row.enabled:
        raise HTTPException(409, "Use the existing Telegram binding handshake to activate this endpoint")
    if body.signing_secret is not None or row.enabled != body.enabled:
        row.config_version = (row.config_version or 1) + 1
    if body.signing_secret is not None:
        secret = body.signing_secret.get_secret_value()
        if str(row.provider).lower() != "webhook" or not 32 <= len(secret) <= 1024:
            raise HTTPException(400, "Invalid endpoint credential")
        row.secret_encrypted = encrypt_secret(secret)
        row.verified = False
    row.name, row.enabled = body.name, body.enabled
    if not body.enabled:
        await cancel_endpoint_deliveries(db, endpoint_id)
    db.add(NotificationAudit(actor_id=int(user["user_id"]), action="endpoint.updated", target_id=str(endpoint_id)))
    await db.commit()
    return endpoint_view(row)


@router.delete("/endpoints/{endpoint_id}", dependencies=[Depends(require_csrf_header)])
async def remove_endpoint(endpoint_id: int, user=Depends(require_auth), db=Depends(get_db)):
    try:
        row = await owned_endpoint(db, int(user["user_id"]), endpoint_id, for_update=True)
    except ValueError:
        raise HTTPException(404, "Notification endpoint not found") from None
    # Retain endpoint identity for diagnostic history. No authentication row or
    # role is changed, including the legacy Telegram account mirror.
    if row.enabled:
        row.config_version = (row.config_version or 1) + 1
    row.enabled = False
    await cancel_endpoint_deliveries(db, endpoint_id)
    db.add(NotificationAudit(actor_id=int(user["user_id"]), action="endpoint.unbound", target_id=str(endpoint_id)))
    await db.commit()
    return {"ok": True}


@router.post("/endpoints/{endpoint_id}/verify", dependencies=[Depends(require_csrf_header)])
async def verify(endpoint_id: int, body: Verification, user=Depends(require_auth), db=Depends(get_db)):
    try:
        return await verify_endpoint(db, int(user["user_id"]), endpoint_id, code=body.code)
    except ValueError:
        raise HTTPException(400, "Verification unavailable, invalid or expired") from None
    except NotificationProviderError:
        raise HTTPException(502, "Notification verification provider unavailable") from None


@router.post("/endpoints/{endpoint_id}/test", dependencies=[Depends(require_csrf_header)])
async def test_endpoint(endpoint_id: int, user=Depends(require_auth), db=Depends(get_db)):
    try:
        endpoint = await owned_endpoint(db, int(user["user_id"]), endpoint_id)
    except ValueError:
        raise HTTPException(404, "Notification endpoint not found") from None
    if not endpoint.enabled or not endpoint.verified:
        raise HTTPException(400, "Endpoint must be enabled and verified")
    # Identical routing, fresh eligibility, network policy and rate budget as a
    # normal send; the HTTP request itself never invokes a provider.
    item = await emit_event(db, event_type="endpoint.test", resource_type="user", resource_id=user["user_id"],
        run_key=str(uuid.uuid4()), title="Sakura-AI notification test", content="Your notification endpoint is ready.",
        recipient_ids=[int(user["user_id"])], payload={"endpoint_id": endpoint_id})
    db.add(NotificationAudit(actor_id=int(user["user_id"]), action="endpoint.test", target_id=str(endpoint_id)))
    await db.commit()
    return {"event_id": item.id, "status": "queued"}


@router.get("/endpoints/{endpoint_id}/status")
async def endpoint_status(endpoint_id: int, user=Depends(require_auth), db=Depends(get_db)):
    try:
        await owned_endpoint(db, int(user["user_id"]), endpoint_id)
    except ValueError:
        raise HTTPException(404, "Notification endpoint not found") from None
    rows = (await db.execute(select(NotificationDelivery).where(
        NotificationDelivery.endpoint_id == endpoint_id, NotificationDelivery.user_id == user["user_id"]
    ).order_by(NotificationDelivery.id.desc()).limit(20))).scalars().all()
    return {"deliveries": [{"id": row.id, "status": row.status, "category": row.error_category,
            "attempts": row.attempts, "sent_at": format_rfc3339(row.sent_at) if row.sent_at else None} for row in rows]}


@router.post("/subscriptions", dependencies=[Depends(require_csrf_header)])
async def add_subscription(body: Preference, user=Depends(require_auth), db=Depends(get_db)):
    valid = {"*", *EVENT_TYPES, *(kind.split(".")[0] + ".*" for kind in EVENT_TYPES)}
    if body.event_type not in valid or body.min_severity not in SEVERITIES:
        raise HTTPException(400, "Invalid notification preference")
    if body.repo_name and (body.repo_name.count("/") != 1 or any(part in {"", ".", ".."} for part in body.repo_name.split("/"))):
        raise HTTPException(400, "Invalid repository scope")
    if body.endpoint_id is not None:
        try:
            await owned_endpoint(db, int(user["user_id"]), body.endpoint_id)
        except ValueError:
            raise HTTPException(404, "Notification endpoint not found") from None
    # Persisting a preference grants no resource permission. Fanout/send/read
    # independently evaluate the current application resource ACL.
    try:
        row = await save_subscription(db, int(user["user_id"]), body.model_dump())
    except ValueError:
        raise HTTPException(404, "Notification user not found") from None
    return {"id": row.id}


@router.delete("/subscriptions/{subscription_id}", dependencies=[Depends(require_csrf_header)])
async def remove_subscription(subscription_id: int, user=Depends(require_auth), db=Depends(get_db)):
    # Keep the user -> rule lock order used by fanout and preference upserts.
    await db.get(TelegramUser, int(user["user_id"]), with_for_update=True, populate_existing=True)
    row = (await db.execute(select(NotificationSubscription).where(
        NotificationSubscription.id == subscription_id, NotificationSubscription.user_id == user["user_id"]
    ))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Notification preference not found")
    await db.delete(row)
    db.add(NotificationAudit(actor_id=int(user["user_id"]), action="subscription.deleted", target_id=str(subscription_id)))
    await db.commit()
    return {"ok": True}


@router.get("/history")
async def history(user=Depends(require_auth), db=Depends(get_db), page: int = Query(1, ge=1)):
    owner = await db.get(TelegramUser, int(user["user_id"]), populate_existing=True)
    if not owner or not owner.is_active:
        return {"items": [], "has_next": False, "page": page, "unread": 0}
    scope = [UserNotification.user_id == user["user_id"], NotificationEvent.resource_type != "announcement"]
    if owner.role not in {"admin", "super_admin"}:
        scope.append(NotificationEvent.resource_type != "scan")
    rows = (await db.execute(select(UserNotification, NotificationEvent).join(
        NotificationEvent, NotificationEvent.id == UserNotification.event_id
    ).where(*scope).order_by(UserNotification.id.desc()).offset((page - 1) * 50).limit(51))).all()
    items = []
    for inbox, item in rows[:50]:
        if not await resource_allowed(db, owner, item):
            continue
        items.append({"id": inbox.id, "type": item.event_type, "title": item.title,
            "content": item.content, "severity": item.severity, "detail_url": item.detail_url,
            "read": inbox.read_at is not None, "occurred_at": format_rfc3339(item.occurred_at)})
    unread = (await db.execute(select(func.count(UserNotification.id)).join(NotificationEvent,
        NotificationEvent.id == UserNotification.event_id).where(*scope, UserNotification.read_at.is_(None)))).scalar_one()
    return {"items": items, "has_next": len(rows) > 50, "page": page, "unread": unread}


@router.post("/read-all", dependencies=[Depends(require_csrf_header)])
async def mark_all_read(user=Depends(require_auth), db=Depends(get_db)):
    await db.execute(update(UserNotification).where(UserNotification.user_id == user["user_id"],
        UserNotification.read_at.is_(None)).values(read_at=now_utc()))
    await db.commit()
    return {"ok": True}


@router.post("/{notification_id}/read", dependencies=[Depends(require_csrf_header)])
async def mark_read(notification_id: int, user=Depends(require_auth), db=Depends(get_db)):
    result = await db.execute(update(UserNotification).where(UserNotification.id == notification_id,
        UserNotification.user_id == user["user_id"]).values(read_at=now_utc()))
    await db.commit()
    if result.rowcount != 1:
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.get("/admin/diagnostics")
async def diagnostics(user=Depends(require_super_admin), db=Depends(get_db)):
    counts = (await db.execute(select(NotificationDelivery.channel, NotificationDelivery.status,
        NotificationDelivery.error_category, func.count()).group_by(NotificationDelivery.channel,
        NotificationDelivery.status, NotificationDelivery.error_category))).all()
    failures = (await db.execute(select(NotificationDelivery).where(NotificationDelivery.status.in_(["failed", "pending", "cancelled"]))
        .order_by(NotificationDelivery.updated_at.desc()).limit(100))).scalars().all()
    audit = (await db.execute(select(NotificationAudit).order_by(NotificationAudit.id.desc()).limit(50))).scalars().all()
    events = (await db.execute(select(NotificationEvent).where(NotificationEvent.status.in_(["pending", "no_recipients"]))
        .order_by(NotificationEvent.occurred_at.desc()).limit(100))).scalars().all()
    return {"providers": notification_service.registry.descriptors(),
            "events": [{"id": item.id, "type": item.event_type, "status": item.status,
                "category": item.error_category} for item in events],
            "counts": [{"channel": channel, "status": status, "category": category, "count": count} for channel, status, category, count in counts],
            "deliveries": [{"id": row.id, "channel": row.channel, "status": row.status,
                "category": row.error_category, "attempts": row.attempts,
                "next_retry_at": format_rfc3339(row.next_retry_at) if row.next_retry_at else None} for row in failures],
            "audit": [{"actor_id": row.actor_id, "action": row.action, "target_id": row.target_id, "at": format_rfc3339(row.created_at)} for row in audit]}


@router.post("/admin/deliveries/{delivery_id}/retry", dependencies=[Depends(require_csrf_header)])
async def retry(delivery_id: int, user=Depends(require_super_admin), db=Depends(get_db)):
    row = await db.get(NotificationDelivery, delivery_id, populate_existing=True)
    if not row or not row.event_id or row.status != "failed" or row.claim_token:
        raise HTTPException(409, "Only unclaimed failed event deliveries can be retried")
    item = await db.get(NotificationEvent, row.event_id, populate_existing=True)
    eligible, _ = await notification_service._event_eligible(db, row, item)
    if not eligible:
        raise HTTPException(409, "Recipient no longer eligible")
    result = await db.execute(update(NotificationDelivery).where(NotificationDelivery.id == delivery_id,
        NotificationDelivery.status == "failed", NotificationDelivery.claim_token.is_(None)).values(
        status="pending", next_retry_at=now_utc(), attempts=0, error_category=None, error_message=None))
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Delivery state changed")
    db.add(NotificationAudit(actor_id=int(user["user_id"]), action="delivery.retry", target_id=str(delivery_id)))
    await db.commit()
    return {"status": "queued"}
