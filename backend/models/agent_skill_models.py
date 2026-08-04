"""Agent Skills 数据模型。"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, Column, Integer, String, Text

from backend.models.database import Base, utc_now


class AgentSkill(Base):
    """Agent 可加载 Skill 元数据。"""

    __tablename__ = "agent_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(120), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    when_to_use = Column(Text, nullable=True)
    version = Column(String(100), nullable=True)
    source_type = Column(String(50), nullable=False, default="upload", index=True)
    source_url = Column(Text, nullable=True)
    source_ref = Column(String(255), nullable=True)
    source_path = Column(Text, nullable=True)
    install_path = Column(Text, nullable=False)
    enabled = Column(Integer, default=1, nullable=False, index=True)
    content_hash = Column(String(64), nullable=False, index=True)
    file_count = Column(Integer, default=1, nullable=False)
    allowed_tools = Column(
        Text, nullable=True, comment="Skill 声明允许使用的工具，JSON 数组"
    )
    arguments = Column(Text, nullable=True, comment="Skill 命名参数定义，JSON 数组")
    requires = Column(Text, nullable=True, comment="Skill 运行前置条件描述")
    created_by = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False, index=True)
    updated_at = Column(TIMESTAMP, default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self) -> str:
        return f"<AgentSkill(id={self.id}, slug={self.slug}, enabled={self.enabled})>"
