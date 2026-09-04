"""Announcements, read markers, and provider delivery state.

The current delivery rows intentionally retain their historical unique key
(``announcement_id``, ``user_id``, ``channel``).  A publication version makes
those rows reusable for a new send round while the archive models preserve the
content and delivery state of the previous round.
"""

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
    # Additive publication identity.  Existing installations receive the
    # value through the automatic ``ADD COLUMN ... DEFAULT 1`` migration.
    publication_version = Column(Integer, default=1, nullable=False, index=True)

    creator = relationship("TelegramUser", foreign_keys=[created_by])
    reads = relationship("AnnouncementRead", cascade="all, delete-orphan", back_populates="announcement")
    deliveries = relationship("NotificationDelivery", cascade="all, delete-orphan", back_populates="announcement")
    publication_history = relationship(
        "AnnouncementPublicationHistory",
        cascade="all, delete-orphan",
        back_populates="announcement",
        order_by="AnnouncementPublicationHistory.publication_version",
    )


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
    publication_version = Column(Integer, default=1, nullable=False, index=True)

    announcement = relationship("Announcement", back_populates="deliveries")
    user = relationship("TelegramUser", foreign_keys=[user_id])
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", "channel", name="uq_notification_delivery"),)


class AnnouncementPublicationHistory(Base):
    """Immutable content snapshot and archived state for one send round.

    A row is created when a publication version starts, before any delivery
    worker can run.  ``archived_at`` and the child rows are filled when that
    version is withdrawn or superseded.  This ordering is important when an
    administrator edits a withdrawn announcement: the historical content is
    still the content that was actually published, not the later draft text.
    """

    __tablename__ = "announcement_publication_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    announcement_id = Column(
        Integer,
        ForeignKey("announcements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    publication_version = Column(Integer, nullable=False)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    announcement_type = Column("type", String(50), nullable=False)
    published_at = Column(UTCDateTime, nullable=True)
    archived_at = Column(UTCDateTime, nullable=True)
    # A compact JSON mirror makes the archived result inspectable without
    # loading the child rows.  The normalized child rows remain authoritative.
    delivery_states = Column(Text, nullable=False, default="[]")
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)

    announcement = relationship("Announcement", back_populates="publication_history")
    deliveries = relationship(
        "AnnouncementDeliveryHistory",
        cascade="all, delete-orphan",
        back_populates="publication",
        order_by="AnnouncementDeliveryHistory.id",
    )
    __table_args__ = (
        UniqueConstraint(
            "announcement_id",
            "publication_version",
            name="uq_announcement_publication_history_version",
        ),
    )


class AnnouncementDeliveryHistory(Base):
    """Frozen delivery outcome belonging to an archived publication round."""

    __tablename__ = "announcement_delivery_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    publication_id = Column(
        Integer,
        ForeignKey("announcement_publication_history.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Keep the identity even if the current delivery row is later removed;
    # nullable + SET NULL also permits deleting an old user safely.
    user_id = Column(
        Integer,
        ForeignKey("telegram_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    error_message = Column(Text, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    sent_at = Column(UTCDateTime, nullable=True)
    next_retry_at = Column(UTCDateTime, nullable=True)
    source_delivery_id = Column(
        Integer,
        ForeignKey("notification_deliveries.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)

    publication = relationship(
        "AnnouncementPublicationHistory", back_populates="deliveries"
    )
    user = relationship("TelegramUser", foreign_keys=[user_id])


# Friendly aliases used by integrations that call a publication round an
# archive.  Keep the canonical class names above for SQLAlchemy metadata.
AnnouncementPublication = AnnouncementPublicationHistory
AnnouncementDeliveryArchive = AnnouncementDeliveryHistory
