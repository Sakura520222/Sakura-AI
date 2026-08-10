"""仓库定时扫描调度器"""

from loguru import logger

from backend.core.config import get_settings


class ScanScheduler:
    """仓库定时扫描调度器（基于 APScheduler）"""

    def __init__(self):
        self._scheduler = None
        self._worker = None

    def start(self):
        """启动调度器"""
        settings = get_settings()

        if not settings.enable_repo_scan:
            logger.info("仓库扫描未启用（enable_repo_scan=False）")
            return

        if not settings.enable_scheduler:
            logger.info("调度器未启用（enable_scheduler=False）")
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger

            from backend.workers.scan_worker import ScanWorker

            self._worker = ScanWorker()
            self._scheduler = AsyncIOScheduler(
                timezone="Asia/Shanghai",
                job_defaults={"coalesce": True, "max_instances": 1},
            )

            interval_minutes = settings.scan_interval_minutes
            self._scheduler.add_job(
                self._run_scheduled_scan,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id="repo_scan",
                name="仓库定时扫描",
                replace_existing=True,
            )

            self._scheduler.start()
            logger.info(f"✅ 仓库扫描调度器已启动，间隔: {interval_minutes} 分钟")

        except ImportError:
            logger.warning(
                "APScheduler 未安装，跳过扫描调度器启动。请安装: pip install APScheduler"
            )
        except Exception as e:
            logger.error(f"❌ 扫描调度器启动失败: {e}")

    def stop(self):
        """停止调度器"""
        if self._scheduler and self._scheduler.running:
            # APScheduler's AsyncIO executor cancels pending jobs when shutdown
            # is requested. ``wait=True`` is required here so a reset cannot
            # cross into DROP while an already-started scan still owns a DB
            # session; the runtime supervisor then awaits the coroutine task.
            self._scheduler.shutdown(wait=True)
            logger.info("✅ 仓库扫描调度器已停止")

    async def _run_scheduled_scan(self):
        """定时扫描入口"""
        from backend.services.database_reset_runtime_service import (
            register_current_background_task,
        )

        if register_current_background_task("scan_scheduler") is None:
            return
        try:
            logger.info("🕐 定时扫描任务触发")
            result = await self._worker.get_scan_candidates()
            candidates = result["candidates"]

            if not candidates:
                total_active = result["total_active"]
                if total_active == 0:
                    logger.info("无可扫描仓库（无活跃订阅），跳过本轮")
                else:
                    logger.info(
                        f"无可扫描仓库（{result['cooldown_count']}/{total_active} 个在冷却期内），跳过本轮"
                    )
                return

            logger.info(f"本轮扫描候选仓库: {len(candidates)} 个")

            for repo_name in candidates:
                try:
                    scan_id = await self._worker.create_scan_record(
                        repo_name=repo_name,
                        trigger_type="scheduled",
                    )
                    await self._worker.process_scan(scan_id)
                except Exception as e:
                    logger.error(f"扫描仓库 {repo_name} 失败: {e}")

            logger.info("✅ 本轮定时扫描任务完成")

        except Exception as e:
            logger.error(f"❌ 定时扫描调度异常: {e}", exc_info=True)
