"""Telegram Bot 数据模型"""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    TIMESTAMP,
    ForeignKey,
    Boolean,
    UniqueConstraint,
)
import enum

from backend.models.database import Base


class UserRole(str, enum.Enum):
    """用户角色"""

    SUPER_ADMIN = "super_admin"  # 超级管理员（唯一，从环境变量读取）
    ADMIN = "admin"  # 管理员
    USER = "user"  # 普通用户


class QuotaType(str, enum.Enum):
    """配额类型"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TelegramUser(Base):
    """Telegram 用户表"""

    __tablename__ = "telegram_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    github_username = Column(String(100), unique=True, nullable=True, index=True)
    role = Column(
        String(50), default=UserRole.USER.value, nullable=False
    )  # 改为 String 类型

    # 配额设置
    daily_quota = Column(Integer, default=10, nullable=False)
    weekly_quota = Column(Integer, default=50, nullable=False)
    monthly_quota = Column(Integer, default=200, nullable=False)

    # 已使用配额
    daily_used = Column(Integer, default=0, nullable=False)
    weekly_used = Column(Integer, default=0, nullable=False)
    monthly_used = Column(Integer, default=0, nullable=False)

    # 配额重置时间
    last_reset_daily = Column(TIMESTAMP, nullable=True)
    last_reset_weekly = Column(TIMESTAMP, nullable=True)
    last_reset_monthly = Column(TIMESTAMP, nullable=True)

    # Issue 分析配额设置
    issue_daily_quota = Column(Integer, default=20, nullable=False)
    issue_weekly_quota = Column(Integer, default=80, nullable=False)
    issue_monthly_quota = Column(Integer, default=300, nullable=False)

    # Issue 分析已使用配额
    issue_daily_used = Column(Integer, default=0, nullable=False)
    issue_weekly_used = Column(Integer, default=0, nullable=False)
    issue_monthly_used = Column(Integer, default=0, nullable=False)

    # Issue 配额重置时间
    last_reset_issue_daily = Column(TIMESTAMP, nullable=True)
    last_reset_issue_weekly = Column(TIMESTAMP, nullable=True)
    last_reset_issue_monthly = Column(TIMESTAMP, nullable=True)

    # 状态
    is_active = Column(Boolean, default=True, nullable=False)

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<TelegramUser(telegram_id={self.telegram_id}, github_username={self.github_username}, role={self.role})>"


class RepoSubscription(Base):
    """仓库订阅表（白名单）"""

    __tablename__ = "repo_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_name = Column(
        String(255), unique=True, nullable=False, index=True
    )  # 格式: owner/repo
    is_active = Column(Boolean, default=True, nullable=False)

    # 创建者
    added_by = Column(BigInteger, nullable=True)  # Telegram ID

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<RepoSubscription(repo_name={self.repo_name}, is_active={self.is_active})>"


class UserRepoSubscription(Base):
    """用户仓库订阅表"""

    __tablename__ = "user_repo_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(
        BigInteger,
        ForeignKey("telegram_users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_name = Column(String(255), nullable=False, index=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("telegram_id", "repo_name", name="uq_user_repo"),
    )

    def __repr__(self):
        return f"<UserRepoSubscription(telegram_id={self.telegram_id}, repo_name={self.repo_name})>"


class QuotaUsageLog(Base):
    """配额使用日志"""

    __tablename__ = "quota_usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    repo_name = Column(String(255), nullable=False)
    pr_number = Column(Integer, nullable=False)
    usage_type = Column(String(50), nullable=False)  # 改为 String 类型
    usage_category = Column(String(50), nullable=True)  # "pr_review" 或 "issue_analysis"

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)

    def __repr__(self):
        return f"<QuotaUsageLog(user_id={self.telegram_user_id}, repo={self.repo_name}, pr={self.pr_number})>"
