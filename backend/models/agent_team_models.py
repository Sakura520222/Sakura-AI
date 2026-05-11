"""Agent 专家团队模式数据模型"""

import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text, TIMESTAMP
from sqlalchemy.orm import relationship

from backend.models.database import Base


DEFAULT_AGENT_TEAM_MAX_ITERATIONS = 3


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


class AgentTeamTaskStatus(str, enum.Enum):
    """Agent 专家团队任务状态"""

    CANDIDATE = "candidate"
    QUEUED = "queued"
    PLANNING = "planning"
    CLONING = "cloning"
    EDITING = "editing"
    SELF_REVIEWING = "self_reviewing"
    VALIDATING = "validating"
    PUSHING = "pushing"
    PR_OPENED = "pr_opened"
    EXTERNAL_REVIEWING = "external_reviewing"
    ITERATING = "iterating"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class AgentTeamSourceType(str, enum.Enum):
    """Agent 任务来源类型"""

    ISSUE_ANALYSIS = "issue_analysis"
    SCAN_FINDING = "scan_finding"
    SCAN_REPORT_ISSUE = "scan_report_issue"


class AgentTeamFeedbackSource(str, enum.Enum):
    """Agent 反馈来源"""

    INTERNAL_REVIEW = "internal_review"
    SAKURA_PR_REVIEW = "sakura_pr_review"
    HUMAN_REVIEW = "human_review"
    ISSUE_COMMENT = "issue_comment"
    SYSTEM = "system"


class AgentTeamTask(Base):
    """Agent 专家团队任务"""

    __tablename__ = "agent_team_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(String(50), nullable=False, index=True)
    source_id = Column(Integer, nullable=True, index=True)
    source_issue_number = Column(BigInteger, nullable=True, index=True)

    repo_full_name = Column(String(255), nullable=False, index=True)
    repo_owner = Column(String(100), nullable=False)
    repo_name = Column(String(255), nullable=False)

    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    priority = Column(String(50), nullable=True, index=True)
    candidate_score = Column(Integer, default=0, nullable=False, index=True)

    status = Column(
        String(50),
        default=AgentTeamTaskStatus.CANDIDATE.value,
        nullable=False,
        index=True,
    )
    current_phase = Column(String(50), nullable=True)
    branch_name = Column(String(255), nullable=True)
    pr_number = Column(BigInteger, nullable=True, index=True)
    pr_url = Column(String(500), nullable=True)

    iteration_count = Column(Integer, default=0, nullable=False)
    max_iterations = Column(Integer, default=DEFAULT_AGENT_TEAM_MAX_ITERATIONS, nullable=False)
    started_by = Column(String(100), nullable=True)
    locked_by = Column(String(100), nullable=True)
    ai_config_snapshot = Column(Text, nullable=True)

    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    estimated_cost = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, default=utc_now, nullable=False, index=True)
    updated_at = Column(
        TIMESTAMP, default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)

    iterations = relationship(
        "AgentTeamIteration", back_populates="task", cascade="all, delete-orphan"
    )
    feedback = relationship(
        "AgentTeamFeedback", back_populates="task", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<AgentTeamTask(id={self.id}, repo={self.repo_full_name}, status={self.status})>"


class AgentTeamIteration(Base):
    """Agent 专家团队迭代记录"""

    __tablename__ = "agent_team_iterations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer,
        ForeignKey("agent_team_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    iteration_number = Column(Integer, nullable=False)
    fullstack_plan = Column(Text, nullable=True)
    fullstack_result = Column(Text, nullable=True)
    professional_review = Column(Text, nullable=True)
    review_passed = Column(Integer, default=0, nullable=False)
    test_command = Column(Text, nullable=True)
    test_output = Column(Text, nullable=True)
    test_passed = Column(Integer, default=0, nullable=False)
    diff_summary = Column(Text, nullable=True)
    decision = Column(String(50), nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)

    task = relationship("AgentTeamTask", back_populates="iterations")
    patch_files = relationship(
        "AgentTeamPatchFile",
        back_populates="iteration",
        cascade="all, delete-orphan",
    )


class AgentTeamPatchFile(Base):
    """Agent 修改文件记录"""

    __tablename__ = "agent_team_patch_files"
    id = Column(Integer, primary_key=True, autoincrement=True)
    iteration_id = Column(
        Integer,
        ForeignKey("agent_team_iterations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(String(512), nullable=False, index=True)
    change_type = Column(String(50), nullable=True)
    additions = Column(Integer, default=0)
    deletions = Column(Integer, default=0)
    diff_summary = Column(Text, nullable=True)
    risk_level = Column(String(50), nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    iteration = relationship("AgentTeamIteration", back_populates="patch_files")


class AgentTeamFeedback(Base):
    """Agent 任务反馈记录"""

    __tablename__ = "agent_team_feedback"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer,
        ForeignKey("agent_team_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source = Column(String(50), nullable=False, index=True)
    external_id = Column(String(255), nullable=True)
    author = Column(String(100), nullable=True)
    content = Column(Text, nullable=False)
    resolved = Column(Integer, default=0, nullable=False)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    task = relationship("AgentTeamTask", back_populates="feedback")