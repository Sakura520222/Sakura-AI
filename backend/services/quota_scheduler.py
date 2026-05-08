"""配额定时重置调度器"""

from loguru import logger

from backend.core.config import get_settings


class QuotaResetScheduler:
    """每天 00:00 UTC 批量重置过期用户配额。"""

    def __init__(self):
        self._scheduler = None

    def start(self):
        """启动配额重置调度器。"""
        settings = get_settings()
        if not settings.enable_scheduler:
            logger.info("配额重置调度器未启用（enable_scheduler=False）")
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            # 配额统计按 UTC 自然日/周/月结算，避免部署机器本地时区影响重置时间。
            self._scheduler = AsyncIOScheduler(
                timezone="UTC",
                job_defaults={"coalesce": True, "max_instances": 1},
            )
            self._scheduler.add_job(
                self._run_quota_reset,
                trigger=CronTrigger(hour=0, minute=0),
                id="quota_reset_daily",
                name="用户配额每日重置",
                replace_existing=True,
            )
            self._scheduler.start()
            logger.info("✅ 配额重置调度器已启动，每天 00:00 UTC 执行")
        except ImportError:
            logger.warning(
                "APScheduler 未安装，跳过配额重置调度器启动。请安装: pip install APScheduler"
            )
        except Exception as e:
            logger.error(f"❌ 配额重置调度器启动失败: {e}")

    def stop(self):
        """停止配额重置调度器。"""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("✅ 配额重置调度器已停止")

    async def _run_quota_reset(self):
        """定时批量重置入口。"""
        try:
            # 延迟导入避免模块初始化时数据库会话工厂尚未完成引导，造成循环依赖。
            from backend.models.database import async_session
            from backend.services.quota_service import QuotaService

            if async_session is None:
                logger.warning("配额重置跳过：数据库会话工厂未初始化")
                return

            async with async_session() as session:
                reset_count = await QuotaService(session).reset_all_expired_quotas_atomic()
            logger.info(f"✅ 定时配额重置完成，字段维度更新次数: {reset_count}")
        except Exception as e:
            logger.error(f"❌ 定时配额重置异常: {e}", exc_info=True)