"""实时活动事件模型 — 记录 PR 审查 / Issue 分析 / 仓库扫描的对话流事件。"""

from sqlalchemy import TIMESTAMP, Column, Index, Integer, String
from sqlalchemy.dialects.mysql import LONGTEXT

from backend.models.database import Base, utc_now


class ActivityEvent(Base):
    """实时活动事件日志，用于驱动前端对话流 UI。"""

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_task", "task_type", "task_id"),
        Index("ix_activity_task_id", "task_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_type = Column(
        String(20), nullable=False, index=True
    )  # 'pr' | 'issue' | 'scan'
    task_id = Column(Integer, nullable=False)
    # 事件类型: status / thinking / tool_call / tool_result / ai_response / error / result
    event_type = Column(String(50), nullable=False)
    content = Column(LONGTEXT, nullable=True)  # JSON 字符串
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    def __repr__(self):
        return f"<ActivityEvent(id={self.id}, {self.task_type}-{self.task_id}, {self.event_type})>"
