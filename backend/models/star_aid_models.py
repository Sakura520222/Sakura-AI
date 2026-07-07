"""仓库互助功能数据模型 / Repository mutual-star-aid data models.

本模块定义"仓库互助"功能所需的五张表：

- ``StarAidMember``        — 计划参与者状态（加入/退出/封禁/重新授权）。
- ``StarAidCredential``    — GitHub App user-to-server 凭据（仅加密保存）。
- ``StarAidRepository``    — 参与展示的仓库与 AI 摘要。
- ``StarAidActionLog``     — star / unstar / skip / fail 操作幂等审计日志。
- ``StarAidRepositoryMetric`` — 仓库活跃度采样，用于排序。

设计约束（见 docs/plans/2026-06-28-repository-star-aid.md）：

- token 字段只存加密密文，日志/异常/审计一律不打印原文。
- ``actor_user_id`` / ``owner_user_id`` 不设外键硬约束，保证审计记录在
  用户退出/删除后仍可追溯；由业务层负责引用一致性。
- 幂等唯一键 ``UNIQUE(actor_user_id, target_repository_id, action)`` 防止
  重复 star/unstar；重试明细如有需要另存，不破坏最终状态幂等表。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)

from backend.models.database import Base


# ---------- 合法状态枚举值（以字符串存储，避免 Enum 迁移陷阱）----------
# Member status
MEMBER_STATUS_ACTIVE = "active"
MEMBER_STATUS_PAUSED = "paused"
MEMBER_STATUS_LEFT = "left"
MEMBER_STATUS_BANNED = "banned"
MEMBER_STATUS_REAUTH_REQUIRED = "reauth_required"

# Action log
ACTION_STAR = "star"
ACTION_UNSTAR = "unstar"
ACTION_SKIP = "skip"
ACTION_REFRESH = "refresh"
ACTION_MANUAL_STAR = "manual_star"

TRIGGER_SCHEDULER = "scheduler"
TRIGGER_MANUAL = "manual"
TRIGGER_JOIN = "join"
TRIGGER_EXIT_CLEANUP = "exit_cleanup"
TRIGGER_SUMMARY_REFRESH = "summary_refresh"

ACTION_STATUS_SUCCESS = "success"
ACTION_STATUS_ALREADY_DONE = "already_done"
ACTION_STATUS_SKIPPED = "skipped"
ACTION_STATUS_FAILED = "failed"
ACTION_STATUS_RATE_LIMITED = "rate_limited"
ACTION_STATUS_REAUTH_REQUIRED = "reauth_required"

# Summary status
SUMMARY_PENDING = "pending"
SUMMARY_READY = "ready"
SUMMARY_FAILED = "failed"
SUMMARY_STALE = "stale"


class StarAidMember(Base):
    """仓库互助计划参与者状态 / Star-aid plan member state.

    一名系统用户最多对应一条记录（``UNIQUE(user_id)``）。
    """

    __tablename__ = "star_aid_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 对应 telegram_users.id；不加外键，保留审计独立性
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    github_username = Column(String(100), nullable=True, index=True)

    # active / paused / left / banned / reauth_required
    status = Column(String(30), nullable=False, default=MEMBER_STATUS_ACTIVE)

    joined_at = Column(TIMESTAMP, nullable=True)
    left_at = Column(TIMESTAMP, nullable=True)
    banned_at = Column(TIMESTAMP, nullable=True)
    banned_by_user_id = Column(Integer, nullable=True)
    ban_reason = Column(Text, nullable=True)

    auto_star_enabled = Column(Boolean, nullable=False, default=True)

    # 每日自动 star 配额；join 时由 service 从 settings 写入实际默认值
    daily_star_limit = Column(Integer, nullable=False, default=20)
    daily_star_used = Column(Integer, nullable=False, default=0)
    last_daily_reset_at = Column(TIMESTAMP, nullable=True)

    # 调度器使用的下一次执行时间（随机间隔）
    last_scheduled_at = Column(TIMESTAMP, nullable=True)
    next_scheduled_at = Column(TIMESTAMP, nullable=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_star_aid_member_status_next", "status", "next_scheduled_at"),
    )

    def __repr__(self):
        return (
            f"<StarAidMember(user_id={self.user_id}, "
            f"github={self.github_username}, status={self.status})>"
        )


class StarAidCredential(Base):
    """GitHub App user-to-server 凭据（仅加密保存）/ GitHub App user token.

    只保存加密后的 access_token / refresh_token，绝不保存 WebUI 登录用的
    OAuth App token。日志与异常中禁止打印 token 前缀与全文。
    """

    __tablename__ = "star_aid_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    github_username = Column(String(100), nullable=True, index=True)

    encrypted_access_token = Column(Text, nullable=True)
    access_token_expires_at = Column(TIMESTAMP, nullable=True)

    encrypted_refresh_token = Column(Text, nullable=True)
    refresh_token_expires_at = Column(TIMESTAMP, nullable=True)

    token_type = Column(String(30), nullable=False, default="bearer")
    # 授权时使用的 GitHub App client id，便于多 App 场景区分
    github_app_client_id = Column(String(100), nullable=True)

    last_authorized_at = Column(TIMESTAMP, nullable=True)
    last_refresh_at = Column(TIMESTAMP, nullable=True)
    revoked_at = Column(TIMESTAMP, nullable=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self):
        # 刻意不输出任何 token 信息
        return f"<StarAidCredential(user_id={self.user_id}, github={self.github_username})>"


class StarAidRepository(Base):
    """互助展示仓库与 AI 摘要 / Displayed repository with AI summary.

    ``full_name`` 全局唯一（``owner/repo``）。一名用户可展示多个仓库。
    """

    __tablename__ = "star_aid_repositories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 提交该展示仓库的系统用户
    owner_user_id = Column(Integer, nullable=False, index=True)
    installation_id = Column(BigInteger, nullable=True)

    repo_id = Column(BigInteger, nullable=True, unique=True)
    full_name = Column(String(255), nullable=False, unique=True, index=True)
    owner_login = Column(String(100), nullable=True, index=True)
    repo_name = Column(String(150), nullable=True)
    html_url = Column(String(500), nullable=True)

    description = Column(Text, nullable=True)
    topics_json = Column(Text, nullable=True)
    primary_language = Column(String(100), nullable=True)

    stargazers_count = Column(Integer, nullable=False, default=0)
    pushed_at = Column(TIMESTAMP, nullable=True)

    readme_sha = Column(String(80), nullable=True)
    readme_excerpt = Column(Text, nullable=True)

    ai_summary = Column(Text, nullable=True)
    ai_summary_language = Column(String(10), nullable=True)
    # pending / ready / failed / stale
    ai_summary_status = Column(String(30), nullable=False, default=SUMMARY_PENDING)
    ai_summary_error = Column(Text, nullable=True)
    ai_summary_updated_at = Column(TIMESTAMP, nullable=True)

    is_displayed = Column(Boolean, nullable=False, default=True)
    is_public = Column(Boolean, nullable=False, default=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    disabled_by_admin = Column(Boolean, nullable=False, default=False)
    disabled_reason = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_star_aid_repo_display_stars_pushed",
            "is_displayed",
            "disabled_by_admin",
            "stargazers_count",
            "pushed_at",
        ),
    )

    def __repr__(self):
        return f"<StarAidRepository(full_name={self.full_name}, stars={self.stargazers_count})>"


class StarAidActionLog(Base):
    """star / unstar / skip / fail 操作幂等审计日志 / Action audit log.

    ``UNIQUE(actor_user_id, target_repository_id, action)`` 用于幂等：同一
    用户对同一仓库的同一动作只保留最终状态记录。多次重试明细若需要可另
    存到独立尝试表，不破坏此幂等表。
    """

    __tablename__ = "star_aid_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, nullable=False, index=True)
    target_repository_id = Column(Integer, nullable=False, index=True)

    # star / unstar / skip / refresh / manual_star
    action = Column(String(30), nullable=False)
    # scheduler / manual / join / exit_cleanup / summary_refresh
    trigger = Column(String(30), nullable=False)
    # success / already_done / skipped / failed / rate_limited / reauth_required
    status = Column(String(30), nullable=False)

    github_status_code = Column(Integer, nullable=True)
    github_request_id = Column(String(100), nullable=True)
    error_code = Column(String(100), nullable=True)
    # sanitized error message，不含 token
    error_message = Column(Text, nullable=True)

    # 是否由本功能创建了 star（用于退出时可选批量取消）
    created_star = Column(Boolean, nullable=False, default=False)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "target_repository_id",
            "action",
            name="uq_star_aid_action_actor_repo_action",
        ),
    )

    def __repr__(self):
        return (
            f"<StarAidActionLog(actor={self.actor_user_id}, "
            f"repo={self.target_repository_id}, action={self.action}, "
            f"status={self.status})>"
        )


class StarAidRepositoryMetric(Base):
    """仓库活跃度采样（用于按活跃度排序）/ Repository activity metric sample.

    活跃度计算（见计划文档 6.5）::

        activity_score =
            min(stargazers_count, 5000) * 0.5
            + recent_push_score * 0.3
            + min(forks_count, 1000) * 0.2

    ``recent_push_score``：30 天内 100，90 天内 60，180 天内 30，否则 0。
    计算逻辑由 service 层实现，本表只存采样快照。
    """

    __tablename__ = "star_aid_repository_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repository_id = Column(Integer, nullable=False, index=True)
    sampled_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)
    stargazers_count = Column(Integer, nullable=True)
    pushed_at = Column(TIMESTAMP, nullable=True)
    open_issues_count = Column(Integer, nullable=True)
    forks_count = Column(Integer, nullable=True)

    def __repr__(self):
        return f"<StarAidRepositoryMetric(repo={self.repository_id}, stars={self.stargazers_count})>"
