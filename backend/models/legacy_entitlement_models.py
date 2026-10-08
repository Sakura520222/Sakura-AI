"""Source-linked legacy allowances; never treated as a Credits balance."""

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class LegacyEntitlement(Base):
    __tablename__ = "billing_legacy_entitlements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("telegram_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        Integer,
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    source_key = Column(String(191), nullable=False, unique=True)
    snapshot = Column(JSON, nullable=False)
    pr_remaining = Column(Integer, default=0, nullable=False)
    issue_remaining = Column(Integer, default=0, nullable=False)
    agent_remaining = Column(Integer, default=0, nullable=False)
    rate_limits = Column(JSON, nullable=False, default=dict)
    starts_at = Column(UTCDateTime, nullable=False, default=utc_now)
    expires_at = Column(UTCDateTime, nullable=True)
    revoked_at = Column(UTCDateTime, nullable=True)
    converted_at = Column(UTCDateTime, nullable=True)
    conversion_transaction_id = Column(Integer, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "pr_remaining >= 0 AND issue_remaining >= 0 AND agent_remaining >= 0",
            name="ck_legacy_remaining_nonnegative",
        ),
    )


class LegacyEntitlementEvent(Base):
    __tablename__ = "billing_legacy_entitlement_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entitlement_id = Column(
        Integer,
        ForeignKey("billing_legacy_entitlements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_key = Column(String(191), nullable=False, unique=True)
    kind = Column(String(32), nullable=False)
    feature = Column(String(32), nullable=True)
    units = Column(Integer, nullable=False, default=0)
    actor_id = Column(Integer, nullable=True)
    reason = Column(String(512), nullable=True)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class RedeemCodeRedemption(Base):
    __tablename__ = "payment_code_redemptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    redeem_code_id = Column(
        Integer,
        ForeignKey("payment_redeem_codes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False
    )
    order_id = Column(
        Integer,
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("redeem_code_id", "user_id", name="uq_redeem_code_user"),
    )


@event.listens_for(LegacyEntitlementEvent, "before_update")
@event.listens_for(LegacyEntitlementEvent, "before_delete")
def _immutable_entitlement_events(*_args):
    raise ValueError("Legacy entitlement events are append-only")


class PaymentReceipt(Base):
    """A verified upstream payment may fund only one order."""

    __tablename__ = "payment_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(50), nullable=False)
    event_id = Column(String(255), nullable=False)
    order_id = Column(
        Integer,
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_receipt_event"),
    )


class PaymentRefundAttempt(Base):
    """Durable upstream refund intent, separate from its financial outcome."""

    __tablename__ = "payment_refund_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer,
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    active_order_id = Column(Integer, nullable=True, unique=True)
    idempotency_key = Column(String(191), nullable=False, unique=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False)
    gateway_amount_cents = Column(Integer, nullable=True)
    gateway_currency = Column(String(10), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    credit_transaction_id = Column(Integer, nullable=True)
    credit_hold_units = Column(BigInteger, nullable=False, default=0)
    provider_refund_id = Column(String(255), nullable=True)
    actor_id = Column(Integer, nullable=True)
    evidence = Column(JSON, nullable=False, default=dict)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    updated_at = Column(UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now)


class PaymentRefundAttemptEvent(Base):
    __tablename__ = "payment_refund_attempt_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey("payment_refund_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_key = Column(String(191), nullable=False, unique=True)
    status = Column(String(32), nullable=False)
    actor_id = Column(Integer, nullable=True)
    evidence = Column(JSON, nullable=False, default=dict)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


@event.listens_for(PaymentRefundAttemptEvent, "before_update")
@event.listens_for(PaymentRefundAttemptEvent, "before_delete")
def _immutable_refund_events(*_args):
    raise ValueError("Refund attempt events are append-only")


class RateLimitAdmission(Base):
    """An admitted business execution consumes rate headroom only once."""

    __tablename__ = "billing_rate_limit_admissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(191), nullable=False, unique=True)
    user_id = Column(
        Integer,
        ForeignKey("telegram_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    feature = Column(String(32), nullable=False)
    source_kind = Column(String(32), nullable=False)
    entitlement_id = Column(
        Integer,
        ForeignKey("billing_legacy_entitlements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repo_name = Column(String(255), nullable=False)
    number = Column(Integer, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


@event.listens_for(RateLimitAdmission, "before_update")
@event.listens_for(RateLimitAdmission, "before_delete")
def _immutable_rate_admissions(*_args):
    raise ValueError("Rate-limit admissions are append-only")


class RedeemCodeSnapshotAudit(Base):
    __tablename__ = "payment_code_snapshot_audits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    redeem_code_id = Column(
        Integer,
        ForeignKey("payment_redeem_codes.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    actor_id = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)
    evidence = Column(String(1024), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


@event.listens_for(RedeemCodeSnapshotAudit, "before_update")
@event.listens_for(RedeemCodeSnapshotAudit, "before_delete")
def _immutable_code_snapshot_audit(*_args):
    raise ValueError("Redeem code snapshot audits are append-only")


class PaymentRefundInboxEvent(Base):
    """Verified incoming refund delivery; unresolved evidence stays durable."""

    __tablename__ = "payment_refund_inbox_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(50), nullable=False)
    event_key = Column(String(191), nullable=False)
    status = Column(String(32), nullable=False, default="pending_reconciliation")
    pending_reason = Column(String(128), nullable=True)
    order_id = Column(Integer, nullable=True, index=True)
    evidence = Column(JSON, nullable=False)
    actor_id = Column(Integer, nullable=True)
    received_at = Column(UTCDateTime, nullable=False, default=utc_now)
    resolved_at = Column(UTCDateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_refund_inbox_delivery"),
    )


class PaymentRefundReference(Base):
    """One upstream refund identity has one cash/credit effect."""

    __tablename__ = "payment_refund_references"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(50), nullable=False)
    reference_id = Column(String(191), nullable=False)
    order_id = Column(Integer, nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False)
    ledger_transaction_id = Column(Integer, nullable=True)
    inbox_event_id = Column(
        Integer,
        ForeignKey("payment_refund_inbox_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("provider", "reference_id", name="uq_refund_reference"),
    )


class PaymentRefundInboxAudit(Base):
    __tablename__ = "payment_refund_inbox_audits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    inbox_event_id = Column(
        Integer,
        ForeignKey("payment_refund_inbox_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_key = Column(String(191), nullable=False, unique=True)
    status = Column(String(32), nullable=False)
    actor_id = Column(Integer, nullable=True)
    evidence = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


@event.listens_for(PaymentRefundReference, "before_update")
@event.listens_for(PaymentRefundReference, "before_delete")
@event.listens_for(PaymentRefundInboxAudit, "before_update")
@event.listens_for(PaymentRefundInboxAudit, "before_delete")
def _immutable_refund_inbox_financial_records(*_args):
    raise ValueError("Refund references and audit events are append-only")
