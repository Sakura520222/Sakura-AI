"""Durable, verified payment/refund inbox; receiving never repeats upstream I/O."""

import hashlib
import json

from sqlalchemy import JSON, or_, select, type_coerce
from sqlalchemy.exc import IntegrityError

from backend.core.time_service import now_utc
from backend.models.billing_models import BillingTransaction
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    PaymentRefundAttempt,
    PaymentRefundInboxAudit,
    PaymentRefundInboxEvent,
    PaymentRefundReference,
)
from backend.models.payment_models import Order
from backend.models.telegram_models import TelegramUser
from backend.services.billing_pricing import proportional_units
from backend.services.billing_service import BillingError, BillingService
from backend.services.legacy_entitlement_service import LegacyEntitlementService
from backend.services.payment.currency_units import currency_minor_exponent
from backend.services.payment.gateway_base import WebhookEventType
from backend.services.payment_service import PaymentError, PaymentService


class PaymentEventService:
    def __init__(self, session):
        self.session = session

    async def accept(
        self, provider: str, event, *, payload_hash: str
    ) -> PaymentRefundInboxEvent:
        """Persist verified minimal evidence BEFORE any financial handling."""
        native = getattr(event, "event_id", "")
        key = native if isinstance(native, str) and native else payload_hash
        if not key or len(key) > 191:
            key = hashlib.sha256(str(key).encode()).hexdigest()
        evidence = {
            "type": event.event_type.value,
            "order_no": event.order_no,
            "provider_tx_id": event.provider_tx_id,
            "payment_reference_id": event.payment_reference_id,
            "amount_cents": event.amount_cents,
            "currency": event.currency,
            "refund_items": event.refund_items,
            "refund_total_cents": event.refund_total_cents,
            "original_amount_cents": event.original_amount_cents,
            "refund_evidence_complete": event.refund_evidence_complete,
            "normalization_error": event.normalization_error,
            "wire_evidence": event.wire_evidence,
        }
        record = (
            await self.session.execute(
                select(PaymentRefundInboxEvent)
                .where(
                    PaymentRefundInboxEvent.provider == provider,
                    PaymentRefundInboxEvent.event_key == key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            try:
                async with self.session.begin_nested():
                    record = PaymentRefundInboxEvent(
                        provider=provider,
                        event_key=key,
                        evidence=evidence,
                        status="pending_reconciliation",
                    )
                    self.session.add(record)
                    await self.session.flush()
                    self.session.add(
                        PaymentRefundInboxAudit(
                            inbox_event_id=record.id,
                            event_key=f"received:{record.id}",
                            status="received",
                            evidence=evidence,
                        )
                    )
                    await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                record = (
                    await self.session.execute(
                        select(PaymentRefundInboxEvent)
                        .where(
                            PaymentRefundInboxEvent.provider == provider,
                            PaymentRefundInboxEvent.event_key == key,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
        received = (
            await self.session.execute(
                select(PaymentRefundInboxAudit)
                .where(
                    PaymentRefundInboxAudit.inbox_event_id == record.id,
                    PaymentRefundInboxAudit.event_key == f"received:{record.id}",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        # Reviewed working evidence can change, while a provider delivery's
        # original verified evidence stays immutable in its received audit.
        if received is None or received.evidence != evidence:
            raise PaymentError("Provider event identity conflicts with prior evidence")
        await self.session.commit()
        if record.status == "processed":
            return record
        return await self.replay(record.id)

    async def replay(self, event_id: int) -> PaymentRefundInboxEvent:
        record = await self.session.get(
            PaymentRefundInboxEvent,
            event_id,
            with_for_update=True,
            populate_existing=True,
        )
        if record is None:
            raise PaymentError("Payment inbox event not found")
        if record.status == "processed":
            return record
        try:
            async with self.session.begin_nested():
                if record.evidence.get("normalization_error"):
                    raise PaymentError(
                        "Verified provider amount/currency needs review",
                        code=record.evidence["normalization_error"],
                    )
                if record.evidence["type"] == WebhookEventType.PAYMENT_COMPLETED.value:
                    await self._payment(record)
                elif record.evidence["type"] == WebhookEventType.PAYMENT_REFUNDED.value:
                    await self._refund(record)
                else:
                    raise PaymentError(
                        "Unsupported incoming payment event", code="unsupported_event"
                    )
                record.status = "processed"
                record.pending_reason = None
                record.resolved_at = now_utc()
                self.session.add(
                    PaymentRefundInboxAudit(
                        inbox_event_id=record.id,
                        event_key=f"processed:{record.id}",
                        status="processed",
                        actor_id=record.actor_id,
                        evidence={"order_id": record.order_id},
                    )
                )
                await self.session.flush()
        except (PaymentError, BillingError, ValueError) as exc:
            # Receipt remains committed even when the financial savepoint fails.
            record = await self.session.get(PaymentRefundInboxEvent, event_id)
            record.status = "pending_reconciliation"
            record.pending_reason = getattr(exc, "code", "invalid_provider_evidence")
            await self.session.flush()
        return record

    async def _order(self, record: PaymentRefundInboxEvent) -> Order:
        evidence = record.evidence
        if evidence.get("reviewed_order_id"):
            statement = select(Order).where(Order.id == evidence["reviewed_order_id"])
        elif evidence.get("order_no"):
            statement = select(Order).where(Order.order_no == evidence["order_no"])
        else:
            reference = evidence.get("payment_reference_id") or evidence.get(
                "provider_tx_id"
            )
            if not reference:
                raise PaymentError(
                    "Incoming payment ownership needs review", code="unknown_order"
                )
            statement = select(Order).where(
                Order.payment_provider == record.provider,
                or_(
                    Order.provider_tx_id == reference,
                    type_coerce(Order.metadata_json, JSON)[
                        "payment_reference_id"
                    ].as_string()
                    == reference,
                ),
            )
        order = (
            await self.session.execute(
                statement.with_for_update().execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            raise PaymentError(
                "Incoming payment ownership needs review", code="unknown_order"
            )
        if order.payment_provider != record.provider:
            raise PaymentError(
                "Incoming payment provider differs from order", code="provider_mismatch"
            )
        native_reference = evidence.get("payment_reference_id")
        if native_reference:
            metadata = json.loads(order.metadata_json or "{}")
            expected_reference = metadata.get("payment_reference_id")
            # Checkout ids and payment-intent ids are distinct in Stripe.
            # Other gateways use the transaction id as their payment reference.
            if not expected_reference and record.provider != "stripe":
                expected_reference = order.provider_tx_id
            if expected_reference and native_reference != expected_reference:
                raise PaymentError(
                    "Incoming payment reference differs from its order",
                    code="payment_reference_mismatch",
                )
        record.order_id = order.id
        return order

    async def _payment(self, record):
        order = await self._order(record)
        evidence = record.evidence
        confirmed = await PaymentService(self.session).confirm_payment(
            order.order_no,
            evidence["provider_tx_id"],
            evidence["amount_cents"],
            evidence["currency"],
        )
        if confirmed.status not in {"fulfilled", "refunded"}:
            raise PaymentError(
                "Paid event conflicts with terminal order state",
                code="payment_state_requires_review",
            )
        if evidence.get("payment_reference_id"):
            metadata = json.loads(order.metadata_json or "{}")
            metadata["payment_reference_id"] = evidence["payment_reference_id"]
            order.metadata_json = json.dumps(metadata)

    async def _refund(self, record):
        evidence = record.evidence
        denied = evidence.get("refund_items") or []
        if denied and all(
            item.get("status") in {"failed", "canceled", "rejected"} for item in denied
        ):
            order = await self._order(record)
            billing = BillingService(self.session)
            await billing.get_wallet(order.user_id)
            for item in denied:
                attempt = (
                    await self.session.execute(
                        select(PaymentRefundAttempt)
                        .where(
                            PaymentRefundAttempt.order_id == order.id,
                            PaymentRefundAttempt.provider_refund_id == item.get("id"),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if not attempt:
                    continue
                if attempt.status == "succeeded":
                    raise PaymentError(
                        "Successful refund conflicts with later failed evidence",
                        code="refund_outcome_conflict",
                    )
                if attempt.status != "failed":
                    if attempt.credit_hold_units:
                        await billing.release_purchase_refund(
                            attempt.credit_transaction_id,
                            attempt.credit_hold_units,
                            f"{attempt.idempotency_key}:hold",
                        )
                    attempt.status = "failed"
                    attempt.active_order_id = None
            await self.session.flush()
            return
        if not evidence.get("refund_evidence_complete") or not evidence.get(
            "refund_items"
        ):
            raise PaymentError(
                "Actual refund amount/status needs provider review",
                code="refund_evidence_incomplete",
            )
        order = await self._order(record)
        billing = BillingService(self.session)
        await billing.get_wallet(order.user_id)
        metadata = json.loads(order.metadata_json or "{}")
        if not order.plan_snapshot:
            raise PaymentError(
                "Purchased snapshot needs audit", code="snapshot_required"
            )
        gateway_total = metadata.get("gateway_amount_cents")
        gateway_currency = metadata.get("gateway_currency")
        if gateway_total is None or not gateway_currency:
            # Source-only legacy restore does not establish historical FX/wire units.
            raise PaymentError(
                "Historical checkout amount/currency needs audit",
                code="checkout_snapshot_required",
            )
        if (
            not isinstance(gateway_total, int)
            or isinstance(gateway_total, bool)
            or gateway_total <= 0
        ):
            raise PaymentError(
                "Invalid checkout snapshot", code="checkout_snapshot_required"
            )
        if (
            evidence.get("original_amount_cents") is not None
            and evidence["original_amount_cents"] != gateway_total
        ):
            raise PaymentError(
                "Provider original amount differs from checkout",
                code="checkout_amount_mismatch",
            )
        purchase = (
            await self.session.execute(
                select(BillingTransaction)
                .where(
                    BillingTransaction.idempotency_key == f"order:{order.id}:credits"
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not purchase:
            source = (
                await self.session.execute(
                    select(LegacyEntitlement)
                    .where(LegacyEntitlement.order_id == order.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if source and source.conversion_transaction_id:
                purchase = await self.session.get(
                    BillingTransaction, source.conversion_transaction_id
                )
        items = evidence["refund_items"]
        for item in items:
            refund_id = item.get("id")
            amount = item.get("amount_cents")
            if (
                not isinstance(refund_id, str)
                or not refund_id
                or len(refund_id) > 191
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount <= 0
                or item.get("status") != "succeeded"
            ):
                raise PaymentError(
                    "Refund identity/amount/outcome needs review",
                    code="refund_evidence_incomplete",
                )
            currency = str(item.get("currency") or evidence["currency"]).upper()
            if currency != str(gateway_currency).upper():
                raise PaymentError(
                    "Refund currency differs from checkout",
                    code="refund_currency_mismatch",
                )
            previous = (
                await self.session.execute(
                    select(PaymentRefundReference)
                    .where(
                        PaymentRefundReference.provider == record.provider,
                        PaymentRefundReference.reference_id == refund_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if previous:
                if (previous.order_id, previous.amount_cents, previous.currency) != (
                    order.id,
                    amount,
                    currency,
                ):
                    raise PaymentError(
                        "Refund reference conflicts with prior financial effect",
                        code="refund_reference_conflict",
                    )
                continue
            attempt = (
                await self.session.execute(
                    select(PaymentRefundAttempt)
                    .where(
                        PaymentRefundAttempt.order_id == order.id,
                        PaymentRefundAttempt.provider_refund_id == refund_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if attempt and attempt.status == "failed":
                attempt = None
            if attempt is None:
                active_attempt = (
                    await self.session.execute(
                        select(PaymentRefundAttempt)
                        .where(PaymentRefundAttempt.active_order_id == order.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    active_attempt
                    and active_attempt.gateway_amount_cents == amount
                    and str(active_attempt.gateway_currency).upper() == currency
                ):
                    attempt = active_attempt
                    attempt.provider_refund_id = refund_id
            ledger_id = None
            if attempt:
                if (
                    attempt.gateway_amount_cents != amount
                    or str(attempt.gateway_currency).upper() != currency
                ):
                    raise PaymentError(
                        "Refund outcome differs from staged intent",
                        code="refund_intent_mismatch",
                    )
                if attempt.status != "succeeded":
                    attempt.status = "upstream_succeeded"
                    await PaymentService(self.session)._finalize_refund(attempt, order)
                metadata = json.loads(order.metadata_json or "{}")
            else:
                # Cash totals are independent from a rounded display in order currency.
                prior_cash = metadata.get("refunded_gateway_amount_cents", 0)
                cumulative = prior_cash + amount
                if cumulative > gateway_total:
                    raise PaymentError(
                        "Refund exceeds original payment", code="refund_overflow"
                    )
                prior_units = 0
                if purchase:
                    reversals = (
                        (
                            await self.session.execute(
                                select(BillingTransaction.delta_units)
                                .where(
                                    BillingTransaction.reference_transaction_id
                                    == purchase.id
                                )
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                    prior_units = -sum(value for value in reversals if value < 0)
                    target_units = (
                        purchase.delta_units
                        if cumulative == gateway_total
                        else proportional_units(
                            purchase.delta_units, cumulative, gateway_total
                        )
                    )
                    units = target_units - prior_units
                    if units < 0:
                        raise PaymentError(
                            "Refund credit provenance needs reconciliation",
                            code="refund_credit_mismatch",
                        )
                    if units:
                        transaction = await billing.reverse_external_purchase(
                            purchase.id,
                            units,
                            f"external:{record.provider}:{hashlib.sha256(refund_id.encode()).hexdigest()}",
                            reason="Verified upstream refund",
                            snapshot={
                                "provider": record.provider,
                                "refund_id": refund_id,
                                "event_id": record.event_key,
                                "amount_cents": amount,
                                "currency": currency,
                                "original_amount_cents": gateway_total,
                            },
                        )
                        ledger_id = transaction.id
                elif cumulative != gateway_total:
                    raise PaymentError(
                        "Legacy partial entitlement refund requires review",
                        code="legacy_refund_policy_required",
                    )
                metadata["refunded_gateway_amount_cents"] = cumulative
                order.refunded_amount_cents = (
                    order.amount_cents
                    if cumulative == gateway_total
                    else proportional_units(
                        order.amount_cents, cumulative, gateway_total
                    )
                )
                if cumulative == gateway_total:
                    await LegacyEntitlementService(self.session).revoke_order(
                        order.id, reason="Verified upstream full refund"
                    )
                    order.status = "refunded"
            self.session.add(
                PaymentRefundReference(
                    provider=record.provider,
                    reference_id=refund_id,
                    order_id=order.id,
                    amount_cents=amount,
                    currency=currency,
                    ledger_transaction_id=ledger_id,
                    inbox_event_id=record.id,
                )
            )
            await self.session.flush()
        order.metadata_json = json.dumps(metadata)

    async def resolve(
        self,
        event_id: int,
        *,
        operator_id: int,
        evidence: str,
        order_id: int | None = None,
        checkout_amount_cents: int | None = None,
        checkout_currency: str | None = None,
        refund_reference_id: str | None = None,
        refund_amount_cents: int | None = None,
        refund_currency: str | None = None,
    ):
        actor = await self.session.get(TelegramUser, operator_id)
        if (
            not actor
            or not actor.is_active
            or actor.role != "super_admin"
            or not evidence.strip()
        ):
            raise PaymentError(
                "Inbox reconciliation requires a super-admin and source evidence"
            )
        record = await self.session.get(
            PaymentRefundInboxEvent, event_id, with_for_update=True
        )
        if record is None:
            raise PaymentError("Payment inbox event not found")
        if record.status == "processed":
            return record
        reviewed = dict(record.evidence)
        if order_id is not None:
            order = await self.session.get(Order, order_id, with_for_update=True)
            if order is None or order.payment_provider != record.provider:
                raise PaymentError("Reviewed order/provider mismatch")
            reviewed["reviewed_order_id"] = order_id
            if checkout_amount_cents is not None or checkout_currency is not None:
                if (
                    not isinstance(checkout_amount_cents, int)
                    or isinstance(checkout_amount_cents, bool)
                    or checkout_amount_cents <= 0
                    or not isinstance(checkout_currency, str)
                    or not checkout_currency
                ):
                    raise PaymentError(
                        "Reviewed checkout requires exact amount and currency"
                    )
                metadata = json.loads(order.metadata_json or "{}")
                try:
                    currency_minor_exponent(checkout_currency)
                except ValueError as exc:
                    raise PaymentError(
                        "Reviewed checkout currency unit is unknown"
                    ) from exc
                metadata.update(
                    gateway_amount_cents=checkout_amount_cents,
                    gateway_currency=checkout_currency.upper(),
                )
                order.metadata_json = json.dumps(metadata)
                if (
                    reviewed.get("normalization_error")
                    and reviewed["type"] == WebhookEventType.PAYMENT_COMPLETED.value
                ):
                    reviewed["amount_cents"] = checkout_amount_cents
                    reviewed["currency"] = checkout_currency.upper()
                    reviewed["normalization_error"] = ""
        if (
            refund_reference_id is not None
            or refund_amount_cents is not None
            or refund_currency is not None
        ):
            if (
                reviewed["type"] != WebhookEventType.PAYMENT_REFUNDED.value
                or not isinstance(refund_reference_id, str)
                or not refund_reference_id
                or len(refund_reference_id) > 191
                or not isinstance(refund_amount_cents, int)
                or isinstance(refund_amount_cents, bool)
                or refund_amount_cents <= 0
                or not isinstance(refund_currency, str)
                or not refund_currency
            ):
                raise PaymentError(
                    "Reviewed actual refund requires exact identity, amount and currency"
                )
            try:
                currency_minor_exponent(refund_currency)
            except ValueError as exc:
                raise PaymentError("Reviewed refund currency unit is unknown") from exc
            reviewed["refund_items"] = [
                {
                    "id": refund_reference_id,
                    "amount_cents": refund_amount_cents,
                    "currency": refund_currency.upper(),
                    "status": "succeeded",
                }
            ]
            reviewed["refund_evidence_complete"] = True
            reviewed["normalization_error"] = ""
        reviewed["operator_evidence"] = evidence
        record.evidence = reviewed
        record.actor_id = operator_id
        self.session.add(
            PaymentRefundInboxAudit(
                inbox_event_id=record.id,
                event_key=f"review:{record.id}:{now_utc().isoformat()}",
                status="reviewed",
                actor_id=operator_id,
                evidence=reviewed,
            )
        )
        await self.session.flush()
        return await self.replay(event_id)

    async def list_pending(self, limit: int = 100, *, offset: int = 0):
        return (
            (
                await self.session.execute(
                    select(PaymentRefundInboxEvent)
                    .where(PaymentRefundInboxEvent.status != "processed")
                    .order_by(PaymentRefundInboxEvent.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
