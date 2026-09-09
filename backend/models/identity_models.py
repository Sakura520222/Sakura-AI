"""Internal identities and notification endpoint models.

The legacy ``telegram_users`` table remains the internal user store for now.
These tables provide a stable boundary around third-party identities and
delivery addresses without changing legacy primary keys or foreign keys.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class AuthProvider(str, enum.Enum):
    """Supported authentication providers."""

    GITHUB = "github"
    PASSKEY = "passkey"


class NotificationProvider(str, enum.Enum):
    """Supported notification providers."""

    WEB = "web"
    EMAIL = "email"
    TELEGRAM = "telegram"


class UserIdentity(Base):
    """An external login identity linked to an internal user id."""

    __tablename__ = "user_identities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    provider = Column(String(50), nullable=False, index=True)
    # Provider identifiers are opaque strings (GitHub ids are currently
    # numeric, while OIDC providers may use UUIDs or subject strings).
    provider_user_id = Column(String(255), nullable=False)
    provider_username = Column(String(255), nullable=True, index=True)
    metadata_json = Column("metadata", Text, nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("TelegramUser", foreign_keys=[user_id])

    __table_args__ = (
        # A provider identity can belong to exactly one internal user.  This
        # is the key invariant that prevents duplicate accounts when a GitHub
        # username changes.
        UniqueConstraint(
            "provider", "provider_user_id", name="uq_user_identity_provider_id"
        ),
    )


class NotificationEndpoint(Base):
    """A user-owned address used by a notification provider."""

    __tablename__ = "notification_endpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    provider = Column(String(50), nullable=False, index=True)
    address = Column(String(320), nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    metadata_json = Column("metadata", Text, nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("TelegramUser", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint(
            "provider", "address", name="uq_notification_endpoint_provider_address"
        ),
    )
