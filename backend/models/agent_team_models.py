"""Agent 专家团队模式数据模型"""

import enum

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text, TIMESTAMP
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship

from backend.models.database import Base
from backend.models.database import utc_now


DEFAULT_AGENT_TEAM_MAX_ITERATIONS = 3


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
    MANUAL_ISSUE = "manual_issue"


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
    workspace_path = Column(String(1000), nullable=True)
    base_branch = Column(String(255), nullable=True)
    base_commit_sha = Column(String(64), nullable=True)
    resume_count = Column(Integer, default=0, nullable=False)
    failed_phase = Column(String(50), nullable=True)
    failed_role = Column(String(50), nullable=True)
    rate_limit_reset_at = Column(TIMESTAMP, nullable=True)
    last_checkpoint_at = Column(TIMESTAMP, nullable=True)
    pr_number = Column(BigInteger, nullable=True, index=True)
    pr_url = Column(String(500), nullable=True)

    iteration_count = Column(Integer, default=0, nullable=False)
    max_iterations = Column(
        Integer, default=DEFAULT_AGENT_TEAM_MAX_ITERATIONS, nullable=False
    )
    started_by = Column(String(100), nullable=True)
    locked_by = Column(String(100), nullable=True)
    ai_config_snapshot = Column(Text, nullable=True)

    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    estimated_cost = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, default=utc_now, nullable=False, index=True)
    updated_at = Column(TIMESTAMP, default=utc_now, onupdate=utc_now, nullable=False)
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)

    iterations = relationship(
        "AgentTeamIteration", back_populates="task", cascade="all, delete-orphan"
    )
    feedback = relationship(
        "AgentTeamFeedback", back_populates="task", cascade="all, delete-orphan"
    )
    sessions = relationship(
        "AgentTeamSession", back_populates="task", cascade="all, delete-orphan"
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


class AgentTeamSession(Base):
    """Agent 角色会话记录。"""

    __tablename__ = "agent_team_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer,
        ForeignKey("agent_team_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    iteration_number = Column(Integer, nullable=False, index=True)
    role_name = Column(String(50), nullable=False, index=True)
    resume_index = Column(Integer, default=0, nullable=False)
    status = Column(String(50), default="running", nullable=False, index=True)
    model = Column(String(255), nullable=True)
    tool_calls_count = Column(Integer, default=0, nullable=False)
    last_seq = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    started_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)

    task = relationship("AgentTeamTask", back_populates="sessions")
    messages = relationship(
        "AgentTeamMessage", back_populates="session", cascade="all, delete-orphan"
    )
    tool_calls = relationship(
        "AgentTeamToolCall", back_populates="session", cascade="all, delete-orphan"
    )


class AgentTeamMessage(Base):
    """Agent OpenAI-compatible 消息日志。"""

    __tablename__ = "agent_team_messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_agent_message_seq"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("agent_team_sessions.id", ondelete="CASCADE"),
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

    session = relationship("AgentTeamSession", back_populates="messages")


class AgentTeamToolCall(Base):
    """Agent 工具调用账本。"""

    __tablename__ = "agent_team_tool_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("agent_team_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assistant_message_id = Column(
        Integer,
        ForeignKey("agent_team_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_call_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    arguments_json = Column(LONGTEXT, nullable=True)
    arguments_hash = Column(String(64), nullable=True)
    status = Column(String(50), default="pending", nullable=False, index=True)
    result_message_id = Column(
        Integer,
        ForeignKey("agent_team_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    error_message = Column(Text, nullable=True)

    session = relationship("AgentTeamSession", back_populates="tool_calls")
    assistant_message = relationship(
        "AgentTeamMessage", foreign_keys=[assistant_message_id]
    )
    result_message = relationship("AgentTeamMessage", foreign_keys=[result_message_id])


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
