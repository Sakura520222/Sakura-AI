"""Business state adapters collected at flush and written in the SAME transaction.

The application sessionmaker enables this hook explicitly; migrations and
historical imports do not. No provider is invoked from an ORM transaction.
Bulk task writers call emit_task_event explicitly before committing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models.notification_models import NotificationEvent
from backend.models.telegram_models import TelegramUser

_installed = False
_OUTCOMES = {
    "reviewing": "started", "analyzing": "started", "indexing": "started",
    "cloning": "started", "completed": "completed", "failed": "failed",
    "cancelled": "cancelled", "waiting_human": "action_required",
}


def _insert_event(conn, *, event_type, resource_type, resource_id, run_key,
                  title, content, recipient_ids=(), repo_name=None,
                  audience="users", sensitive=False, severity="info", detail_url=None, payload=None):
    identity = [event_type, resource_type, str(resource_id), str(run_key), audience]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    values = {
        "id": str(uuid.uuid4()), "dedup_key": key, "event_type": event_type,
        "resource_type": resource_type, "resource_id": str(resource_id),
        "run_key": str(run_key), "title": title[:500], "content": content,
        "repo_name": repo_name, "audience": audience, "sensitive": sensitive,
        "severity": severity, "detail_url": detail_url, "occurred_at": now_utc(),
        "payload_json": json.dumps(payload or {}, ensure_ascii=False), "recipients_json": json.dumps(sorted(set(recipient_ids))), "status": "pending",
    }
    if conn.dialect.name in {"mysql", "mariadb"}:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(NotificationEvent).values(**values).on_duplicate_key_update(dedup_key=NotificationEvent.dedup_key)
    else:
        if conn.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        statement = insert(NotificationEvent).values(**values).on_conflict_do_nothing(index_elements=["dedup_key"])
    conn.execute(statement)


def _task_result_metadata(kind, record, outcome):
    """Severity follows business result fields, never provider/model limits."""
    from backend.models.database import IssuePriority, ReviewDecision

    severity = "error" if outcome == "failed" else "warning" if outcome == "action_required" else "info"
    payload = {}
    if outcome == "completed":
        if kind == "scan":
            payload = {"critical_count": record.critical_count or 0, "major_count": record.major_count or 0}
            if payload["critical_count"]:
                severity = "critical"
            elif payload["major_count"]:
                severity = "warning"
        elif kind == "issue":
            priority = getattr(record.priority, "value", record.priority)
            payload = {"priority": priority, "issue_number": record.issue_number}
            if priority == IssuePriority.CRITICAL.value:
                severity = "critical"
            elif priority == IssuePriority.HIGH.value:
                severity = "warning"
        elif kind == "review":
            decision = getattr(record.decision, "value", record.decision)
            payload = {"decision": decision, "pr_number": record.pr_number}
            if decision == ReviewDecision.REQUEST_CHANGES.value:
                severity = "warning"
    return severity, payload


def notification_outbox_enabled(db):
    """Use the same explicit opt-in for ORM and bulk application writes."""
    return getattr(db, "info", {}).get("notification_outbox") is True


def _task_initiator(kind, record):
    if kind == "agent":
        return record.started_by
    if kind == "scan":
        return record.triggered_by
    return getattr(record, "author", None)


def _task_event_values(kind, record, outcome, recipient_ids):
    repo = getattr(record, "repo_full_name", None) or record.repo_name
    if "/" not in repo:
        repo = f"{record.repo_owner}/{repo}"
    if kind == "agent":
        run_key = f"{record.resume_count}:{record.iteration_count}"
        if record.execution_round:
            run_key = f"{record.execution_round}:{run_key}"
        detail_url = f"/agent-team/?task_id={record.id}"
    else:
        run_key = str(getattr(record, "analysis_version", None) or record.id)
        if kind == "scan" and record.execution_round:
            run_key = f"{record.execution_round}:{run_key}"
        prefix = {"review": "pr", "issue": "issues", "scan": "scans"}[kind]
        detail_url = f"/{prefix}/{record.id}"
    severity, payload = _task_result_metadata(kind, record, outcome)
    return {"event_type": f"{kind}.{outcome}", "resource_type": kind,
        "resource_id": record.id, "run_key": run_key, "title": f"{repo}: {kind} {outcome}",
        "content": f"{kind.capitalize()} task #{record.id}: {outcome}.", "repo_name": repo,
        "recipient_ids": recipient_ids, "severity": severity, "payload": payload, "detail_url": detail_url}


async def emit_task_event(db, kind, record, outcome, *, refresh=False):
    """Persist a successful bulk transition on the caller's transaction.

    The caller must first win its conditional UPDATE or lock the affected rows.
    This uses the ORM adapter's identity, so a later flush/repeated callback
    cannot create a second event. Import/migration sessions remain opted out.
    Any persistence failure rolls back the transition before a worker can
    catch the exception and accidentally commit state without its event.
    """
    if not notification_outbox_enabled(db):
        return
    from backend.services.notification_events import emit_event

    try:
        if refresh:
            await db.refresh(record)
        initiator = _task_initiator(kind, record)
        ids = (await db.execute(select(TelegramUser.id).where(
            func.lower(TelegramUser.github_username) == initiator.casefold()
        ))).scalars().all() if initiator else []
        return await emit_event(db, **_task_event_values(kind, record, outcome, ids))
    except BaseException:
        await db.rollback()
        raise


async def _lockout_round(db, user_id):
    from backend.models.notification_models import NotificationLockoutRound
    from backend.services.notification_events import insert_once

    await insert_once(db, NotificationLockoutRound, {"user_id": user_id, "generation": 0}, ["user_id"])
    return (await db.execute(select(NotificationLockoutRound).where(
        NotificationLockoutRound.user_id == user_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()


async def emit_mfa_lockout_event(db, user_id, started_at, ttl):
    """Collapse fallback/Redis callbacks into one persisted lock generation."""
    from backend.services.notification_events import emit_event

    current = await _lockout_round(db, user_id)
    if current.reset_at and started_at <= current.reset_at:
        return None  # A successful reset has superseded this delayed callback.
    if current.active_until is None or started_at >= current.active_until:
        current.generation += 1
        current.started_at = started_at
    current.latest_started_at = max(current.latest_started_at, started_at) if current.latest_started_at else started_at
    until = started_at + timedelta(seconds=ttl)
    current.active_until = max(current.active_until, until) if current.active_until else until
    return await emit_event(db, event_type="security.lockout", resource_type="user", resource_id=user_id,
        run_key=f"lock:{current.generation}", title="Account temporarily locked",
        content="Repeated MFA verification failures locked your account.", recipient_ids=[user_id],
        sensitive=True, severity="warning", detail_url="/settings/")


async def reset_mfa_lockout_round(db, user_id, reset_at):
    current = await _lockout_round(db, user_id)
    if current.reset_at and reset_at <= current.reset_at:
        return
    current.reset_at = reset_at
    if current.active_until and current.latest_started_at > reset_at:
        if current.started_at <= reset_at:
            # The primary reset completed before a new lock, but its SQL
            # update arrived later. Split the window instead of losing the
            # new alert or invalidating the new lock's deduplication round.
            latest = current.latest_started_at
            ttl = (current.active_until - latest).total_seconds()
            current.active_until = None
            await emit_mfa_lockout_event(db, user_id, latest, ttl)
    else:
        current.active_until = None


def _assign_task_execution_round(session, record, status):
    """Serialize admissions with the task write, including duplicate queues.

    Queueing starts a round before skills/cloning can fail. Terminal events
    refresh its persisted identity under the same lock. Migration/import
    sessions do not opt in, and round zero retains legacy event identities.
    """
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.scan_models import RepoScan

    model = type(record)
    admission, start, legacy = ("queued", "cloning", {"queued", "candidate"}) if model is AgentTeamTask else (
        "pending", "indexing", {"pending"}
    )
    if model not in {AgentTeamTask, RepoScan}:
        raise ValueError("Unsupported task execution round")

    if record in session.new:
        if status in {admission, start}:
            record.execution_round = (record.execution_round or 0) + 1
        return
    current = session.connection().execute(select(
        model.execution_round, model.status
    ).where(model.id == record.id).with_for_update()).one_or_none()
    if current is None:
        return  # The ORM flush will reject a concurrently deleted task.
    run = current.execution_round or 0
    if status == admission and current.status != admission:
        run += 1
    elif status == start and not run and current.status in legacy:
        run = 1  # First execution of a legacy pending task after migration.
    record.execution_round = run


def _collect(session, *_):
    if not session.info.get("notification_outbox"):
        session.info.pop("notification_changes", None)
        return
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.database import IssueAnalysis, PRReview
    from backend.models.payment_models import Order, RefundRequest
    from backend.models.scan_models import RepoScan
    from backend.models.telegram_models import UserWebAuthnCredential

    kinds = {PRReview: "review", IssueAnalysis: "issue", RepoScan: "scan", AgentTeamTask: "agent"}
    changes = []
    for record in set(session.new) | set(session.dirty) | set(session.deleted):
        state = inspect(record)
        if type(record) in kinds and state.attrs.status.history.has_changes():
            value = getattr(record.status, "value", record.status)
            outcome = _OUTCOMES.get(value)
            # Agent phase changes are deliberately not process notifications.
            if isinstance(record, AgentTeamTask):
                if value in {"queued", "cloning", "completed", "failed", "cancelled", "waiting_human"}:
                    _assign_task_execution_round(session, record, value)
                if value not in {"cloning", "completed", "failed", "cancelled", "waiting_human"}:
                    continue
            if isinstance(record, RepoScan):
                if value in {"pending", "indexing", "completed", "failed", "cancelled"}:
                    _assign_task_execution_round(session, record, value)
                if value == "analyzing":
                    continue
            if outcome:
                changes.append(("task", record, kinds[type(record)], outcome))
        elif isinstance(record, Order) and state.attrs.status.history.has_changes():
            outcome = {"fulfilled": "payment.completed", "refunded": "refund.completed", "expired": "payment.failed"}.get(record.status)
            if outcome:
                changes.append(("personal", record, outcome, "users"))
        elif isinstance(record, RefundRequest) and (record in session.new or state.attrs.status.history.has_changes()):
            outcome = {"pending": "refund.requested", "rejected": "refund.rejected", "failed": "refund.failed"}.get(record.status)
            if outcome:
                changes.append(("personal", record, outcome, "users"))
                if outcome in {"refund.requested", "refund.failed"}:
                    changes.append(("personal", record, outcome, "admins"))
        elif isinstance(record, TelegramUser) and record not in session.new and any(
            state.attrs[field].history.has_changes() for field in ("totp_enabled", "mfa_required", "role", "is_active")
        ):
            changes.append(("security", record, "Account security settings changed", str(uuid.uuid4())))
        elif isinstance(record, UserWebAuthnCredential) and (record in session.new or record in session.deleted):
            changes.append(("credential", record, "Passkey added" if record in session.new else "Passkey removed", str(uuid.uuid4())))
    session.info["notification_changes"] = changes


def _persist(session, *_):
    changes = session.info.pop("notification_changes", [])
    if not changes or not session.info.get("notification_outbox"):
        return
    conn = session.connection()
    for action, record, kind, outcome in changes:
        if action == "task":
            initiator = _task_initiator(kind, record)
            ids = conn.execute(select(TelegramUser.id).where(
                func.lower(TelegramUser.github_username) == initiator.casefold()
            )).scalars().all() if initiator else []
            _insert_event(conn, **_task_event_values(kind, record, outcome, ids))
        elif action == "personal":
            _insert_event(conn, event_type=kind, resource_type="refund_request" if type(record).__name__ == "RefundRequest" else "payment",
                resource_id=record.id, run_key=str(getattr(record, "reviewed_at", None) or record.id),
                title=kind.replace(".", " ").capitalize(), content=f"Account transaction #{record.id}: {kind}.",
                recipient_ids=[record.user_id], audience=outcome, sensitive=True,
                severity="error" if kind.endswith("failed") else "info", detail_url="/billing")
        else:
            user_id = record.id if action == "security" else record.user_id
            _insert_event(conn, event_type="security.changed", resource_type="user", resource_id=user_id,
                run_key=outcome, title=kind, content=kind + ". Review your account settings.",
                recipient_ids=[user_id], sensitive=True, severity="warning", detail_url="/settings/")


def install_notification_outbox():
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _collect)
    event.listen(Session, "after_flush", _persist)
    _installed = True
