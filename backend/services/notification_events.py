"""Transactional notification event entry point and authorization policy.

Producers supply business identities and safe summaries, never channel addresses.
This module never commits a caller's transaction or performs external delivery.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select

from backend.core.time_service import format_rfc3339, now_utc
from backend.models.announcement_models import Announcement
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
)
from backend.models.telegram_models import TelegramUser, UserRepoSubscription

SEVERITIES = {"info": 0, "warning": 1, "error": 2, "critical": 3}
EVENT_TYPES = (
    "review.started", "review.completed", "review.failed", "review.cancelled",
    "issue.started", "issue.completed", "issue.failed", "issue.cancelled",
    "scan.started", "scan.completed", "scan.failed", "scan.cancelled",
    "agent.started", "agent.completed", "agent.failed", "agent.cancelled", "agent.action_required",
    "announcement.published", "security.changed", "security.lockout",
    "quota.exceeded", "payment.completed", "payment.failed", "refund.requested",
    "refund.completed", "refund.rejected", "refund.failed", "system.failed", "endpoint.test",
)


def is_sensitive_event(event):
    return bool(event.sensitive) or event.event_type.startswith(("security.", "payment.", "refund.", "quota."))


@dataclass(frozen=True)
class NotificationMessage:
    event_id: str
    event_type: str
    title: str
    content: str
    severity: str
    detail_url: str | None = None
    payload: dict | None = None
    resource: dict | None = None
    occurred_at: str | None = None
    run_key: str | None = None

    @classmethod
    def from_event(cls, event):
        return cls(event.id, event.event_type, event.title, event.content,
                   event.severity, event.detail_url, json.loads(event.payload_json),
                   {"type": event.resource_type, "id": event.resource_id, "repository": event.repo_name},
                   format_rfc3339(event.occurred_at), event.run_key)

    def as_dict(self):
        return {"id": self.event_id, "type": self.event_type, "title": self.title,
                "content": self.content, "severity": self.severity,
                "detail_url": self.detail_url, "payload": self.payload or {},
                "resource": self.resource, "occurred_at": self.occurred_at, "run_key": self.run_key}


async def insert_once(db, model, values, conflict_columns):
    """Database uniqueness is the authority, including concurrent producers."""
    dialect = db.get_bind().dialect.name
    if dialect in {"mysql", "mariadb"}:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(model).values(**values)
        # A no-op update handles only duplicate keys; INSERT IGNORE would hide
        # foreign-key/validation failures and create success-shaped fallbacks.
        key = conflict_columns[0]
        statement = statement.on_duplicate_key_update(**{key: getattr(model, key)})
    elif dialect in {"sqlite", "postgresql"}:
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        statement = insert(model).values(**values).on_conflict_do_nothing(
            index_elements=conflict_columns
        )
    else:
        raise RuntimeError("Unsupported notification database dialect")
    await db.execute(statement)


async def emit_event(
    db, *, event_type: str, resource_type: str, resource_id: str | int,
    run_key: str, title: str, content: str, recipient_ids=(), repo_name=None,
    severity="info", audience="users", sensitive=False, detail_url=None, payload=None,
) -> NotificationEvent:
    if event_type not in EVENT_TYPES or severity not in SEVERITIES:
        raise ValueError("Unknown notification event type or severity")
    if audience not in {"users", "all", "admins"}:
        raise ValueError("Invalid notification audience")
    sensitive = bool(sensitive) or event_type.startswith(("security.", "payment.", "refund.", "quota."))
    if sensitive and audience == "all":
        raise ValueError("Sensitive notifications require a scoped audience")
    if detail_url and not (detail_url.startswith("/") and not detail_url.startswith("//")):
        raise ValueError("Notification detail URL must be an application path")
    identity = [event_type, resource_type, str(resource_id), str(run_key), audience]
    dedup_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    values = {
        "id": str(uuid.uuid4()), "dedup_key": dedup_key, "event_type": event_type,
        "resource_type": resource_type, "resource_id": str(resource_id), "run_key": str(run_key),
        "title": title[:500], "content": content, "repo_name": repo_name, "severity": severity,
        "audience": audience, "sensitive": sensitive, "detail_url": detail_url,
        "payload_json": json.dumps(payload or {}, ensure_ascii=False),
        "recipients_json": json.dumps(sorted({int(user_id) for user_id in recipient_ids})),
        "occurred_at": now_utc(), "status": "pending",
    }
    await insert_once(db, NotificationEvent, values, ["dedup_key"])
    return (await db.execute(select(NotificationEvent).where(
        NotificationEvent.dedup_key == dedup_key
    ).with_for_update())).scalar_one()


def rule_matches(rule, event):
    return (
        rule.enabled and rule.event_type in {"*", event.event_type, event.event_type.split(".")[0] + ".*"}
        and (not rule.repo_name or rule.repo_name.casefold() == (event.repo_name or "").casefold())
        and (not rule.resource_type or rule.resource_type == event.resource_type)
        and (not rule.resource_id or rule.resource_id == event.resource_id)
        and SEVERITIES.get(event.severity, 0) >= SEVERITIES.get(rule.min_severity, 0)
    )


async def _resource_record(db, model, row_id, cache):
    key = (model, int(row_id))
    if cache is not None and key in cache:
        return cache[key]
    row = await db.get(model, key[1], populate_existing=True)
    if cache is not None:
        cache[key] = row
    return row


async def resource_allowed(db, user, event, *, resource_cache=None):
    """Match current WebUI resource scopes; subscriptions never grant access."""
    if not user or not user.is_active:
        return False
    if event.audience == "admins" and user.role not in {"admin", "super_admin"}:
        return False
    # These resources have stricter WebUI scopes than generic repositories.
    # Check them before owner/subscription eligibility can grant access.
    if event.resource_type == "scan":
        return user.role in {"admin", "super_admin"}
    if event.resource_type == "agent":
        from backend.models.agent_team_models import AgentTeamTask

        row = await _resource_record(db, AgentTeamTask, event.resource_id, resource_cache)
        return bool(row and (user.role in {"admin", "super_admin"}
            or (user.github_username and row.started_by == user.github_username)))
    if event.resource_type == "announcement":
        row = await _resource_record(db, Announcement, event.resource_id, resource_cache)
        return bool(row and row.status == "published" and str(row.publication_version) == event.run_key)
    if event.resource_type in {"payment", "refund_request"}:
        from backend.models.payment_models import Order, RefundRequest

        model = Order if event.resource_type == "payment" else RefundRequest
        row = await _resource_record(db, model, event.resource_id, resource_cache)
        return bool(row and (row.user_id == user.id or event.audience == "admins"))
    if event.resource_type == "user" and event.audience != "admins":
        return event.resource_id == str(user.id) and user.id in json.loads(event.recipients_json)
    if event.repo_name:
        if user.role in {"admin", "super_admin"}:
            return True
        username = (user.github_username or "").casefold()
        if username and event.repo_name.split("/")[0].casefold() == username:
            return True
        # PR/Issue authors already have access to those records in the WebUI.
        from backend.models.database import IssueAnalysis, PRReview
        model = {"review": PRReview, "issue": IssueAnalysis}.get(event.resource_type)
        if model:
            row = await _resource_record(db, model, event.resource_id, resource_cache)
            return bool(row and username and (row.author or "").casefold() == username)
        return False
    if event.audience == "all":
        return True
    return event.audience == "admins" or user.id in json.loads(event.recipients_json)


async def routing_rules(db, event, *, user_ids=None):
    """Filter event families/scopes in SQL and group targets once per user.

    An empty target set still denotes explicit preferences: disabled or
    nonmatching scoped rules must suppress legacy/default routing.
    """
    rule = NotificationSubscription
    matches = and_(
        rule.enabled.is_(True),
        or_(rule.repo_name.is_(None), rule.repo_name == "", func.lower(rule.repo_name) == (event.repo_name or "").casefold()),
        or_(rule.resource_type.is_(None), rule.resource_type == "", rule.resource_type == event.resource_type),
        or_(rule.resource_id.is_(None), rule.resource_id == "", rule.resource_id == event.resource_id),
        case(SEVERITIES, value=rule.min_severity, else_=0) <= SEVERITIES.get(event.severity, 0),
    )
    query = select(rule.user_id, rule.endpoint_id, matches).distinct().where(
        rule.event_type.in_(["*", event.event_type, event.event_type.split(".")[0] + ".*"])
    )
    if user_ids is not None:
        query = query.where(rule.user_id.in_(user_ids))
    grouped = {}
    for user_id, endpoint_id, matched in (await db.execute(query)).all():
        targets = grouped.setdefault(user_id, set())
        if matched:
            targets.add(endpoint_id)
    return grouped


async def candidates(db, event):
    """Map legacy subscriptions via internal user IDs, preserving opt-in."""
    ids = set(json.loads(event.recipients_json))
    rules = await routing_rules(db, event)
    ids.update(user_id for user_id, targets in rules.items() if targets)
    if event.repo_name:
        owners = await db.execute(select(TelegramUser.id).where(
            TelegramUser.github_username == event.repo_name.split("/")[0]
        ))
        ids.update(owners.scalars().all())
        legacy = await db.execute(select(TelegramUser.id).join(
            UserRepoSubscription, TelegramUser.telegram_id == UserRepoSubscription.telegram_id
        ).where(UserRepoSubscription.repo_name == event.repo_name))
        ids.update(legacy.scalars().all())
    if event.audience in {"all", "admins"}:
        query = select(TelegramUser.id).where(TelegramUser.is_active.is_(True))
        if event.audience == "admins":
            query = query.where(TelegramUser.role.in_(["admin", "super_admin"]))
        ids.update((await db.execute(query)).scalars().all())
    # No recipient means no delivery. Never fall back from a repo to admins.
    return sorted(ids), rules
