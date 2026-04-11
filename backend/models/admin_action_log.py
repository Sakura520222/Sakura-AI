"""管理员操作日志模型"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship

from backend.models.database import Base


class AdminActionLog(Base):
    """管理员操作日志"""

    __tablename__ = "admin_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False, index=True)
    target_type = Column(String(50), nullable=True)
    target_id = Column(String(255), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)

    admin = relationship("TelegramUser", foreign_keys=[admin_id])
