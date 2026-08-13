"""仓库扫描数据模型"""

import enum

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class ScanStatus(str, enum.Enum):
    """扫描状态"""

    PENDING = "pending"
    INDEXING = "indexing"
    ANALYZING = "analyzing"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingSeverity(str, enum.Enum):
    """发现严重程度"""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class FindingCategory(str, enum.Enum):
    """发现分类"""

    SECURITY = "security"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    MAINTAINABILITY = "maintainability"
    ARCHITECTURE = "architecture"


class RepoScan(Base):
    """仓库扫描记录表"""

    __tablename__ = "repo_scans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_name = Column(String(255), nullable=False, index=True)
    repo_owner = Column(String(100), nullable=False)

    # 触发方式
    trigger_type = Column(String(50), nullable=False)  # scheduled / manual
    triggered_by = Column(String(100), nullable=True)  # manual 时有值

    # 扫描配置快照
    commit_sha = Column(String(64), nullable=True)
    file_count = Column(Integer, default=0)
    code_file_count = Column(Integer, default=0)

    # 状态
    status = Column(
        String(50), default=ScanStatus.PENDING.value, nullable=False, index=True
    )
    progress = Column(Integer, default=0)  # 0-100
    current_phase = Column(
        String(50), nullable=True
    )  # indexing / analyzing / reporting
    error_message = Column(Text, nullable=True)

    # 扫描结果摘要
    total_findings = Column(Integer, default=0)
    critical_count = Column(Integer, default=0)
    major_count = Column(Integer, default=0)
    minor_count = Column(Integer, default=0)
    suggestion_count = Column(Integer, default=0)
    overall_health_score = Column(Integer, nullable=True)  # 0-100

    # Token 消耗与成本
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    estimated_cost = Column(Integer, default=0)  # 单位：分

    # GitHub Issue 报告
    report_issue_number = Column(BigInteger, nullable=True, index=True)
    report_issue_url = Column(String(500), nullable=True)

    # Issue 分析关联（扫描创建的 Issue 被 AI 分析后回填）
    issue_analysis_id = Column(Integer, nullable=True)

    # 时间戳
    created_at = Column(UTCDateTime, default=utc_now, nullable=False, index=True)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at = Column(UTCDateTime, nullable=True)
    completed_at = Column(UTCDateTime, nullable=True)

    # 关联
    findings = relationship(
        "ScanFinding", back_populates="scan", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<RepoScan(id={self.id}, repo={self.repo_name}, status={self.status})>"


class ScanFinding(Base):
    """扫描发现详情表"""

    __tablename__ = "scan_findings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(
        Integer,
        ForeignKey("repo_scans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 发现位置
    file_path = Column(String(512), nullable=True, index=True)
    line_start = Column(Integer, nullable=True)
    line_end = Column(Integer, nullable=True)

    # 分类
    severity = Column(String(50), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)

    # 内容
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=False)
    suggestion = Column(Text, nullable=True)
    code_snippet = Column(Text, nullable=True)

    # AI 置信度
    confidence = Column(Integer, nullable=True)  # 0-100

    # 时间戳
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)

    # 关联
    scan = relationship("RepoScan", back_populates="findings")

    def __repr__(self):
        return f"<ScanFinding(id={self.id}, severity={self.severity}, category={self.category})>"
