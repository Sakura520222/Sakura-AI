"""Announcements, read markers, and provider delivery state."""

from __future__ import annotations

import enum

from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class AnnouncementType(str, enum.Enum):
    GENERAL = "general"
    IMPORTANT = "important"
    FEATURE = "feature"
    MAINTENANCE = "maintenance"
    RELEASE = "release"


class AnnouncementStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    announcement_type = Column("type", String(50), default=AnnouncementType.GENERAL.value, nullable=False)
    status = Column(String(50), default=AnnouncementStatus.DRAFT.value, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    published_at = Column(UTCDateTime, nullable=True, index=True)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    creator = relationship("TelegramUser", foreign_keys=[created_by])
    reads = relationship("AnnouncementRead", cascade="all, delete-orphan", back_populates="announcement")
    deliveries = relationship("NotificationDelivery", cascade="all, delete-orphan", back_populates="announcement")


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False)
    read_at = Column(UTCDateTime, default=utc_now, nullable=False)

    announcement = relationship("Announcement", back_populates="reads")
    user = relationship("TelegramUser", foreign_keys=[user_id])
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", name="uq_announcement_read"),)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), nullable=False)
    status = Column(String(50), default=DeliveryStatus.PENDING.value, nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    sent_at = Column(UTCDateTime, nullable=True)
    next_retry_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    announcement = relationship("Announcement", back_populates="deliveries")
    user = relationship("TelegramUser", foreign_keys=[user_id])
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", "channel", name="uq_notification_delivery"),)

