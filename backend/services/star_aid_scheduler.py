"""仓库互助自动点星调度器 / Star-aid auto-star scheduler.

基于 APScheduler 的 IntervalTrigger 周期性触发 ``StarAidWorker.run_tick``。
成员级别的随机间隔由 ``next_scheduled_at`` 控制，本调度器只负责按时扫描
到期成员。启动与停止挂载在 ``backend.main`` lifespan。
"""

from __future__ import annotations

from loguru import logger

from backend.core.config import get_dynamic_config, get_settings

# 扫描周期（分钟）：多久检查一次到期成员。成员实际节奏由 next_scheduled_at 决定。
_TICK_INTERVAL_MINUTES = 3


class StarAidScheduler:
    """仓库互助自动 star 调度器。"""

    def __init__(self):
        self._scheduler = None
        self._worker = None

    def start(self) -> None:
        settings = get_settings()
        # 启动时检查调度器开关（已在 lifespan 的 load_dynamic_configs 后）
        if not bool(getattr(settings, "star_aid_scheduler_enabled", True)):
            logger.info("star_aid 调度器未启用（star_aid_scheduler_enabled=False）")
            return
        if not bool(getattr(settings, "enable_scheduler", True)):
            logger.info("star_aid 调度器未启用（enable_scheduler=False）")
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger

            from backend.workers.star_aid_worker import StarAidWorker

            self._worker = StarAidWorker()
            self._scheduler = AsyncIOScheduler(
                timezone="Asia/Shanghai",
                job_defaults={"coalesce": True, "max_instances": 1},
            )
            self._scheduler.add_job(
                self._run_tick,
                trigger=IntervalTrigger(minutes=_TICK_INTERVAL_MINUTES),
                id="star_aid_tick",
                name="仓库互助自动点星",
                replace_existing=True,
            )
            self._scheduler.start()
            logger.info(
                "star_aid 调度器已启动，扫描间隔 {} 分钟", _TICK_INTERVAL_MINUTES
            )
        except ImportError:
            logger.warning(
                "APScheduler 未安装，跳过 star_aid 调度器。请安装: pip install APScheduler"
            )
        except Exception as exc:
            logger.error("star_aid 调度器启动失败: {}", exc)

    def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("star_aid 调度器已停止")

    async def _run_tick(self) -> None:
        from backend.services.database_reset_runtime_service import (
            register_current_background_task,
        )

        if register_current_background_task("star_aid_scheduler") is None:
            return
        if self._worker is None:
            return
        # 运行时再次校验动态配置（WebUI 可能在运行中关闭功能）
        if not bool(await get_dynamic_config("star_aid_enabled")):
            return
        if not bool(await get_dynamic_config("star_aid_auto_star_enabled")):
            return
        try:
            await self._worker.run_tick()
        except Exception as exc:
            logger.error("star_aid tick 异常: {}", exc)
