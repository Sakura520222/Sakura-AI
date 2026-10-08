"""Durable admission receipts for signed GitHub business deliveries."""

from sqlalchemy import JSON, Column, String, UniqueConstraint

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class WebhookExecutionReceipt(Base):
    __tablename__ = "webhook_execution_receipts"
    __table_args__ = (
        UniqueConstraint(
            "feature", "delivery_id", name="uq_webhook_execution_delivery"
        ),
    )

    receipt_id = Column(String(64), primary_key=True)
    feature = Column(String(32), nullable=False)
    delivery_id = Column(String(191), nullable=False)
    payload_digest = Column(String(64), nullable=False)
    source = Column(JSON, nullable=False)
    operation_id = Column(String(128), nullable=False, index=True)
    owner_token = Column(String(36), nullable=False)
    status = Column(String(32), nullable=False, default="processing", index=True)
    response = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    updated_at = Column(UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now)
