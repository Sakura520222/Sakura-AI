"""数据库模型包"""

from backend.models.database import (
    PRReview,
    ReviewComment,
    AppConfig,
    UserConfig,
    ReviewQueue,
    init_database,
    init_async_db,
    close_async_db,
    create_tables_async,
    insert_default_configs_async,
    Base,
)
from backend.models.admin_action_log import AdminActionLog
from backend.models.scan_models import (
    RepoScan,
    ScanFinding,
    ScanStatus,
    FindingSeverity,
    FindingCategory,
)
from backend.models.payment_models import (
    Plan,
    PlanType,
    Order,
    OrderStatus,
    RedeemCode,
    RedeemCodeStatus,
    UserSubscription,
    SubscriptionStatus,
    PaymentLog,
    PaymentAction,
)
from backend.core.config import get_settings
import logging

logger = logging.getLogger(__name__)

__all__ = [
    "PRReview",
    "ReviewComment",
    "AppConfig",
    "UserConfig",
    "ReviewQueue",
    "AdminActionLog",
    "Plan",
    "PlanType",
    "Order",
    "OrderStatus",
    "RedeemCode",
    "RedeemCodeStatus",
    "UserSubscription",
    "SubscriptionStatus",
    "PaymentLog",
    "PaymentAction",
    "RepoScan",
    "ScanFinding",
    "ScanStatus",
    "FindingSeverity",
    "FindingCategory",
    "init_database",
    "init_async_db",
    "close_async_db",
    "Base",
    "init_db",
]

settings = get_settings()


async def init_db():
    """初始化数据库（完全异步）

    在应用启动时调用，自动创建数据库表、插入默认配置并初始化异步引擎
    """
    try:
        logger.info("正在初始化数据库...")
        logger.info(f"数据库地址: {settings.database_url}")

        # 1. 先初始化异步数据库引擎
        init_async_db(settings.database_url)

        # 2. 异步创建所有表
        await create_tables_async()

        # 3. 自动迁移（检测缺失列并添加）
        from backend.models.database import _auto_migrate

        await _auto_migrate()

        # 4. 异步插入默认配置
        await insert_default_configs_async()

        logger.info("✅ 数据库初始化成功")

    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")
        raise
