"""Agent Skills 数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, TIMESTAMP

from backend.models.database import Base


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
    created_by = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<AgentSkill(id={self.id}, slug={self.slug}, enabled={self.enabled})>"