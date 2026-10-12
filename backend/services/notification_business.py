"""Business state adapters collected at flush and written in the SAME transaction.

The application sessionmaker enables this hook explicitly; migrations and
historical imports do not. No provider is invoked from an ORM transaction.
Bulk SQL writers must call emit_event explicitly (quota and MFA lockout do).
"""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import event, inspect, select
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


def _collect(session, *_):
    if not session.info.get("notification_outbox"):
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
            if isinstance(record, AgentTeamTask) and value not in {"cloning", "completed", "failed", "cancelled", "waiting_human"}:
                continue
            if isinstance(record, RepoScan) and value == "analyzing":
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
    if not changes:
        return
    conn = session.connection()
    for action, record, kind, outcome in changes:
        if action == "task":
            repo = getattr(record, "repo_full_name", None) or record.repo_name
            if "/" not in repo:
                repo = f"{record.repo_owner}/{repo}"
            author = getattr(record, "author", None)
            ids = conn.execute(select(TelegramUser.id).where(TelegramUser.github_username == author)).scalars().all() if author else []
            if kind == "agent":
                run_key = f"{record.resume_count}:{record.iteration_count}"
            else:
                run_key = str(getattr(record, "analysis_version", None) or record.id)
            prefix = {"review": "pr", "issue": "issues", "scan": "scans", "agent": "agent-team"}[kind]
            severity, payload = _task_result_metadata(kind, record, outcome)
            _insert_event(conn, event_type=f"{kind}.{outcome}", resource_type=kind,
                resource_id=record.id, run_key=run_key, title=f"{repo}: {kind} {outcome}",
                content=f"{kind.capitalize()} task #{record.id}: {outcome}.", repo_name=repo,
                recipient_ids=ids, severity=severity, payload=payload, detail_url=f"/{prefix}/{record.id}")
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
