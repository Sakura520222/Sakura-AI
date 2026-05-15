"""数据库模型定义"""

from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    TIMESTAMP,
    ForeignKey,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import enum

Base = declarative_base()


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间（公共工具函数，供所有模型共享）。"""
    return datetime.now(timezone.utc)


# 异步数据库引擎和会话（将在 init_async_db 中初始化）
async_engine = None
async_session = None


class PRStatus(str, enum.Enum):
    """PR审查状态"""

    PENDING = "pending"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewDecision(str, enum.Enum):
    """审查决策（小写值匹配数据库）"""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


class ReviewStrategy(str, enum.Enum):
    """审查策略（小写值匹配数据库）"""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"
    LARGE = "large"
    SKIP = "skip"


class CommentSeverity(str, enum.Enum):
    """评论严重程度（小写值匹配数据库）"""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class CommentType(str, enum.Enum):
    """评论类型（小写值匹配数据库）"""

    OVERALL = "overall"
    FILE = "file"
    LINE = "line"


class IndexingStatus(str, enum.Enum):
    """文档索引状态"""

    PENDING = "pending"
    INDEXING = "indexing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class CodeIndexingStatus(str, enum.Enum):
    """代码索引状态"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class IssueAnalysisStatus(str, enum.Enum):
    """Issue分析状态"""

    PENDING = "pending"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class IssueCategory(str, enum.Enum):
    """Issue分类"""

    BUG = "bug"
    FEATURE = "feature"
    QUESTION = "question"
    DOCUMENTATION = "documentation"
    ENHANCEMENT = "enhancement"
    PERFORMANCE = "performance"
    SECURITY = "security"
    REFACTOR = "refactor"
    OTHER = "other"


class IssuePriority(str, enum.Enum):
    """Issue优先级"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PRReview(Base):
    """PR审查记录表"""

    __tablename__ = "pr_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pr_id = Column(BigInteger, nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    repo_owner = Column(String(100), nullable=False)
    author = Column(String(100))
    title = Column(String(500))
    branch = Column(String(100))

    # PR统计信息
    file_count = Column(Integer)
    line_count = Column(Integer)
    code_file_count = Column(Integer)

    # 审查配置
    strategy = Column(String(50), nullable=False)

    # 状态
    status = Column(String(50), default=PRStatus.PENDING.value, nullable=False)
    error_message = Column(Text, nullable=True)

    # 审查结果
    review_summary = Column(Text, nullable=True)
    overall_score = Column(Integer, nullable=True)  # 1-10分

    # 审查决策
    decision = Column(String(50), nullable=True)
    decision_reason = Column(Text, nullable=True)

    # Token 消耗与成本
    prompt_tokens = Column(Integer, default=0, nullable=True)
    completion_tokens = Column(Integer, default=0, nullable=True)
    estimated_cost = Column(Integer, default=0, nullable=True)

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    completed_at = Column(TIMESTAMP, nullable=True)

    # 关联评论
    comments = relationship(
        "ReviewComment", back_populates="review", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<PRReview(id={self.id}, pr_id={self.pr_id}, repo={self.repo_name}, strategy={self.strategy})>"


class ReviewComment(Base):
    """审查评论表"""

    __tablename__ = "review_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(
        Integer, ForeignKey("pr_reviews.id", ondelete="CASCADE"), nullable=False
    )

    # 文件信息
    file_path = Column(String(500), nullable=True)
    line_number = Column(Integer, nullable=True)

    # 评论内容
    comment_type = Column(String(50), default=CommentType.OVERALL.value, nullable=False)
    severity = Column(
        String(50), default=CommentSeverity.SUGGESTION.value, nullable=False
    )
    content = Column(Text, nullable=False)

    # 创建时间
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)

    # 关联审查记录
    review = relationship("PRReview", back_populates="comments")

    def __repr__(self):
        return f"<ReviewComment(id={self.id}, type={self.comment_type}, severity={self.severity})>"


class AppConfig(Base):
    """应用配置表"""

    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_name = Column(String(100), unique=True, nullable=False, index=True)
    key_value = Column(Text, nullable=True)
    description = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<AppConfig(key={self.key_name})>"


class UserConfig(Base):
    """用户级业务配置表 / User-scoped business configuration."""

    __tablename__ = "user_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    config_key = Column(String(100), nullable=False, index=True)
    config_value = Column(Text, nullable=True)
    description = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "config_key", name="uq_user_config_key"),
    )

    def __repr__(self):
        return f"<UserConfig(user_id={self.user_id}, key={self.config_key})>"


class ReviewQueue(Base):
    """审查队列表"""

    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pr_id = Column(BigInteger, nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    action = Column(String(50), nullable=False)  # opened, synchronized, reopened

    # 优先级（数字越小优先级越高）
    priority = Column(Integer, default=10, nullable=False)

    # 状态
    status = Column(
        String(50), default="pending", nullable=False
    )  # pending, processing, completed, failed
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)
    error_message = Column(Text, nullable=True)

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    processed_at = Column(TIMESTAMP, nullable=True)

    def __repr__(self):
        return f"<ReviewQueue(id={self.id}, pr_id={self.pr_id}, status={self.status})>"


class DocumentIndex(Base):
    """文档索引表"""

    __tablename__ = "document_indices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name = Column(String(255), unique=True, nullable=False, index=True)
    last_commit_hash = Column(String(64), nullable=True)
    last_indexed_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    document_count = Column(Integer, default=0, nullable=False)
    total_chunks = Column(Integer, default=0, nullable=False)
    indexing_status = Column(
        String(50), default=IndexingStatus.PENDING.value, nullable=False, index=True
    )
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<DocumentIndex(id={self.id}, repo={self.repo_full_name}, status={self.indexing_status})>"


class DocumentFile(Base):
    """文档文件表（文件级别的索引追踪）"""

    __tablename__ = "document_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name = Column(String(255), nullable=False, index=True)
    file_path = Column(String(512), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    file_size = Column(Integer, default=0, nullable=False)
    chunk_count = Column(Integer, default=0, nullable=False)
    last_indexed_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    last_indexed_commit_hash = Column(String(64), nullable=True, index=True)
    indexed = Column(
        Integer, default=0, nullable=False
    )  # 0=False, 1=True for MySQL compatibility
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<DocumentFile(id={self.id}, path={self.file_path}, indexed={self.indexed})>"


class CodeIndex(Base):
    """代码索引表 - 追踪仓库级别的代码索引状态"""

    __tablename__ = "code_indices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name = Column(String(255), unique=True, nullable=False, index=True)
    last_commit_hash = Column(String(64), nullable=True)
    last_indexed_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    file_count = Column(Integer, default=0, nullable=False)
    total_chunks = Column(Integer, default=0, nullable=False)
    indexing_status = Column(
        String(50),
        default=CodeIndexingStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    index_type = Column(
        String(50), default="full", nullable=False
    )  # full, pr, incremental
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<CodeIndex(id={self.id}, repo={self.repo_full_name}, status={self.indexing_status})>"


class CodeFile(Base):
    """代码文件索引表 - 文件级别的索引追踪"""

    __tablename__ = "code_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name = Column(String(255), nullable=False, index=True)
    file_path = Column(String(512), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)  # SHA-256 Content Hash
    language = Column(String(50), nullable=True)  # python, javascript, etc.
    chunk_count = Column(Integer, default=0, nullable=False)
    last_indexed_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    last_indexed_commit_hash = Column(String(64), nullable=True, index=True)
    commit_sha = Column(String(64), nullable=True)  # 精准指向Git版本
    indexed = Column(Integer, default=0, nullable=False)
    # PR关联（可选）
    pr_number = Column(Integer, nullable=True)
    # 状态管理
    is_deleted = Column(Integer, default=0, nullable=False)  # 0=False, 1=True
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return (
            f"<CodeFile(id={self.id}, path={self.file_path}, indexed={self.indexed})>"
        )


class IssueAnalysis(Base):
    """Issue 分析记录表"""

    __tablename__ = "issue_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_number = Column(BigInteger, nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    repo_owner = Column(String(100), nullable=False)
    author = Column(String(100))
    title = Column(String(500))
    body = Column(Text, nullable=True)

    # AI 分析结果
    category = Column(String(50), nullable=True)
    priority = Column(String(50), nullable=True)
    summary = Column(Text, nullable=True)
    feasibility = Column(Text, nullable=True)
    suggested_title = Column(String(256), nullable=True)
    suggested_assignees = Column(Text, nullable=True)
    suggested_labels = Column(Text, nullable=True)
    suggested_milestone = Column(String(255), nullable=True)
    duplicate_of = Column(BigInteger, nullable=True, index=True)
    related_prs = Column(Text, nullable=True)
    analysis_detail = Column(Text, nullable=True)

    # 版本
    analysis_version = Column(Integer, default=1, nullable=False)

    # Token 消耗与成本
    prompt_tokens = Column(Integer, default=0, nullable=True)
    completion_tokens = Column(Integer, default=0, nullable=True)
    estimated_cost = Column(Integer, default=0, nullable=True)

    # 状态
    status = Column(
        String(50), default=IssueAnalysisStatus.PENDING.value, nullable=False
    )
    error_message = Column(Text, nullable=True)

    # 评论与标签
    comment_posted = Column(Integer, default=0)
    comment_url = Column(String(500), nullable=True)
    labels_applied = Column(Integer, default=0)
    applied_label_names = Column(Text, nullable=True)

    # 时间戳
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    completed_at = Column(TIMESTAMP, nullable=True)

    def __repr__(self):
        return f"<IssueAnalysis(id={self.id}, issue={self.issue_number}, repo={self.repo_name})>"


class PRIssueLink(Base):
    """PR-Issue 关联表"""

    __tablename__ = "pr_issue_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pr_id = Column(BigInteger, nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    issue_number = Column(BigInteger, nullable=False, index=True)
    link_type = Column(String(50), nullable=False)
    reference_text = Column(String(255), nullable=True)
    inference_reason = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<PRIssueLink(pr={self.pr_id}, issue={self.issue_number}, type={self.link_type})>"


class IssueAnalysisQueue(Base):
    """Issue 分析队列表"""

    __tablename__ = "issue_analysis_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_number = Column(BigInteger, nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    action = Column(String(50), nullable=False)
    priority = Column(Integer, default=10, nullable=False)
    status = Column(String(50), default="pending", nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    processed_at = Column(TIMESTAMP, nullable=True)

    def __repr__(self):
        return f"<IssueAnalysisQueue(id={self.id}, issue={self.issue_number}, status={self.status})>"


async def create_tables_async():
    """异步创建所有数据库表"""
    global async_engine
    import logging

    logger = logging.getLogger(__name__)

    if async_engine is None:
        raise RuntimeError("异步数据库引擎未初始化,请先调用 init_async_db()")

    try:
        _ensure_model_modules_imported()

        # 在异步上下文中创建表
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("✅ 数据库表创建成功")

    except Exception as e:
        logger.error(f"❌ 数据库表创建失败: {e}")
        raise


def _ensure_model_modules_imported() -> None:
    """导入独立模型模块，确保 metadata 已注册。"""
    import backend.models.agent_skill_models  # noqa: F401
    import backend.models.agent_team_models  # noqa: F401


def _append_dynamic_config_defaults(default_configs: list) -> None:
    """向 default_configs 列表追加动态配置默认值"""
    try:
        _ensure_model_modules_imported()

        from backend.core.config import (
            DYNAMIC_CONFIG_GROUPS,
            DYNAMIC_CONFIG_LABELS,
            get_settings,
        )

        settings = get_settings()
        for group_data in DYNAMIC_CONFIG_GROUPS.values():
            for key in group_data["keys"]:
                default_val = str(getattr(settings, key, ""))
                default_configs.append(
                    AppConfig(
                        key_name=key,
                        key_value=default_val,
                        description=DYNAMIC_CONFIG_LABELS.get(key, key),
                    )
                )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"追加动态配置默认值失败: {e}")


async def insert_default_configs_async():
    """异步插入默认配置"""
    global async_session
    import logging

    logger = logging.getLogger(__name__)

    if async_session is None:
        raise RuntimeError("异步会话工厂未初始化,请先调用 init_async_db()")

    default_configs = [
        AppConfig(key_name="app_version", key_value="2.10.1", description="应用版本号"),
        AppConfig(
            key_name="max_concurrent_reviews",
            key_value="5",
            description="最大并发审查数量",
        ),
        AppConfig(
            key_name="review_timeout_seconds",
            key_value="300",
            description="审查任务整体超时时间（秒）",
        ),
        AppConfig(
            key_name="enable_auto_review",
            key_value="true",
            description="是否启用 Webhook 自动审查",
        ),
        AppConfig(
            key_name="web_search_enabled",
            key_value="true",
            description="启用 Web 搜索工具",
        ),
        AppConfig(
            key_name="web_search_provider",
            key_value="duckduckgo",
            description="Web 搜索提供商",
        ),
        AppConfig(
            key_name="web_search_api_key", key_value="", description="Web 搜索 API Key"
        ),
        AppConfig(
            key_name="web_search_max_results",
            key_value="3",
            description="Web 搜索最大返回结果数",
        ),
        AppConfig(
            key_name="web_search_max_content_length",
            key_value="500",
            description="Web 搜索结果截断长度",
        ),
        AppConfig(
            key_name="web_search_timeout",
            key_value="15",
            description="Web 搜索超时时间（秒）",
        ),
        AppConfig(
            key_name="issue_auto_create_labels",
            key_value="true",
            description="自动为 Issue 应用 AI 推荐的标签",
        ),
        AppConfig(
            key_name="issue_max_tool_iterations",
            key_value="15",
            description="Issues 分析中 AI 工具调用最大迭代次数",
        ),
    ]

    # 从 config 模块追加动态配置默认值
    _append_dynamic_config_defaults(default_configs)

    try:
        async with async_session() as session:
            # 检查是否已有配置
            from sqlalchemy import select, func

            result = await session.execute(select(func.count(AppConfig.id)))
            existing_configs = result.scalar()

            added = 0
            for cfg in default_configs:
                result = await session.execute(
                    select(AppConfig).where(AppConfig.key_name == cfg.key_name)
                )
                if not result.scalar_one_or_none():
                    session.add(cfg)
                    added += 1
            if added > 0:
                await session.commit()
                logger.info(
                    f"✅ {'已插入默认配置' if existing_configs == 0 else f'补插 {added} 条缺失配置'}"
                )
            else:
                logger.info("配置已是最新，无需补插")

    except Exception as e:
        logger.error(f"❌ 插入默认配置失败: {e}")
        raise


def init_database(database_url: str):
    """初始化数据库,创建所有表(同步版本,仅用于迁移等特殊场景)

    Args:
        database_url: 数据库连接字符串
    """
    from sqlalchemy import create_engine
    import logging

    logger = logging.getLogger(__name__)

    try:
        # 创建数据库引擎
        engine = create_engine(database_url, echo=False)

        _ensure_model_modules_imported()

        # 创建所有表
        Base.metadata.create_all(engine)

        logger.info("数据库表初始化完成")

        # 插入默认配置
        from sqlalchemy.orm import Session

        session = Session(engine)

        try:
            # 检查是否已有配置
            existing_configs = session.query(AppConfig).count()

            default_configs = [
                AppConfig(
                    key_name="app_version", key_value="2.10.1", description="应用版本号"
                ),
                AppConfig(
                    key_name="max_concurrent_reviews",
                    key_value="5",
                    description="最大并发审查数量",
                ),
                AppConfig(
                    key_name="review_timeout_seconds",
                    key_value="300",
                    description="审查任务整体超时时间（秒）",
                ),
                AppConfig(
                    key_name="enable_auto_review",
                    key_value="true",
                    description="是否启用 Webhook 自动审查",
                ),
                AppConfig(
                    key_name="web_search_enabled",
                    key_value="false",
                    description="启用 Web 搜索工具",
                ),
                AppConfig(
                    key_name="web_search_provider",
                    key_value="duckduckgo",
                    description="Web 搜索提供商",
                ),
                AppConfig(
                    key_name="web_search_api_key",
                    key_value="",
                    description="Web 搜索 API Key",
                ),
                AppConfig(
                    key_name="web_search_max_results",
                    key_value="3",
                    description="Web 搜索最大返回结果数",
                ),
                AppConfig(
                    key_name="web_search_max_content_length",
                    key_value="500",
                    description="Web 搜索结果截断长度",
                ),
                AppConfig(
                    key_name="web_search_timeout",
                    key_value="15",
                    description="Web 搜索超时时间（秒）",
                ),
                AppConfig(
                    key_name="issue_auto_create_labels",
                    key_value="true",
                    description="自动为 Issue 应用 AI 推荐的标签",
                ),
                AppConfig(
                    key_name="issue_max_tool_iterations",
                    key_value="15",
                    description="Issues 分析中 AI 工具调用最大迭代次数",
                ),
            ]

            # 从 config 模块追加动态配置默认值
            _append_dynamic_config_defaults(default_configs)

            added = 0
            for cfg in default_configs:
                existing = (
                    session.query(AppConfig)
                    .filter(AppConfig.key_name == cfg.key_name)
                    .first()
                )
                if not existing:
                    session.add(cfg)
                    added += 1
            if added > 0:
                session.commit()
                logger.info(
                    f"{'已插入默认配置' if existing_configs == 0 else f'补插 {added} 条缺失配置'}"
                )
            else:
                logger.info("配置已是最新，无需补插")

        except Exception as e:
            session.rollback()
            logger.error(f"插入默认配置失败: {e}")
        finally:
            session.close()

        return engine

    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise


def init_async_db(database_url: str):
    """初始化异步数据库引擎和会话

    Args:
        database_url: 数据库连接字符串（需要是异步URL，如 mysql+aiomysql://...）
    """
    global async_engine, async_session
    import logging

    logger = logging.getLogger(__name__)

    try:
        # 确保使用异步驱动
        if not database_url.startswith(
            "mysql+aiomysql://"
        ) and not database_url.startswith("postgresql+asyncpg://"):
            # 如果不是异步URL，尝试转换
            if database_url.startswith("mysql://"):
                database_url = database_url.replace("mysql://", "mysql+aiomysql://", 1)
            elif database_url.startswith("postgresql://"):
                database_url = database_url.replace(
                    "postgresql://", "postgresql+asyncpg://", 1
                )

        logger.info(f"初始化异步数据库引擎: {database_url}")

        # 创建异步引擎
        async_engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            pool_recycle=1800,
            pool_timeout=30,
        )

        # 创建异步会话工厂
        async_session = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )

        logger.info("✅ 异步数据库引擎初始化成功")

    except Exception as e:
        logger.error(f"❌ 异步数据库引擎初始化失败: {e}")
        raise


async def close_async_db():
    """关闭异步数据库连接"""
    global async_engine
    import logging

    logger = logging.getLogger(__name__)

    if async_engine:
        await async_engine.dispose()
        logger.info("异步数据库连接已关闭")


class WebUIConfig(Base):
    """用户 WebUI 偏好设置"""

    __tablename__ = "webui_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, unique=True, nullable=False)
    theme = Column(String(10), default="light")  # light / dark
    language = Column(String(10), default="zh-CN")
    items_per_page = Column(Integer, default=20)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<WebUIConfig(user_id={self.user_id}, theme={self.theme})>"


class SakuraMemoryState(Base):
    """Sakura 记忆系统状态跟踪 / Sakura memory system state tracking"""

    __tablename__ = "sakura_memory_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name = Column(String(255), unique=True, nullable=False, index=True)

    # 状态跟踪 / State tracking
    reflection_count = Column(Integer, default=0, nullable=False)
    last_consolidation_at = Column(TIMESTAMP, nullable=True)
    last_consolidation_count = Column(
        Integer, nullable=True
    )  # 上次合并时的 reflection_count
    is_initialized = Column(Boolean, default=False, nullable=False)

    # 知识提取状态 / Knowledge extraction state
    knowledge_extracted = Column(Boolean, default=False, nullable=False)

    # 最后写入的文件 SHA / Last written file SHAs
    last_sakura_md_sha = Column(String(40), nullable=True)
    last_memory_md_sha = Column(String(40), nullable=True)

    # 配置覆盖 / Config override
    consolidation_interval = Column(Integer, default=5, nullable=False)

    created_at = Column(
        TIMESTAMP, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        TIMESTAMP,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<SakuraMemoryState(repo={self.repo_full_name}, initialized={self.is_initialized})>"


class SchemaMigration(Base):
    """Schema 迁移记录 / Schema migration log"""

    __tablename__ = "schema_migrations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(50), unique=True, nullable=False)
    applied_at = Column(
        TIMESTAMP,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _get_default_sql(col) -> str | None:
    """获取列的默认值 SQL / Get default value SQL for a column"""
    if col.default is not None and col.default.is_scalar:
        val = col.default.arg
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            escaped = val.replace("'", "''")
            return f"'{escaped}'"
    if col.server_default is not None:
        arg = col.server_default.arg
        if isinstance(arg, (str, int, float)):
            return str(arg)
        return None
    return None


async def _auto_migrate():
    """自动检测并执行 schema 迁移 / Auto-detect and run schema migrations

    用 Inspector 对比 SQLAlchemy 模型定义与数据库实际列，
    自动 ALTER TABLE 添加缺失的列（仅 ADD COLUMN，不做 DROP 或 MODIFY）。
    """
    from sqlalchemy import inspect

    import logging

    _logger = logging.getLogger(__name__)

    if async_engine is None:
        return

    _ensure_model_modules_imported()

    async with async_engine.begin() as conn:
        # 确保 schema_migrations 表存在
        await conn.run_sync(
            lambda sync_conn: SchemaMigration.__table__.create(
                sync_conn, checkfirst=True
            )
        )

        # 用 Inspector 逐表检测缺失列
        def _get_missing_columns(sync_conn):
            insp = inspect(sync_conn)
            missing = []
            for table_cls in Base.__subclasses__():
                table_name = getattr(table_cls, "__tablename__", None)
                if not table_name:
                    continue
                if not insp.has_table(table_name):
                    continue
                db_columns = {col["name"] for col in insp.get_columns(table_name)}
                for col in table_cls.__table__.columns:
                    if col.name not in db_columns:
                        missing.append((table_name, col))
            return missing

        missing = await conn.run_sync(_get_missing_columns)

        if not missing:
            return

        # 执行 ALTER TABLE ADD COLUMN
        for table_name, col in missing:
            col_type = col.type.compile(dialect=async_engine.dialect)
            sql = f"ALTER TABLE `{table_name}` ADD COLUMN `{col.name}` {col_type}"
            if col.nullable:
                sql += " NULL"
            else:
                default = _get_default_sql(col)
                if default:
                    sql += f" NOT NULL DEFAULT {default}"
                else:
                    sql += " NOT NULL"
            await conn.execute(text(sql))
            _logger.info("[auto-migrate] 添加列: {}.{}", table_name, col.name)

        # 记录迁移版本
        version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        await conn.execute(
            text(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (:v, CURRENT_TIMESTAMP)"
            ),
            {"v": version},
        )
        _logger.info("[auto-migrate] 迁移完成, version={}", version)
