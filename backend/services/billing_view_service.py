"""Permission-scoped billing projections, shared by API and WebUI.

Public responses deliberately exclude raw pricing snapshots and provider payloads.
Business links use the same access rules as their existing detail pages.
"""

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import format_rfc3339, now_utc
from backend.models.admin_action_log import AdminActionLog
from backend.models.agent_team_models import AgentTeamTask
from backend.models.billing_models import (
    BillingNotice,
    BillingOperation,
    BillingPriceProfile,
    BillingTransaction,
    BillingWallet,
)
from backend.models.database import IssueAnalysis, PRReview
from backend.models.payment_models import Order
from backend.models.scan_models import RepoScan
from backend.services.billing_pricing import units_to_credits
from backend.webui.deps import build_user_scope_filter

BILLING_FEATURES = ("pr_review", "issue_analysis", "agent", "repo_scan")
TRANSACTION_KINDS = (
    "purchase",
    "grant",
    "consumption",
    "refund",
    "adjustment",
    "migration",
)


def wallet_summary(wallet: BillingWallet | None) -> dict:
    balance = int(wallet.balance_units or 0) if wallet else 0
    reserved = int(wallet.reserved_units or 0) if wallet else 0
    threshold = int(wallet.low_balance_threshold_units or 0) if wallet else 0
    return {
        "balance": units_to_credits(balance),
        "reserved": units_to_credits(reserved),
        "available": units_to_credits(balance - reserved),
        "low_balance_threshold": units_to_credits(threshold),
        "low_balance": threshold > 0 and balance - reserved <= threshold,
        "low_balance_notified": bool(wallet.low_balance_notified) if wallet else False,
    }


def add_billing_admin_audit(
    db: AsyncSession, *, actor_id: int, action: str, target_id: str, detail: dict
) -> None:
    """Billing audit commits atomically with the financial/configuration write."""
    db.add(
        AdminActionLog(
            admin_id=actor_id,
            action=action,
            target_type="billing",
            target_id=target_id,
            detail=json.dumps(detail, ensure_ascii=False),
        )
    )


class BillingViewService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def wallet(self, user_id: int) -> dict:
        return wallet_summary(await self.session.get(BillingWallet, user_id))

    async def _source(self, source: dict | None, user: dict) -> dict:
        """Whitelist source identifiers after checking current business access."""
        source = source if isinstance(source, dict) else {}
        admin = user.get("role") in {"admin", "super_admin"}
        username = user.get("sub") or user.get("github_username") or ""
        for field, model, path in (
            ("review_id", PRReview, "pr"),
            ("issue_analysis_id", IssueAnalysis, "issues"),
            ("agent_task_id", AgentTeamTask, "agent-team/tasks"),
            ("scan_id", RepoScan, "scans"),
        ):
            identifier = source.get(field)
            if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
                continue
            try:
                identifier = int(identifier)
            except ValueError:
                continue
            query = select(model).where(model.id == identifier)
            if model in {PRReview, IssueAnalysis}:
                scope = build_user_scope_filter(user, model)
                if scope is not None:
                    query = query.where(scope)
            elif model is AgentTeamTask and not admin:
                query = query.where(AgentTeamTask.started_by == username)
            elif model is RepoScan and not admin:
                # Scan detail pages are admin-only.
                continue
            record = (await self.session.execute(query)).scalar_one_or_none()
            if record is None:
                continue
            repo = f"{record.repo_owner}/{record.repo_name}"
            result = {field: identifier, "repo": repo, "url": f"/{path}/{identifier}"}
            if model is PRReview and record.pr_number is not None:
                result["pr_number"] = record.pr_number
            if model is IssueAnalysis:
                result["issue_number"] = record.issue_number
            return result
        return {}

    async def transactions(
        self,
        user: dict,
        *,
        offset: int = 0,
        limit: int = 20,
        feature: str | None = None,
        kind: str | None = None,
    ) -> dict:
        query = (
            select(BillingTransaction, BillingOperation)
            .outerjoin(
                BillingOperation,
                and_(
                    BillingOperation.operation_id == BillingTransaction.operation_id,
                    BillingOperation.user_id == BillingTransaction.user_id,
                ),
            )
            .where(BillingTransaction.user_id == user["user_id"])
        )
        if feature:
            query = query.where(BillingOperation.feature == feature)
        if kind:
            query = query.where(BillingTransaction.kind == kind)
        total = await self.session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = (
            await self.session.execute(
                query.order_by(BillingTransaction.id.desc()).offset(offset).limit(limit)
            )
        ).all()
        items = []
        for transaction, operation in rows:
            source = await self._source(operation.source, user) if operation else {}
            if not source and transaction.order_id:
                order = (
                    await self.session.execute(
                        select(Order).where(
                            Order.id == transaction.order_id,
                            Order.user_id == user["user_id"],
                        )
                    )
                ).scalar_one_or_none()
                if order is not None:
                    source = {
                        "order_id": order.id,
                        "order_no": order.order_no,
                        "url": "/billing/",
                    }
            items.append(
                {
                    "id": transaction.id,
                    "operation_id": transaction.operation_id,
                    "kind": transaction.kind,
                    "credits": units_to_credits(transaction.delta_units),
                    "created_at": format_rfc3339(transaction.created_at),
                    "order_id": transaction.order_id,
                    "reference_transaction_id": transaction.reference_transaction_id,
                    "feature": operation.feature if operation else None,
                    "source": source,
                }
            )
        return {
            "items": items,
            "total": int(total or 0),
            "offset": offset,
            "limit": limit,
        }

    async def operations(
        self,
        user: dict,
        *,
        offset: int = 0,
        limit: int = 20,
        feature: str | None = None,
        status: str | None = None,
    ) -> dict:
        query = select(BillingOperation).where(
            BillingOperation.user_id == user["user_id"]
        )
        if feature:
            query = query.where(BillingOperation.feature == feature)
        if status:
            query = query.where(BillingOperation.status == status)
        total = await self.session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        operations = (
            (
                await self.session.execute(
                    query.order_by(
                        BillingOperation.created_at.desc(),
                        BillingOperation.operation_id,
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items = []
        for operation in operations:
            items.append(
                {
                    "operation_id": operation.operation_id,
                    "feature": operation.feature,
                    "status": operation.status,
                    "outcome": operation.outcome,
                    "known_credits": operation.known_credits,
                    "reserved_credits": units_to_credits(operation.reserve_units),
                    "settled_credits": units_to_credits(operation.settled_units),
                    "created_at": format_rfc3339(operation.created_at),
                    "updated_at": format_rfc3339(operation.updated_at),
                    "source": await self._source(operation.source, user),
                }
            )
        return {
            "items": items,
            "total": int(total or 0),
            "offset": offset,
            "limit": limit,
        }

    async def notices(self, user_id: int, *, offset: int = 0, limit: int = 20) -> dict:
        query = select(BillingNotice).where(BillingNotice.user_id == user_id)
        total = await self.session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = (
            (
                await self.session.execute(
                    query.order_by(BillingNotice.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "id": notice.id,
                    "kind": notice.kind,
                    "created_at": format_rfc3339(notice.created_at),
                    "read_at": format_rfc3339(notice.read_at)
                    if notice.read_at
                    else None,
                    "transaction_id": notice.transaction_id,
                }
                for notice in rows
            ],
            "total": int(total or 0),
            "offset": offset,
            "limit": limit,
        }

    async def read_notice(self, user_id: int, notice_id: int) -> bool:
        notice = (
            await self.session.execute(
                select(BillingNotice)
                .where(BillingNotice.id == notice_id, BillingNotice.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if notice is None:
            return False
        if notice.read_at is None:
            notice.read_at = now_utc()
            await self.session.flush()
        return True

    async def prices(self, *, offset: int = 0, limit: int = 50) -> dict:
        total = await self.session.scalar(select(func.count(BillingPriceProfile.id)))
        rows = (
            (
                await self.session.execute(
                    select(BillingPriceProfile)
                    .order_by(BillingPriceProfile.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "id": row.id,
                    "provider_id": row.provider_id,
                    "account_id": row.account_id,
                    "scope_key": row.scope_key,
                    "model_id": row.model_id,
                    "call_kind": row.call_kind,
                    "version": row.version,
                    "config": row.config,
                    "created_at": format_rfc3339(row.created_at),
                    "actor_id": row.actor_id,
                }
                for row in rows
            ],
            "total": int(total or 0),
            "offset": offset,
            "limit": limit,
        }


def parse_pricing_json(value: str) -> dict[str, Any]:
    """Reject floating JSON values instead of silently accepting binary precision."""
    parsed = json.loads(value, parse_float=Decimal)
    if not isinstance(parsed, dict):
        raise ValueError("Pricing configuration must be a JSON object")

    def normalize(item):
        if isinstance(item, Decimal):
            raise ValueError(
                "Price and conversion amounts must be quoted decimal strings"
            )
        if isinstance(item, dict):
            return {key: normalize(value) for key, value in item.items()}
        if isinstance(item, list):
            return [normalize(value) for value in item]
        return item

    return normalize(parsed)


def payment_event_summary(record) -> dict:
    """Administrative inbox DTO. Never serialize operator proofs or raw evidence."""
    from backend.services.payment.currency_units import format_minor_amount

    evidence = record.evidence if isinstance(record.evidence, dict) else {}
    amount = evidence.get("amount_cents")
    amount = (
        amount if isinstance(amount, int) and not isinstance(amount, bool) else None
    )
    currency = evidence.get("currency")
    currency = currency[:10] if isinstance(currency, str) else None
    formatted_amount = None
    if amount is not None and currency:
        try:
            formatted_amount = format_minor_amount(amount, currency)
        except ValueError:
            pass  # Invalid/unknown currency remains visible as unresolved evidence.
    return {
        "id": record.id,
        "provider": record.provider,
        "event_key": record.event_key,
        "type": evidence.get("type"),
        "status": record.status,
        "pending_reason": record.pending_reason,
        "order_id": record.order_id,
        "order_no": str(evidence.get("order_no") or "")[:64],
        "amount_cents": amount,
        "currency": currency,
        "formatted_amount": formatted_amount,
        "received_at": format_rfc3339(record.received_at),
        "resolved_at": format_rfc3339(record.resolved_at)
        if record.resolved_at
        else None,
        "processed": record.status == "processed",
    }


async def pending_payment_events(session, *, limit: int = 20, offset: int = 0) -> dict:
    from backend.services.payment_event_service import PaymentEventService

    records = await PaymentEventService(session).list_pending(
        limit=limit + 1, offset=offset
    )
    return {
        "items": [payment_event_summary(record) for record in records[:limit]],
        "offset": offset,
        "limit": limit,
        "has_more": len(records) > limit,
    }
