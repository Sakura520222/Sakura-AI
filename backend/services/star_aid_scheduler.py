"""仓库互助自动点星调度器 / Star-aid auto-star scheduler.

基于 APScheduler 的 IntervalTrigger 周期性触发 ``StarAidWorker.run_tick``。
成员级别的随机间隔由 ``next_scheduled_at`` 控制，本调度器只负责按时扫描
到期成员。启动与停止挂载在 ``backend.main`` lifespan，并支持运行时动态启停。
"""

from __future__ import annotations

import threading

from loguru import logger

from backend.core.config import get_dynamic_config, get_settings
from backend.core.time_service import get_time_service

# 扫描周期（分钟）：多久检查一次到期成员。成员实际节奏由 next_scheduled_at 决定。
_TICK_INTERVAL_MINUTES = 3


class StarAidScheduler:
    """仓库互助自动 star 调度器。"""

    def __init__(self):
        self._scheduler = None
        self._worker = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """判断调度器是否正在运行。"""
        with self._lock:
            return bool(self._scheduler and self._scheduler.running)

    def start(self) -> None:
        """启动调度器（幂等操作）。"""
        with self._lock:
            if self._scheduler and self._scheduler.running:
                logger.debug("star_aid 调度器已在运行中，跳过重复启动")
                return

            settings = get_settings()
            # Keep the lightweight tick registered even while Star Aid is
            # disabled: every replica must observe a later DB-backed enable.
            if not bool(getattr(settings, "enable_scheduler", True)):
                logger.info("star_aid 调度器未启用（enable_scheduler=False）")
                return

            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler
                from apscheduler.triggers.interval import IntervalTrigger

                from backend.workers.star_aid_worker import StarAidWorker

                if self._worker is None:
                    self._worker = StarAidWorker()

                self._scheduler = AsyncIOScheduler(
                    timezone=get_time_service().zone,
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
        """停止调度器（幂等操作）。"""
        with self._lock:
            if self._scheduler and self._scheduler.running:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception as exc:
                    logger.warning("star_aid 调度器关闭异常: {}", exc)
                self._scheduler = None
                logger.info("star_aid 调度器已停止")
            else:
                self._scheduler = None

    def restart_if_needed(self) -> None:
        """Only the process-wide scheduler switch controls the timer lifecycle."""
        settings = get_settings()
        enabled = bool(getattr(settings, "enable_scheduler", True))
        if enabled:
            if not self.is_running:
                self.start()
        else:
            if self.is_running:
                self.stop()

    async def _run_tick(self) -> None:
        from backend.services.database_reset_runtime_service import (
            register_current_background_task,
        )

        if register_current_background_task("star_aid_scheduler") is None:
            return
        if self._worker is None:
            return
        # Bypass this process's config TTL so a remote replica's toggle is
        # observed on its next tick even if it started while disabled.
        if not bool(await get_dynamic_config("star_aid_scheduler_enabled", fresh=True)):
            logger.debug("star_aid tick skipped: star_aid_scheduler_enabled is False")
            return
        if not bool(await get_dynamic_config("star_aid_enabled")):
            return
        if not bool(await get_dynamic_config("star_aid_auto_star_enabled")):
            return
        try:
            await self._worker.run_tick()
        except Exception as exc:
            logger.error("star_aid tick 异常: {}", exc)


_star_aid_scheduler_instance: StarAidScheduler | None = None


def get_star_aid_scheduler() -> StarAidScheduler | None:
    return _star_aid_scheduler_instance


def set_star_aid_scheduler(scheduler: StarAidScheduler | None) -> None:
    global _star_aid_scheduler_instance
    _star_aid_scheduler_instance = scheduler
