"""Channel-independent outbox, per-user inbox, routing and deployment budgets."""
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime, UTCMicrosecondDateTime


class NotificationEvent(Base):
    __tablename__ = "notification_events"

    id = Column(String(36), primary_key=True)
    dedup_key = Column(String(64), nullable=False, unique=True)
    event_type = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(255), nullable=False)
    repo_name = Column(String(255), nullable=True, index=True)
    run_key = Column(String(255), nullable=False)
    severity = Column(String(20), nullable=False, default="info")
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    detail_url = Column(String(1000), nullable=True)
    recipients_json = Column(Text, nullable=False, default="[]")
    audience = Column(String(30), nullable=False, default="users")
    sensitive = Column(Boolean, nullable=False, default=False)
    occurred_at = Column(UTCDateTime, nullable=False, default=utc_now)
    status = Column(String(30), nullable=False, default="pending", index=True)
    error_category = Column(String(50), nullable=True)
    next_retry_at = Column(UTCDateTime, nullable=True, index=True)
    claim_token = Column(String(128), nullable=True)
    claim_until = Column(UTCDateTime, nullable=True)


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True)
    read_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_user_notification"),)


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(100), nullable=False, default="*")
    repo_name = Column(String(255), nullable=True)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(String(255), nullable=True)
    min_severity = Column(String(20), nullable=False, default="info")
    endpoint_id = Column(Integer, ForeignKey("notification_endpoints.id", ondelete="CASCADE"), nullable=True)
    # NULL endpoint denotes the user's WebUI inbox, never every external endpoint.
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)


class NotificationProviderBudget(Base):
    __tablename__ = "notification_provider_budgets"

    channel = Column(String(50), primary_key=True)
    next_available_at = Column(UTCMicrosecondDateTime, nullable=False, default=utc_now)


class NotificationAudit(Base):
    __tablename__ = "notification_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False)
    target_id = Column(String(100), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=utc_now)
