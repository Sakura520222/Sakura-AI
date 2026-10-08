"""Short database leases for execution capacity shared by all workers."""

from sqlalchemy import BigInteger, Column, Index, Integer, String

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class ServiceExecutionGate(Base):
    """A feature row is updated to serialize admission on every SQL dialect."""

    __tablename__ = "service_execution_gates"

    feature = Column(String(32), primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0)


class ServiceExecutionLease(Base):
    """A running slot, never a billing reservation or a purchased entitlement."""

    __tablename__ = "service_execution_leases"
    __table_args__ = (
        Index("ix_service_execution_feature_expiry", "feature", "expires_at"),
    )

    token = Column(String(32), primary_key=True)
    feature = Column(String(32), nullable=False)
    owner_id = Column(String(80), nullable=False)
    operation_id = Column(String(128), nullable=True)
    user_id = Column(Integer, nullable=True)
    acquired_at = Column(UTCDateTime, nullable=False, default=utc_now)
    expires_at = Column(UTCDateTime, nullable=False)


class ServiceExecutionOwnership(Base):
    """Worker liveness across waiting and running, independent of slot count.

    Each worker has its own fenced token. Duplicate delivery cannot overwrite
    another worker's owner, and any live owner prevents abandoned-work recovery.
    A stopped token remains inspectable but can never be renewed after expiry.
    """

    __tablename__ = "service_execution_ownerships"
    __table_args__ = (
        Index("ix_service_execution_owner_operation", "operation_id", "expires_at"),
        Index("ix_service_execution_owner_feature_expiry", "feature", "expires_at"),
    )

    token = Column(String(32), primary_key=True)
    operation_id = Column(String(128), nullable=False)
    feature = Column(String(32), nullable=False)
    owner_id = Column(String(80), nullable=False)
    user_id = Column(Integer, nullable=True)
    state = Column(String(16), nullable=False, default="queued")
    acquired_at = Column(UTCDateTime, nullable=False, default=utc_now)
    renewed_at = Column(UTCDateTime, nullable=False, default=utc_now)
    expires_at = Column(UTCDateTime, nullable=False)
