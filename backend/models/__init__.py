"""数据库模型包"""

import logging

from backend.core.config import get_settings
from backend.models.activity_observability_models import (
    ActivityArtifactAccessLog,
    ActivityCanonicalContextRevision,
    ActivityContextOperation,
    ActivityContextSnapshot,
    ActivityEvent,
    ActivityInvocation,
    ActivityInvocationTrigger,
    ActivityInvocationWorkUnit,
    ActivityMessage,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityRoleBindingSnapshot,
    ActivityObservabilitySession,
    ActivityOutbox,
    ActivityPublication,
    ActivityResourceIdentity,
    ActivitySession,
    ActivityThread,
    ActivityThreadLease,
    ActivityToolExecution,
    ActivityTrigger,
    ActivityWorkUnitResult,
)
from backend.models.admin_action_log import AdminActionLog
from backend.models.agent_skill_models import AgentSkill
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.database import (
    AppConfig,
    Base,
    PRReview,
    ReviewComment,
    ReviewQueue,
    UserConfig,
    close_async_db,
    create_tables_async,
    init_async_db,
    insert_default_configs_async,
    migrate_schema_async,
)
from backend.models.payment_models import (
    Order,
    OrderStatus,
    PaymentAction,
    PaymentLog,
    Plan,
    PlanType,
    RedeemCode,
    RedeemCodeStatus,
    RefundRequest,
    RefundRequestStatus,
    SubscriptionStatus,
    UserSubscription,
)
from backend.models.scan_models import (
    FindingCategory,
    FindingSeverity,
    RepoScan,
    ScanFinding,
    ScanStatus,
)
from backend.models.star_aid_models import (
    StarAidActionLog,
    StarAidCredential,
    StarAidMember,
    StarAidRepository,
    StarAidRepositoryMetric,
)
from backend.models.telegram_models import UserRecoveryCode, UserWebAuthnCredential

logger = logging.getLogger(__name__)

__all__ = [
    "AIUsageRecord",
    "ActivityArtifactAccessLog",
    "ActivityCanonicalContextRevision",
    "ActivityContextOperation",
    "ActivityContextSnapshot",
    "ActivityEvent",
    "ActivityInvocation",
    "ActivityInvocationTrigger",
    "ActivityInvocationWorkUnit",
    "ActivityMessage",
    "ActivityModelAttempt",
    "ActivityNativeArtifact",
    "ActivityObservabilityRoleBindingSnapshot",
    "ActivityObservabilitySession",
    "ActivityOutbox",
    "ActivityPublication",
    "ActivityResourceIdentity",
    "ActivitySession",
    "ActivityThread",
    "ActivityThreadLease",
    "ActivityToolExecution",
    "ActivityTrigger",
    "ActivityWorkUnitResult",
    "AdminActionLog",
    "AgentSkill",
    "AppConfig",
    "Base",
    "FindingCategory",
    "FindingSeverity",
    "Order",
    "OrderStatus",
    "PRReview",
    "PaymentAction",
    "PaymentLog",
    "Plan",
    "PlanType",
    "RedeemCode",
    "RedeemCodeStatus",
    "RefundRequest",
    "RefundRequestStatus",
    "RepoScan",
    "ReviewComment",
    "ReviewQueue",
    "ScanFinding",
    "ScanStatus",
    "SecurityEventLog",
    "StarAidActionLog",
    "StarAidCredential",
    "StarAidMember",
    "StarAidRepository",
    "StarAidRepositoryMetric",
    "SubscriptionStatus",
    "UserConfig",
    "UserRecoveryCode",
    "UserSubscription",
    "UserWebAuthnCredential",
    "close_async_db",
    "init_async_db",
    "init_database",
    "init_db",
]

settings = get_settings()


async def init_db():
    """初始化数据库（完全异步）

    在应用启动时调用，自动创建数据库表、插入默认配置并初始化异步引擎
    """
    try:
        logger.info("正在初始化数据库...")
        logger.info("数据库连接地址已配置")

        # 1. 先初始化异步数据库引擎
        init_async_db(settings.database_url)

        # 2. 异步创建所有表
        await create_tables_async()

        # 3. 自动迁移（检测缺失列并添加）
        await migrate_schema_async()

        # 4. 异步插入默认配置
        await insert_default_configs_async()

        logger.info("✅ 数据库初始化成功")

    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")
        raise
