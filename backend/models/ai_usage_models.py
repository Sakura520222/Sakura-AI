"""Provider-reported AI usage ledger.

The ledger is deliberately independent from PR/Issue/Agent business rows so
auxiliary model calls are accounted for as well.  It stores counters and
non-sensitive routing identifiers only; prompts, responses, endpoints, and
credentials never belong here.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    String,
)

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class AIUsageRecord(Base):
    """One idempotent usage record for a successful logical AI call."""

    __tablename__ = "ai_usage_records"
    __table_args__ = (
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_ai_usage_input_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_ai_usage_output_nonnegative",
        ),
        CheckConstraint(
            "cached_input_tokens IS NULL OR cached_input_tokens >= 0",
            name="ck_ai_usage_cached_input_nonnegative",
        ),
        CheckConstraint(
            "cache_creation_tokens IS NULL OR cache_creation_tokens >= 0",
            name="ck_ai_usage_cache_creation_nonnegative",
        ),
        CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0",
            name="ck_ai_usage_reasoning_nonnegative",
        ),
        Index("ix_ai_usage_occurred_at", "occurred_at"),
        Index("ix_ai_usage_call_kind_occurred", "call_kind", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_key = Column(String(191), nullable=False, unique=True)
    call_kind = Column(String(32), nullable=False)
    role = Column(String(64), nullable=False)
    provider_id = Column(String(128), nullable=False)
    model_id = Column(String(255), nullable=False)
    protocol_family = Column(String(64), nullable=False)

    # Cached and reasoning counters are dimensions of input/output usage.  They
    # are retained for diagnostics but MUST NOT be added to input/output again.
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    cached_input_tokens = Column(BigInteger, nullable=True)
    cache_creation_tokens = Column(BigInteger, nullable=True)
    reasoning_tokens = Column(BigInteger, nullable=True)
    usage_reported = Column(Boolean, nullable=False, default=False)

    occurred_at = Column(UTCDateTime, nullable=False, default=utc_now)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


__all__ = ["AIUsageRecord"]
