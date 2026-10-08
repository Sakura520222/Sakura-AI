"""Audited, resumable repair of historical one-time quota inflation.

No conversion rate or historical purchased plan is inferred. Operators supply
source evidence and explicit preserved remaining allowance (or a confirmed
Credits conversion) in a reviewed manifest. Dry-run never mutates rows.
"""

from decimal import Decimal

from sqlalchemy import select

from backend.core.time_service import now_utc, parse_rfc3339
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    LegacyEntitlementEvent,
    RedeemCodeSnapshotAudit,
)
from backend.models.payment_models import Order, Plan, RedeemCode
from backend.models.telegram_models import TelegramUser
from backend.services.legacy_entitlement_service import LegacyEntitlementService
from backend.services.payment_service import PaymentError, PaymentService


class LegacyBillingMigrationService:
    def __init__(self, session):
        self.session = session

    async def audit(self, *, after_id: int = 0, batch_size: int = 100) -> dict:
        orders = (
            (
                await self.session.execute(
                    select(Order)
                    .where(Order.id > after_id, Order.status == "fulfilled")
                    .order_by(Order.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        return {
            "orders": [
                {
                    "order_id": order.id,
                    "user_id": order.user_id,
                    "has_purchased_snapshot": bool(order.plan_snapshot),
                    "status": "source_available"
                    if order.plan_snapshot
                    else "needs_historical_evidence",
                }
                for order in orders
            ],
            "next_after_id": orders[-1].id if orders else after_id,
            "rule": "Do not infer historical benefits from today's Plan or retroactively bill Usage",
        }

    async def convert_source(
        self,
        entitlement_id: int,
        credits,
        *,
        actor_id: int,
        evidence: str,
        dry_run: bool = True,
    ) -> dict:
        from backend.models.billing_models import BillingTransaction
        from backend.services.billing_service import BillingService

        actor = await self.session.get(TelegramUser, actor_id)
        if not dry_run and (
            not actor or not actor.is_active or actor.role != "super_admin"
        ):
            raise PaymentError("Legacy conversion requires an active super-admin")
        PaymentService._validate_billing_plan(credits, None, None)
        if Decimal(str(credits)) <= 0 or not evidence.strip():
            raise PaymentError(
                "Conversion requires positive exact Credits and reviewed source evidence"
            )
        source = await self.session.get(LegacyEntitlement, entitlement_id)
        if not source:
            raise PaymentError("Legacy entitlement source not found")
        if not dry_run:
            await BillingService(self.session).get_wallet(source.user_id)
            source = (
                await self.session.execute(
                    select(LegacyEntitlement)
                    .where(LegacyEntitlement.id == entitlement_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
        key = f"legacy-source:{source.id}:conversion"
        existing_statement = select(BillingTransaction).where(
            BillingTransaction.idempotency_key == key
        )
        if not dry_run:
            existing_statement = existing_statement.with_for_update()
        existing = (await self.session.execute(existing_statement)).scalar_one_or_none()
        if existing:
            from backend.services.billing_pricing import credits_to_units

            if (
                existing.delta_units != credits_to_units(credits)
                or existing.user_id != source.user_id
            ):
                raise PaymentError(
                    "Conversion conflicts with previously approved amount"
                )
            return {
                "entitlement_id": source.id,
                "user_id": source.user_id,
                "status": "already_converted",
                "transaction_id": existing.id,
            }
        if (
            source.converted_at
            or source.revoked_at
            or (source.expires_at and source.expires_at <= now_utc())
        ):
            raise PaymentError(
                "Legacy source is converted, revoked, or legally expired"
            )
        if Decimal(str(source.snapshot.get("credit_grant", "0"))) != 0:
            raise PaymentError(
                "Source already granted Credits; no legacy conversion is due"
            )
        order = (
            await self.session.get(Order, source.order_id) if source.order_id else None
        )
        reliable_subscription = (
            source.snapshot.get("source") == "persisted_subscription_snapshot"
        )
        if not reliable_subscription and (
            not order
            or order.user_id != source.user_id
            or not order.plan_snapshot
            or order.status != "fulfilled"
        ):
            raise PaymentError("Legacy conversion has no reliable purchased source")
        before = {
            "pr": source.pr_remaining,
            "issue": source.issue_remaining,
            "agent": source.agent_remaining,
        }
        report = {
            "entitlement_id": source.id,
            "user_id": source.user_id,
            "order_id": source.order_id,
            "source_key": source.source_key,
            "credits": str(credits),
            "remaining_before": before,
            "evidence": evidence,
            "actor_id": actor_id,
            "status": "dry_run" if dry_run else "converted",
        }
        if dry_run:
            return report
        # Wallet-before-source lock order matches request admission. The source
        # and unique financial conversion key are rechecked under the lock.
        billing = BillingService(self.session)
        await billing.get_wallet(source.user_id)
        source = (
            await self.session.execute(
                select(LegacyEntitlement)
                .where(LegacyEntitlement.id == entitlement_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        existing = (
            await self.session.execute(
                select(BillingTransaction)
                .where(BillingTransaction.idempotency_key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing:
            from backend.services.billing_pricing import credits_to_units

            if (
                existing.delta_units != credits_to_units(credits)
                or existing.user_id != source.user_id
            ):
                raise PaymentError(
                    "Conversion conflicts with previously approved amount"
                )
            return {
                "entitlement_id": source.id,
                "user_id": source.user_id,
                "status": "already_converted",
                "transaction_id": existing.id,
            }
        if (
            source.converted_at
            or source.revoked_at
            or (source.expires_at and source.expires_at <= now_utc())
        ):
            raise PaymentError("Legacy source changed; review conversion again")
        report["remaining_before"] = {
            "pr": source.pr_remaining,
            "issue": source.issue_remaining,
            "agent": source.agent_remaining,
        }
        transaction = await billing.grant(
            source.user_id,
            credits,
            key,
            kind="migration",
            order_id=source.order_id,
            actor_id=actor_id,
            reason="Reviewed source-linked legacy conversion",
            snapshot=report,
        )
        source.pr_remaining = source.issue_remaining = source.agent_remaining = 0
        source.converted_at = now_utc()
        source.conversion_transaction_id = transaction.id
        self.session.add(
            LegacyEntitlementEvent(
                entitlement_id=source.id,
                event_key=key,
                kind="conversion",
                actor_id=actor_id,
                reason=evidence,
                detail={**report, "transaction_id": transaction.id},
            )
        )
        await self.session.flush()
        return {**report, "transaction_id": transaction.id}

    async def prepare_code_snapshot(
        self,
        code_id: int,
        snapshot: dict,
        *,
        actor_id: int,
        evidence: str,
        dry_run: bool = True,
    ) -> dict:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            raise PaymentError(
                "Historical code preparation requires a reviewed purchased snapshot and evidence"
            )
        if not dry_run:
            actor = await self.session.get(TelegramUser, actor_id)
            if not actor or not actor.is_active or actor.role != "super_admin":
                raise PaymentError(
                    "Code snapshot preparation requires an active super-admin"
                )
        code = await self.session.get(RedeemCode, code_id, with_for_update=not dry_run)
        if not code or snapshot.get("id") != code.plan_id:
            raise PaymentError("Historical code snapshot source mismatch")
        required = {"name", "plan_type", "price_cents", "currency", "credit_grant"}
        if (
            not required.issubset(snapshot)
            or isinstance(snapshot["price_cents"], bool)
            or not isinstance(snapshot["price_cents"], int)
            or snapshot["price_cents"] < 0
        ):
            raise PaymentError("Historical code snapshot is incomplete")
        if (
            not isinstance(snapshot["currency"], str)
            or not snapshot["currency"]
            or len(snapshot["currency"]) > 10
        ):
            raise PaymentError("Historical code currency is invalid")
        PaymentService._validate_billing_plan(
            snapshot["credit_grant"],
            snapshot.get("rate_limits"),
            snapshot.get("concurrency_limit"),
        )
        if snapshot["plan_type"] not in {"one_time", "subscription"} or (
            snapshot["plan_type"] == "subscription"
            and (
                not isinstance(snapshot.get("duration_days"), int)
                or snapshot["duration_days"] <= 0
            )
        ):
            raise PaymentError("Historical code snapshot has no confirmed valid period")
        quota_keys = PaymentService(self.session)._plan_quota_values(Plan()).keys()
        normalized = {
            "version": 2,
            "rate_limits": {},
            "concurrency_limit": None,
            "duration_days": None,
            **{key: 0 for key in quota_keys},
            **snapshot,
        }
        normalized["credit_grant"] = str(Decimal(str(normalized["credit_grant"])))
        if code.plan_snapshot:
            if code.plan_snapshot != normalized:
                raise PaymentError(
                    "Code already has a different immutable offer snapshot"
                )
            return {"code_id": code.id, "status": "already_prepared"}
        if any(
            isinstance(normalized[key], bool)
            or not isinstance(normalized[key], int)
            or normalized[key] < 0
            for key in quota_keys
        ):
            raise PaymentError(
                "Historical code allowances must be nonnegative integers"
            )
        report = {
            "code_id": code.id,
            "status": "dry_run" if dry_run else "prepared",
            "snapshot": normalized,
            "evidence": evidence,
        }
        if dry_run:
            return report
        code.plan_snapshot = normalized
        self.session.add(
            RedeemCodeSnapshotAudit(
                redeem_code_id=code.id,
                actor_id=actor_id,
                snapshot=normalized,
                evidence=evidence,
            )
        )
        await self.session.flush()
        return report

    async def repair(self, entry: dict, *, actor_id: int, dry_run: bool = True) -> dict:
        if not dry_run:
            actor = await self.session.get(TelegramUser, actor_id)
            if not actor or not actor.is_active or actor.role != "super_admin":
                raise PaymentError(
                    "Applying historical billing repairs requires an active super-admin"
                )
        order = await self.session.get(
            Order, entry["order_id"], with_for_update=not dry_run
        )
        if (
            not order
            or order.status != "fulfilled"
            or order.user_id != entry["user_id"]
        ):
            raise PaymentError("Migration source order ownership/status mismatch")
        source_key = f"migration:order:{order.id}"
        existing = (
            await self.session.execute(
                select(LegacyEntitlement).where(LegacyEntitlement.order_id == order.id)
            )
        ).scalar_one_or_none()
        if existing and Decimal(str(entry.get("conversion_credits", "0"))) > 0:
            return await self.convert_source(
                existing.id,
                entry["conversion_credits"],
                actor_id=actor_id,
                evidence=entry.get("evidence", ""),
                dry_run=dry_run,
            )
        if existing:
            return {
                "order_id": order.id,
                "status": "already_migrated",
                "source_key": existing.source_key,
            }
        if not entry.get("evidence") or not isinstance(entry.get("snapshot"), dict):
            raise PaymentError(
                "Repair requires reviewed source evidence and purchased snapshot"
            )
        snapshot = entry["snapshot"]
        PaymentService._validate_billing_plan(
            snapshot.get("credit_grant", "0"),
            snapshot.get("rate_limits"),
            snapshot.get("concurrency_limit"),
        )
        remaining = entry.get("remaining", {})
        corrections = entry.get("inflated_daily", {})
        for prefix in ("pr", "issue", "agent"):
            if (
                isinstance(remaining.get(prefix, 0), bool)
                or not isinstance(remaining.get(prefix, 0), int)
                or remaining.get(prefix, 0) < 0
            ):
                raise PaymentError("Invalid preserved remaining allowance")
            if (
                isinstance(corrections.get(prefix, 0), bool)
                or not isinstance(corrections.get(prefix, 0), int)
                or corrections.get(prefix, 0) < 0
            ):
                raise PaymentError("Invalid audited daily correction")
        if snapshot.get("id") != order.plan_id:
            raise PaymentError("Purchased snapshot plan does not match source order")
        for prefix in ("pr", "issue", "agent"):
            original = snapshot.get(f"{prefix}_quota_bonus", 0)
            if (
                remaining.get(prefix, 0) > original
                or corrections.get(prefix, 0) > original
            ):
                raise PaymentError(
                    "Repair exceeds evidenced purchased one-time allowance"
                )
        periodic_fields = {
            f"{prefix}_{period}": f"{user_prefix}{period}_quota"
            for prefix, user_prefix in (
                ("pr", ""),
                ("issue", "issue_"),
                ("agent", "agent_"),
            )
            for period in ("daily", "weekly", "monthly")
        }
        applied_periodic = entry.get("applied_periodic", {})
        if not isinstance(applied_periodic, dict) or set(applied_periodic) - set(
            periodic_fields
        ):
            raise PaymentError("Invalid audited applied periodic allowance mapping")
        for key in periodic_fields:
            original = snapshot.get(f"{key}_add", 0)
            applied = applied_periodic.get(key, 0)
            if (
                isinstance(original, bool)
                or not isinstance(original, int)
                or original < 0
                or isinstance(applied, bool)
                or not isinstance(applied, int)
                or applied < 0
                or applied > original
            ):
                raise PaymentError(
                    "Audited applied periodic allowance exceeds source evidence"
                )
            if original and key not in applied_periodic:
                raise PaymentError(
                    "Historical periodic allowance needs an explicit audited applied amount"
                )
        expires_at = (
            parse_rfc3339(entry["expires_at"]) if entry.get("expires_at") else None
        )
        if snapshot.get("plan_type") == "subscription" and expires_at is None:
            raise PaymentError(
                "Historical subscription repair requires its evidenced expiry"
            )
        conversion = entry.get("conversion_credits", "0")
        PaymentService._validate_billing_plan(conversion, None, None)
        if Decimal(str(conversion)) > 0 and any(remaining.values()):
            raise PaymentError(
                "Converted purchased allowances must not also remain spendable"
            )
        user = await self.session.get(
            TelegramUser,
            order.user_id,
            with_for_update=not dry_run,
            populate_existing=not dry_run,
        )
        fields = {
            "pr": "daily_quota",
            "issue": "issue_daily_quota",
            "agent": "agent_daily_quota",
        }
        before = {prefix: getattr(user, field) for prefix, field in fields.items()}
        base_before = {
            key: getattr(user, field) for key, field in periodic_fields.items()
        }
        deductions = {
            key: applied_periodic.get(key, 0)
            + (
                corrections.get(key.split("_", 1)[0], 0)
                if key.endswith("_daily")
                else 0
            )
            for key in periodic_fields
        }
        if any(deductions[key] > base_before[key] for key in periodic_fields):
            raise PaymentError(
                "Correction conflicts with existing baseline; manual reconciliation required"
            )
        base_after = {
            key: value - deductions[key] for key, value in base_before.items()
        }
        report = {
            "order_id": order.id,
            "user_id": user.id,
            "source_key": source_key,
            "status": "dry_run" if dry_run else "repaired",
            "before": before,
            "after": {prefix: base_after[f"{prefix}_daily"] for prefix in fields},
            "base_before": base_before,
            "base_after": base_after,
            "applied_periodic": dict(applied_periodic),
            "remaining": remaining,
            "conversion_credits": str(conversion),
            "evidence": entry["evidence"],
        }
        if dry_run:
            return report
        # The owning order row is locked; its unique entitlement order_id and
        # append-only event key make retry or racing migration transactional.
        preserved = dict(snapshot)
        for prefix in fields:
            preserved[f"{prefix}_quota_bonus"] = remaining.get(prefix, 0)
        entitlement = await LegacyEntitlementService(self.session).grant(
            user.id,
            preserved,
            source_key,
            order_id=order.id,
            actor_id=actor_id,
            expires_at=expires_at,
        )
        for key, field in periodic_fields.items():
            setattr(user, field, base_after[key])
        order.plan_snapshot = snapshot
        self.session.add(
            LegacyEntitlementEvent(
                entitlement_id=entitlement.id,
                event_key=f"audit:{source_key}",
                kind="migration",
                actor_id=actor_id,
                reason="Reviewed historical source repair",
                detail=report,
            )
        )
        if Decimal(str(conversion)) > 0:
            await self.convert_source(
                entitlement.id,
                conversion,
                actor_id=actor_id,
                evidence=entry["evidence"],
                dry_run=False,
            )
        await self.session.flush()
        return report
