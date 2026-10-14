"""Durable, verified payment/refund inbox; receiving never repeats upstream I/O."""

import hashlib
import json

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.core.config import get_dynamic_config
from backend.core.log_sanitizer import sanitize_exception_text
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
        logger.bind(
            provider=provider, payment_event_id=record.id, status=record.status
        ).info("Verified payment receipt persisted")
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
                    if not await self._resolve_refund_pending(record):
                        await self._refund(record)
                        await self._supersede_refund_pending(record)
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
                logger.bind(
                    provider=record.provider,
                    payment_event_id=record.id,
                    order_id=record.order_id,
                ).info("Payment receipt effects staged")
        except (PaymentError, BillingError, ValueError) as exc:
            # Receipt remains committed even when the financial savepoint fails.
            record = await self.session.get(PaymentRefundInboxEvent, event_id)
            record.status = "pending_reconciliation"
            record.pending_reason = (
                getattr(exc, "code", None) or "invalid_provider_evidence"
            )
            await self.session.flush()
            logger.bind(
                provider=record.provider,
                payment_event_id=record.id,
                pending_reason=record.pending_reason,
            ).opt(exception=True).warning(
                "Payment receipt requires reconciliation: {}",
                sanitize_exception_text(exc),
            )
        return record

    @staticmethod
    def _metadata_reference(metadata_json: str | None) -> str | None:
        """Read only a validated object; unrelated historical text may be corrupt."""
        try:
            metadata = json.loads(metadata_json or "{}")
        except ValueError, TypeError:
            return None
        if not isinstance(metadata, dict):
            return None
        reference = metadata.get("payment_reference_id")
        return reference if isinstance(reference, str) and reference else None

    async def _reference_order_id(self, provider: str, reference: str) -> int | None:
        # metadata_json is legacy TEXT, not SQL JSON. Keyset batches keep memory
        # bounded without casting corrupt rows or assuming a database dialect.
        # Examine all candidates to reject ambiguous ownership before any debit.
        upper_id = (
            await self.session.execute(
                select(func.max(Order.id)).where(Order.payment_provider == provider)
            )
        ).scalar_one()
        if upper_id is None:
            return None
        last_id, match_id = 0, None
        while True:
            rows = (
                await self.session.execute(
                    select(Order.id, Order.provider_tx_id, Order.metadata_json)
                    .where(
                        Order.payment_provider == provider,
                        Order.id > last_id,
                        Order.id <= upper_id,
                    )
                    .order_by(Order.id)
                    .limit(200)
                )
            ).all()
            if not rows:
                return match_id
            for row in rows:
                if (
                    row.provider_tx_id == reference
                    or self._metadata_reference(row.metadata_json) == reference
                ):
                    if match_id is not None:
                        raise PaymentError(
                            "Incoming payment ownership is ambiguous",
                            code="ambiguous_order",
                        )
                    match_id = row.id
            last_id = rows[-1].id

    async def _order(self, record: PaymentRefundInboxEvent) -> Order:
        evidence = record.evidence
        reference = None
        if evidence.get("reviewed_order_id"):
            statement = select(Order).where(Order.id == evidence["reviewed_order_id"])
        elif evidence.get("order_no"):
            statement = select(Order).where(Order.order_no == evidence["order_no"])
        else:
            reference = evidence.get("payment_reference_id") or evidence.get(
                "provider_tx_id"
            )
            if not isinstance(reference, str) or not reference:
                raise PaymentError(
                    "Incoming payment ownership needs review", code="unknown_order"
                )
            order_id = await self._reference_order_id(record.provider, reference)
            statement = select(Order).where(Order.id == order_id)
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
        if reference and not (
            order.provider_tx_id == reference
            or self._metadata_reference(order.metadata_json) == reference
        ):
            # Candidate identity may have changed while waiting for the lock.
            raise PaymentError(
                "Incoming payment reference differs from its order",
                code="payment_reference_mismatch",
            )
        try:
            metadata = json.loads(order.metadata_json or "{}")
        except ValueError, TypeError:
            raise PaymentError(
                "Order metadata needs review", code="order_metadata_required"
            ) from None
        if not isinstance(metadata, dict):
            raise PaymentError(
                "Order metadata needs review", code="order_metadata_required"
            )
        native_reference = evidence.get("payment_reference_id")
        if native_reference:
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
            await self.session.flush()
            if record.provider == "stripe":
                await self._replay_stripe_refunds(order, skip_locked=True)

    async def replay_stripe_refunds_after_commit(self, event_id: int) -> None:
        """Drain busy early receipts after the caller commits payment effects.

        Commit each replay before taking another inbox lock. This phase owns
        its transactions and must run with no uncommitted caller work.
        """
        record = await self.session.get(
            PaymentRefundInboxEvent, event_id, populate_existing=True
        )
        if (
            record is None
            or record.provider != "stripe"
            or record.status != "processed"
            or record.evidence.get("type") != WebhookEventType.PAYMENT_COMPLETED.value
            or not record.evidence.get("payment_reference_id")
        ):
            return
        order = await self.session.get(Order, record.order_id, populate_existing=True)
        if (
            order is None
            or order.payment_provider != "stripe"
            or self._metadata_reference(order.metadata_json)
            != record.evidence["payment_reference_id"]
        ):
            return
        await self._replay_stripe_refunds(order, skip_locked=False, commit_each=True)

    @staticmethod
    def _stripe_refund_replay_query(
        order_id, order_no, reference, after_id, upper_id, *, skip_locked, event_id=None
    ):
        evidence = PaymentRefundInboxEvent.evidence
        query = (
            select(PaymentRefundInboxEvent)
            .where(
                PaymentRefundInboxEvent.provider == "stripe",
                PaymentRefundInboxEvent.status == "pending_reconciliation",
                evidence["type"].as_string() == WebhookEventType.PAYMENT_REFUNDED.value,
                or_(
                    evidence["payment_reference_id"].as_string() == reference,
                    and_(
                        or_(
                            evidence["payment_reference_id"].as_string().is_(None),
                            evidence["payment_reference_id"].as_string() == "",
                        ),
                        evidence["provider_tx_id"].as_string() == reference,
                    ),
                ),
                or_(
                    PaymentRefundInboxEvent.order_id.is_(None),
                    PaymentRefundInboxEvent.order_id == order_id,
                ),
                or_(
                    evidence["reviewed_order_id"].as_integer().is_(None),
                    evidence["reviewed_order_id"].as_integer() == order_id,
                ),
                or_(
                    evidence["order_no"].as_string().is_(None),
                    evidence["order_no"].as_string() == "",
                    evidence["order_no"].as_string() == order_no,
                ),
                PaymentRefundInboxEvent.id > after_id,
                PaymentRefundInboxEvent.id <= upper_id,
            )
            .order_by(PaymentRefundInboxEvent.id)
            .limit(200)
            .execution_options(populate_existing=True)
        )
        if event_id is not None:
            # Lock by primary key, not by a JSON-filtered scan that can acquire
            # unrelated receipt locks under MySQL repeatable-read isolation.
            query = (
                query.where(PaymentRefundInboxEvent.id == event_id)
                .limit(1)
                .with_for_update(skip_locked=skip_locked)
            )
        return query

    async def _replay_stripe_refunds(self, order, *, skip_locked, commit_each=False):
        reference = self._metadata_reference(order.metadata_json)
        if not reference:
            return
        order_id, order_no = order.id, order.order_no
        upper_id = (
            await self.session.execute(select(func.max(PaymentRefundInboxEvent.id)))
        ).scalar() or 0
        after_id = 0
        replayed = 0
        while after_id < upper_id:
            # Payment holds order/wallet locks, so it must skip busy inbox rows.
            # After commit, lock one receipt at a time before its order/wallet.
            query = self._stripe_refund_replay_query(
                order_id,
                order_no,
                reference,
                after_id,
                upper_id,
                skip_locked=skip_locked,
            )
            if commit_each:
                query = query.limit(1)
            candidates = (await self.session.execute(query)).scalars().all()
            if not candidates:
                break
            candidate_ids = [candidate.id for candidate in candidates]
            for candidate_id in candidate_ids:
                current = (
                    await self.session.execute(
                        self._stripe_refund_replay_query(
                            order_id,
                            order_no,
                            reference,
                            after_id,
                            upper_id,
                            skip_locked=skip_locked,
                            event_id=candidate_id,
                        )
                    )
                ).scalar_one_or_none()
                if current is not None:
                    await self.replay(candidate_id)
                    replayed += 1
                if commit_each:
                    await self.session.commit()
            after_id = candidate_ids[-1]
        if replayed:
            logger.bind(order_id=order_id, replay_count=replayed).info(
                "Stripe refund receipt replays {}",
                "committed" if commit_each else "staged",
            )

    @staticmethod
    def _paddle_refund_identity(evidence, statuses):
        """Only match a complete native adjustment identity, never guessed money."""
        if (
            evidence.get("type") != WebhookEventType.PAYMENT_REFUNDED.value
            or evidence.get("normalization_error")
            or (evidence.get("wire_evidence") or {}).get("event_type")
            not in {"adjustment.created", "adjustment.updated"}
        ):
            return None
        items = evidence.get("refund_items") or []
        source = evidence.get("payment_reference_id")
        if len(items) != 1 or not isinstance(source, str) or not source:
            return None
        item = items[0]
        reference, amount, currency = (
            item.get("id"),
            item.get("amount_cents"),
            item.get("currency"),
        )
        if (
            not isinstance(reference, str)
            or not reference
            or reference != evidence.get("provider_tx_id")
            or item.get("status") not in statuses
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or not isinstance(currency, str)
            or not currency
        ):
            return None
        return reference, source, amount, currency.upper()

    def _refund_lifecycle_identities(self, record, *, pending):
        evidence = record.evidence
        if record.provider == "paddle":
            identity = self._paddle_refund_identity(
                evidence, {"pending_approval"} if pending else {"succeeded", "rejected"}
            )
            return [identity] if identity else []
        if record.provider != "stripe":
            return []
        native_type = (evidence.get("wire_evidence") or {}).get("event_type")
        single_refund = native_type in {
            "refund.created",
            "refund.updated",
            "refund.failed",
            "charge.refund.updated",
        }
        if (
            evidence.get("type") != WebhookEventType.PAYMENT_REFUNDED.value
            or evidence.get("normalization_error")
            or not (single_refund or (not pending and native_type == "charge.refunded"))
        ):
            return []
        source = evidence.get("payment_reference_id")
        items = evidence.get("refund_items") or []
        if not isinstance(source, str) or not source or not items:
            return []
        if single_refund and (
            len(items) != 1 or items[0].get("id") != evidence.get("provider_tx_id")
        ):
            return []
        statuses = (
            {"pending", "requires_action"}
            if pending
            else {"succeeded", "failed", "canceled"}
        )
        identities = []
        for item in items:
            reference, amount, currency = (
                item.get("id"),
                item.get("amount_cents"),
                item.get("currency"),
            )
            if (
                not isinstance(reference, str)
                or not reference
                or len(reference) > 191
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount <= 0
                or not isinstance(currency, str)
                or not currency
            ):
                return []
            if item.get("status") in statuses:
                identities.append((reference, source, amount, currency.upper()))
        return identities

    def _refund_lifecycle_query(self, record, identity, *, pending):
        statement = (
            select(PaymentRefundInboxEvent)
            .where(
                PaymentRefundInboxEvent.provider == record.provider,
                PaymentRefundInboxEvent.id != record.id,
                PaymentRefundInboxEvent.status
                == ("pending_reconciliation" if pending else "processed"),
                PaymentRefundInboxEvent.evidence["payment_reference_id"].as_string()
                == identity[1],
            )
            .execution_options(populate_existing=True)
        )
        # Stripe charge receipts can contain multiple native refunds. Their
        # exact IDs, amounts and currencies are checked after this current read.
        if pending or record.provider == "paddle":
            statement = statement.where(
                PaymentRefundInboxEvent.evidence["provider_tx_id"].as_string()
                == identity[0]
            )
        return statement

    def _append_refund_superseded(self, pending, terminal, identity):
        # The projection closes, but the native pending observation and every
        # received/reviewed audit remain intact for source reconstruction.
        self.session.add(
            PaymentRefundInboxAudit(
                inbox_event_id=pending.id,
                event_key=f"superseded:{pending.id}:{terminal.id}",
                status="superseded",
                actor_id=terminal.actor_id,
                evidence={
                    "terminal_event_id": terminal.id,
                    "refund_reference_id": identity[0],
                    "payment_reference_id": identity[1],
                    "order_id": terminal.order_id,
                },
            )
        )
        logger.bind(
            provider=pending.provider,
            payment_event_id=pending.id,
            terminal_event_id=terminal.id,
            order_id=terminal.order_id,
        ).info("Refund receipt superseded by verified terminal evidence")

    async def _resolve_refund_pending(self, record):
        identities = self._refund_lifecycle_identities(record, pending=True)
        if not identities:
            return False
        identity = identities[0]
        # Wait for the order lock before reading terminal observations. A refund
        # already executing in another worker can then commit its terminal event.
        order = await self._order(record)
        terminals = (
            (
                await self.session.execute(
                    self._refund_lifecycle_query(
                        record, identity, pending=False
                    ).with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for terminal in terminals:
            if (
                identity in self._refund_lifecycle_identities(terminal, pending=False)
                and terminal.order_id == order.id
            ):
                self._append_refund_superseded(record, terminal, identity)
                return True
        # A locked terminal event is deliberately skipped: retain this receipt
        # as pending and let the existing replay/reconciliation entry resolve it.
        # The locking read also bypasses an earlier MySQL repeatable-read snapshot.
        return False

    async def _supersede_refund_pending(self, record):
        identities = self._refund_lifecycle_identities(record, pending=False)
        if not identities:
            return
        terminal_order = await self.session.get(Order, record.order_id)
        if terminal_order is None:
            return
        for identity in identities:
            pending_rows = (
                (
                    await self.session.execute(
                        self._refund_lifecycle_query(
                            record, identity, pending=True
                        ).with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for pending in pending_rows:
                if identity not in self._refund_lifecycle_identities(
                    pending, pending=True
                ):
                    continue
                if pending.evidence.get("reviewed_order_id") not in {
                    None,
                    terminal_order.id,
                } or pending.evidence.get("order_no") not in {
                    None,
                    "",
                    terminal_order.order_no,
                }:
                    continue  # Do not lock another order for conflicting ownership.
                try:
                    order = await self._order(pending)
                except PaymentError:
                    continue  # Conflicting ownership remains explicitly pending.
                if order.id != record.order_id:
                    continue
                pending.status = "processed"
                pending.pending_reason = None
                pending.resolved_at = now_utc()
                self._append_refund_superseded(pending, record, identity)
        # An in-flight pending replay is skipped to avoid event/order lock
        # inversion; after it gets the order lock it resolves against this event.

    async def _alipay_refund_attempts(self, order_id, reference, request_no):
        identities = [
            PaymentRefundAttempt.provider_refund_id == reference,
            PaymentRefundAttempt.evidence["refund_request"]["reference_id"].as_string()
            == reference,
        ]
        if request_no is not None:
            identities.extend(
                [
                    PaymentRefundAttempt.idempotency_key == request_no,
                    PaymentRefundAttempt.evidence["refund_request"][
                        "out_request_no"
                    ].as_string()
                    == request_no,
                ]
            )
        return (
            (
                await self.session.execute(
                    select(PaymentRefundAttempt)
                    .where(
                        PaymentRefundAttempt.order_id == order_id,
                        or_(*identities),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )

    async def _alipay_refund_evidence(self, record):
        """A cumulative notification needs one exact audited native request."""
        if record.provider != "alipay":
            return
        from backend.services.payment.alipay_gateway import AlipayGateway

        evidence = record.evidence
        wire = evidence.get("wire_evidence") or {}
        if evidence.get("refund_evidence_complete"):
            order = await self._order(record)
            await BillingService(self.session).get_wallet(order.user_id)
            service = PaymentService(self.session)
            items = []
            for item in evidence.get("refund_items") or []:
                raw = item.get("id")
                request_no = (
                    wire.get("out_request_no")
                    if AlipayGateway.is_refund_reference(raw)
                    else raw
                )
                try:
                    reference = (
                        raw
                        if AlipayGateway.is_refund_reference(raw)
                        else AlipayGateway.refund_reference_id(
                            order.provider_tx_id, raw
                        )
                    )
                except ValueError as exc:
                    raise PaymentError(
                        sanitize_exception_text(exc), code="refund_evidence_incomplete"
                    ) from exc
                if raw == order.provider_tx_id and wire.get("out_request_no") != raw:
                    raise PaymentError(
                        "An Alipay payment ID cannot identify a reviewed refund",
                        code="refund_evidence_incomplete",
                    )
                attempts = await self._alipay_refund_attempts(
                    order.id, reference, request_no
                )
                if len(attempts) > 1:
                    raise PaymentError(
                        "Alipay native refund request is ambiguous",
                        code="refund_reference_conflict",
                    )
                if attempts:
                    attempt = attempts[0]
                    native = await service._alipay_refund_identity(
                        attempt, order, request_no, verified_native=True
                    )
                    if (
                        native != reference
                        or attempt.gateway_amount_cents != item.get("amount_cents")
                        or str(attempt.gateway_currency).upper()
                        != str(item.get("currency") or evidence.get("currency")).upper()
                    ):
                        raise PaymentError(
                            "Alipay refund differs from its audited request",
                            code="refund_intent_mismatch",
                        )
                    service._bind_refund_identity(
                        attempt, reference, actor_id=record.actor_id
                    )
                else:
                    await service._assert_alipay_reference_mapping(order)
                    if request_no is None:
                        known_reference = (
                            await self.session.execute(
                                select(PaymentRefundReference.id)
                                .where(
                                    PaymentRefundReference.provider == "alipay",
                                    PaymentRefundReference.reference_id == reference,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        legacy_attempt = (
                            await self.session.execute(
                                select(PaymentRefundAttempt.id)
                                .where(
                                    PaymentRefundAttempt.order_id == order.id,
                                    PaymentRefundAttempt.evidence["refund_request"][
                                        "reference_id"
                                    ]
                                    .as_string()
                                    .is_(None),
                                    or_(
                                        PaymentRefundAttempt.provider_refund_id.is_(
                                            None
                                        ),
                                        PaymentRefundAttempt.provider_refund_id == "",
                                        PaymentRefundAttempt.provider_refund_id
                                        == order.provider_tx_id,
                                    ),
                                    PaymentRefundAttempt.status != "failed",
                                )
                                .with_for_update()
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if known_reference is None and legacy_attempt is not None:
                            raise PaymentError(
                                "Legacy Alipay refunds require the audited out_request_no",
                                code="refund_evidence_incomplete",
                            )
                items.append({**item, "id": reference})
            record.evidence = {**evidence, "refund_items": items}
            return
        request_no = wire.get("out_request_no")
        trade_no = wire.get("trade_no")
        try:
            reference = AlipayGateway.refund_reference_id(trade_no, request_no)
        except ValueError as exc:
            raise PaymentError(
                sanitize_exception_text(exc), code="refund_evidence_incomplete"
            ) from exc
        order = await self._order(record)
        await BillingService(self.session).get_wallet(order.user_id)
        if trade_no != order.provider_tx_id:
            raise PaymentError(
                "Alipay refund trade differs from the order",
                code="refund_reference_conflict",
            )
        metadata = json.loads(order.metadata_json or "{}")
        cumulative = evidence.get("refund_total_cents")
        prior_cash = metadata.get("refunded_gateway_amount_cents", 0)
        if (
            not isinstance(cumulative, int)
            or isinstance(cumulative, bool)
            or cumulative <= 0
        ):
            raise PaymentError(
                "Alipay refund total needs provider review",
                code="refund_evidence_incomplete",
            )
        attempts = await self._alipay_refund_attempts(order.id, reference, request_no)
        service = PaymentService(self.session)
        if len(attempts) == 1:
            attempt = attempts[0]
            native = await service._alipay_refund_identity(
                attempt, order, request_no, verified_native=True
            )
            amount = attempt.gateway_amount_cents
            if (
                native != reference
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount <= 0
                or str(attempt.gateway_currency).upper() != "CNY"
                or attempt.status
                not in {"pending", "unknown", "upstream_succeeded", "succeeded"}
                or cumulative < amount
                or (attempt.status == "succeeded" and cumulative > prior_cash)
                or (attempt.status != "succeeded" and cumulative != prior_cash + amount)
            ):
                raise PaymentError(
                    "Alipay cumulative refund differs from its audited request",
                    code="refund_intent_mismatch",
                )
            service._bind_refund_identity(attempt, reference, actor_id=record.actor_id)
        elif not attempts:
            await service._assert_alipay_reference_mapping(order)
            prior = (
                await self.session.execute(
                    select(PaymentRefundReference)
                    .where(
                        PaymentRefundReference.provider == "alipay",
                        PaymentRefundReference.reference_id == reference,
                        PaymentRefundReference.order_id == order.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                prior is None
                or cumulative < prior.amount_cents
                or cumulative > prior_cash
            ):
                raise PaymentError(
                    "Alipay cumulative refund lacks request-specific amount evidence",
                    code="refund_evidence_incomplete",
                )
            amount = prior.amount_cents
        else:
            raise PaymentError(
                "Alipay native refund request is ambiguous",
                code="refund_reference_conflict",
            )
        record.evidence = {
            **evidence,
            "refund_evidence_complete": True,
            "refund_items": [
                {
                    "id": reference,
                    "amount_cents": amount,
                    "currency": "CNY",
                    "status": "succeeded",
                }
            ],
        }

    async def _refund(self, record):
        await self._alipay_refund_evidence(record)
        evidence = record.evidence
        denied = evidence.get("refund_items") or []
        if denied and all(
            item.get("status") in {"failed", "canceled", "rejected"} for item in denied
        ):
            order = await self._order(record)
            metadata = json.loads(order.metadata_json or "{}")
            gateway_total = metadata.get("gateway_amount_cents")
            gateway_currency = metadata.get("gateway_currency")
            if (
                not isinstance(gateway_total, int)
                or isinstance(gateway_total, bool)
                or gateway_total <= 0
                or not gateway_currency
            ):
                raise PaymentError(
                    "Historical checkout amount/currency needs audit",
                    code="checkout_snapshot_required",
                )
            billing = BillingService(self.session)
            await billing.get_wallet(order.user_id)
            for item in denied:
                amount = item.get("amount_cents")
                currency = str(
                    item.get("currency") or evidence.get("currency") or ""
                ).upper()
                if currency != str(gateway_currency).upper():
                    raise PaymentError(
                        "Refund currency differs from checkout",
                        code="refund_currency_mismatch",
                    )
                if (
                    not isinstance(amount, int)
                    or isinstance(amount, bool)
                    or amount <= 0
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                ):
                    raise PaymentError(
                        "Refund identity/amount/outcome needs review",
                        code="refund_evidence_incomplete",
                    )
                if amount > gateway_total:
                    raise PaymentError(
                        "Refund exceeds original payment", code="refund_overflow"
                    )
                confirmed = (
                    await self.session.execute(
                        select(PaymentRefundReference)
                        .where(
                            PaymentRefundReference.provider == record.provider,
                            PaymentRefundReference.reference_id == item.get("id"),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if confirmed:
                    raise PaymentError(
                        "Successful refund conflicts with later failed evidence",
                        code="refund_outcome_conflict",
                    )
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
                if (
                    attempt.gateway_amount_cents != amount
                    or str(attempt.gateway_currency).upper() != currency
                ):
                    raise PaymentError(
                        "Refund outcome differs from staged intent",
                        code="refund_intent_mismatch",
                    )
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
        # A complete cash refund can contain several native partial refunds.
        # Validate the entire bundle and subtract already-applied identities
        # before deciding whether this receipt requires partial-refund policy.
        incoming = {}
        for item in items:
            identity, amount = item.get("id"), item.get("amount_cents")
            currency = str(item.get("currency") or evidence["currency"]).upper()
            if (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 191
                or type(amount) is not int
                or amount <= 0
                or item.get("status") != "succeeded"
            ):
                raise PaymentError(
                    "Refund identity/amount/outcome needs review",
                    code="refund_evidence_incomplete",
                )
            if currency != str(gateway_currency).upper():
                raise PaymentError(
                    "Refund currency differs from checkout",
                    code="refund_currency_mismatch",
                )
            value = (amount, currency)
            if identity in incoming and incoming[identity] != value:
                raise PaymentError(
                    "Refund identity conflicts within receipt",
                    code="refund_reference_conflict",
                )
            incoming[identity] = value
        applied = (
            (
                await self.session.execute(
                    select(PaymentRefundReference)
                    .where(
                        PaymentRefundReference.provider == record.provider,
                        PaymentRefundReference.reference_id.in_(incoming),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for previous in applied:
            if (previous.order_id, previous.amount_cents, previous.currency) != (
                order.id,
                *incoming[previous.reference_id],
            ):
                raise PaymentError(
                    "Refund reference conflicts with prior financial effect",
                    code="refund_reference_conflict",
                )
        applied_ids = {previous.reference_id for previous in applied}
        original_cash = metadata.get("refunded_gateway_amount_cents", 0)
        if type(original_cash) is not int or original_cash < 0:
            raise PaymentError(
                "Refund cash provenance needs review", code="refund_credit_mismatch"
            )
        final_cash = original_cash + sum(
            amount
            for identity, (amount, _) in incoming.items()
            if identity not in applied_ids
        )
        if final_cash > gateway_total:
            raise PaymentError(
                "Refund exceeds original payment", code="refund_overflow"
            )
        full_receipt = final_cash == gateway_total
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
                    and not active_attempt.provider_refund_id
                    # Alipay request identities are resolved from exact evidence
                    # above; another refund's amount cannot establish that request.
                    and record.provider != "alipay"
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
                if (
                    not full_receipt
                    and cumulative != gateway_total
                    and await get_dynamic_config("payment_partial_refund_policy")
                    != "proportional_unused_credits"
                ):
                    raise PaymentError(
                        "Partial refund policy requires operator reconciliation",
                        code="partial_refund_unsupported",
                    )
                prior_units = 0
                if purchase:
                    if (
                        not full_receipt
                        and cumulative != gateway_total
                        and not PaymentService(self.session)._is_credits_only_snapshot(
                            order.plan_snapshot
                        )
                    ):
                        raise PaymentError(
                            "Partial refunds require a Credits-only purchased snapshot",
                            code="partial_refund_unsupported",
                        )
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
                elif not full_receipt and cumulative != gateway_total:
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
                    await PaymentService(self.session)._reconcile_refunded_subscription(
                        order
                    )
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
