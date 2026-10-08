"""Credits ledger and recoverable operations; amounts are integer microcredits."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class BillingWallet(Base):
    __tablename__ = "billing_wallets"
    __table_args__ = (
        CheckConstraint("reserved_units >= 0", name="ck_wallet_reserve_nonnegative"),
    )
    user_id = Column(Integer, ForeignKey("telegram_users.id"), primary_key=True)
    balance_units = Column(BigInteger, nullable=False, default=0)
    reserved_units = Column(BigInteger, nullable=False, default=0)
    version = Column(BigInteger, nullable=False, default=0)
    low_balance_threshold_units = Column(BigInteger, nullable=False, default=0)
    low_balance_notified = Column(Boolean, nullable=False, default=False)
    updated_at = Column(UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now)


class BillingTransaction(Base):
    __tablename__ = "billing_transactions"
    __table_args__ = (Index("ix_billing_user_created", "user_id", "created_at"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id"), nullable=False)
    operation_id = Column(String(128), nullable=True, index=True)
    kind = Column(String(32), nullable=False)
    delta_units = Column(BigInteger, nullable=False)
    idempotency_key = Column(String(191), nullable=False, unique=True)
    order_id = Column(Integer, nullable=True, index=True)
    reference_transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), nullable=True
    )
    actor_id = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    snapshot = Column(JSON, nullable=False, default=dict)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class BillingCreditLot(Base):
    """Derived source balance; reconstructable from immutable allocations."""

    __tablename__ = "billing_credit_lots"
    __table_args__ = (
        CheckConstraint("remaining_units >= 0", name="ck_credit_lot_nonnegative"),
    )
    transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), primary_key=True
    )
    user_id = Column(Integer, nullable=False, index=True)
    remaining_units = Column(BigInteger, nullable=False)


class BillingCreditHold(Base):
    """Refund quarantine; source Credits cannot be spent during upstream I/O."""

    __tablename__ = "billing_credit_holds"
    idempotency_key = Column(String(191), primary_key=True)
    transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), nullable=False
    )
    user_id = Column(Integer, nullable=False, index=True)
    units = Column(BigInteger, nullable=False)
    state = Column(String(32), nullable=False, default="active")
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    released_at = Column(UTCDateTime, nullable=True)


class BillingCreditDebt(Base):
    __tablename__ = "billing_credit_debts"
    transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), primary_key=True
    )
    user_id = Column(Integer, nullable=False, index=True)
    outstanding_units = Column(BigInteger, nullable=False)


class BillingDebtFunding(Base):
    __tablename__ = "billing_debt_funding"
    debt_transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), primary_key=True
    )
    funding_transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), primary_key=True
    )
    remaining_units = Column(BigInteger, nullable=False)


class BillingOperation(Base):
    __tablename__ = "billing_operations"
    __table_args__ = (Index("ix_billing_op_user_status", "user_id", "status"),)
    operation_id = Column(String(128), primary_key=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    feature = Column(String(32), nullable=False)
    source = Column(JSON, nullable=False, default=dict)
    platform_reason = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="not_started")
    outcome = Column(String(32), nullable=True)
    charging_enabled = Column(Boolean, nullable=False, default=False)
    charge_failed = Column(Boolean, nullable=False, default=False)
    charge_failed_calls = Column(Boolean, nullable=False, default=False)
    reserve_units = Column(BigInteger, nullable=False, default=0)
    reservation_seq = Column(Integer, nullable=False, default=0)
    settled_units = Column(BigInteger, nullable=False, default=0)
    known_credits = Column(String(256), nullable=False, default="0")
    pending_reason = Column(String(128), nullable=True)
    expires_at = Column(UTCDateTime, nullable=True, index=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    updated_at = Column(UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now)


class BillingPriceProfile(Base):
    __tablename__ = "billing_price_profiles"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "model_id",
            "call_kind",
            "version",
            name="uq_billing_price_version",
        ),
        Index(
            "uq_billing_price_scope_version",
            "scope_key",
            "provider_id",
            "model_id",
            "call_kind",
            "version",
            unique=True,
        ),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(String(128), nullable=False)
    # The provider remains the actual upstream, while account identity selects
    # a tariff. A non-NULL discriminator also constrains legacy/provider scopes
    # on databases where nullable UNIQUE columns allow repeated NULL values.
    account_id = Column(String(128), nullable=True)
    scope_key = Column(
        String(136), nullable=False, default="provider", server_default="provider"
    )
    model_id = Column(String(255), nullable=False)
    call_kind = Column(String(32), nullable=False)
    version = Column(Integer, nullable=False)
    config = Column(JSON, nullable=False)
    actor_id = Column(Integer, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class BillingCallAttempt(Base):
    __tablename__ = "billing_call_attempts"
    call_id = Column(String(191), primary_key=True)
    operation_id = Column(
        String(128),
        ForeignKey("billing_operations.operation_id"),
        nullable=False,
        index=True,
    )
    logical_call_id = Column(String(191), nullable=True)
    provider_id = Column(String(128), nullable=False)
    account_id = Column(String(128), nullable=True)
    model_id = Column(String(255), nullable=False)
    call_kind = Column(String(32), nullable=False)
    protocol_family = Column(String(64), nullable=True)
    state = Column(String(32), nullable=False, default="started")
    usage_record_key = Column(String(191), nullable=True)
    price_profile_id = Column(
        Integer, ForeignKey("billing_price_profiles.id"), nullable=True
    )
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    updated_at = Column(UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now)


class BillingUsageCharge(Base):
    """Frozen exact quote; policy decides whether it becomes a user debit."""

    __tablename__ = "billing_usage_charges"
    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_id = Column(String(128), nullable=False, index=True)
    usage_record_key = Column(String(191), nullable=False, unique=True)
    price_profile_id = Column(
        Integer, ForeignKey("billing_price_profiles.id"), nullable=False
    )
    provider_cost = Column(String(256), nullable=False)
    provider_currency = Column(String(10), nullable=False)
    settlement_amount = Column(String(256), nullable=False)
    settlement_currency = Column(String(10), nullable=False)
    credits = Column(String(256), nullable=False)
    snapshot = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class BillingReservationEvent(Base):
    __tablename__ = "billing_reservation_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    operation_id = Column(String(128), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    delta_units = Column(BigInteger, nullable=False)
    idempotency_key = Column(String(191), nullable=False, unique=True)
    snapshot = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class BillingNotice(Base):
    __tablename__ = "billing_notices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(32), nullable=False, default="low_balance")
    transaction_id = Column(
        Integer, ForeignKey("billing_transactions.id"), nullable=True, unique=True
    )
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    read_at = Column(UTCDateTime, nullable=True)


class BillingReconciliationEvent(Base):
    __tablename__ = "billing_reconciliation_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String(191), nullable=False, index=True)
    event_key = Column(String(191), nullable=False, unique=True)
    actor_id = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


def _reject_mutation(mapper, connection, target):
    raise ValueError(f"{type(target).__name__} is append-only")


for _model in (
    BillingTransaction,
    BillingPriceProfile,
    BillingUsageCharge,
    BillingReservationEvent,
    BillingReconciliationEvent,
):
    event.listen(_model, "before_update", _reject_mutation)
    event.listen(_model, "before_delete", _reject_mutation)
