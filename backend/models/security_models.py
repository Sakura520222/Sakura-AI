"""Security event audit models."""

from datetime import datetime

from sqlalchemy import TIMESTAMP, Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from backend.models.database import Base


class SecurityEventLog(Base):
    """安全事件审计日志。"""

    __tablename__ = "security_event_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True
    )
    target_user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True
    )
    event_type = Column(String(80), nullable=False, index=True)
    event_result = Column(String(30), nullable=False, index=True)
    ip_address = Column(String(100), nullable=True)
    user_agent = Column(String(500), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)

    actor = relationship("TelegramUser", foreign_keys=[actor_user_id])
    target = relationship("TelegramUser", foreign_keys=[target_user_id])

    def __repr__(self):
        return f"<SecurityEventLog(type={self.event_type}, result={self.event_result})>"
