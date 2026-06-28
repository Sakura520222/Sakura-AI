"""Activity conversation models — mirror AgentTeam Session/Message/ToolCall
for PR review, Issue analysis, and Repo scan tasks.

Reuses the exact same schema pattern so the frontend component
(agent_team_live_view_fragment.html) can render them identically.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
    TIMESTAMP,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship

from backend.models.database import Base
from backend.models.database import utc_now


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ActivitySession(Base):
    """Conversation session for PR/Issue/Scan tasks."""

    __tablename__ = "activity_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(
        String(20), nullable=False, index=True
    )  # 'pr' | 'issue' | 'scan'
    source_task_id = Column(Integer, nullable=False, index=True)
    iteration_number = Column(Integer, nullable=False, default=1)
    role_name = Column(String(50), nullable=False, default="reviewer")
    status = Column(String(50), default="running", nullable=False, index=True)
    model = Column(String(255), nullable=True)
    tool_calls_count = Column(Integer, default=0, nullable=False)
    last_seq = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    result_payload = Column(LONGTEXT, nullable=True)
    started_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)

    messages = relationship(
        "ActivityMessage", back_populates="session", cascade="all, delete-orphan"
    )
    tool_calls = relationship(
        "ActivityToolCall", back_populates="session", cascade="all, delete-orphan"
    )


class ActivityMessage(Base):
    """Conversation message for activity tasks."""

    __tablename__ = "activity_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_activity_message_seq"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("activity_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq = Column(Integer, nullable=False)
    role = Column(String(50), nullable=False, index=True)
    content = Column(LONGTEXT, nullable=True)
    message_json = Column(LONGTEXT, nullable=False)
    tool_call_id = Column(String(255), nullable=True, index=True)
    finish_reason = Column(String(100), nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    session = relationship("ActivitySession", back_populates="messages")


class ActivityToolCall(Base):
    """Tool call tracking for activity tasks."""

    __tablename__ = "activity_tool_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("activity_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assistant_message_id = Column(
        Integer,
        ForeignKey("activity_messages.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tool_call_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    arguments_json = Column(LONGTEXT, nullable=True)
    status = Column(String(50), default="pending", nullable=False, index=True)
    result_message_id = Column(
        Integer,
        ForeignKey("activity_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    error_message = Column(Text, nullable=True)

    session = relationship("ActivitySession", back_populates="tool_calls")
    assistant_message = relationship(
        "ActivityMessage", foreign_keys=[assistant_message_id]
    )
    result_message = relationship("ActivityMessage", foreign_keys=[result_message_id])
