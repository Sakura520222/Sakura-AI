"""仓库互助自动点星 worker / Star-aid auto-star worker.

每轮调度（由 ``StarAidScheduler`` 触发）：

1. 读取动态配置校验功能 / 自动 star 开关。
2. 查询 ``status='active'`` 且 ``next_scheduled_at<=now`` 的成员，最多
   ``star_aid_batch_size`` 个。
3. 对每个成员：按需重置每日用量，随机选 1-3 个目标仓库（排除自己 owner、
   已 star、仓库每日上限超限），调用 ``star_aid_service.perform_star``。
4. 根据结果设置下一次 ``next_scheduled_at``：
   - rate limit → reset 时间 + 随机 jitter
   - reauth → 成员已被置 reauth_required，不再调度
   - 其它 → now + random(min_interval, max_interval)
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from backend.core.config import get_dynamic_config
from backend.models.database import async_session
from backend.models.star_aid_models import (
    ACTION_MANUAL_STAR,
    ACTION_STAR,
    ACTION_STATUS_ALREADY_DONE,
    ACTION_STATUS_SUCCESS,
    MEMBER_STATUS_ACTIVE,
    StarAidActionLog,
    StarAidMember,
    StarAidRepository,
)
from backend.services import star_aid_service

# 单成员每轮最多尝试的目标数
_MAX_TARGETS_PER_MEMBER = 3


class StarAidWorker:
    """单轮自动 star 执行器。"""

    async def run_tick(self) -> None:
        if async_session is None:
            logger.debug("star_aid tick skipped: db session not ready")
            return
        if not await star_aid_service.is_feature_enabled():
            return
        if not await star_aid_service.is_auto_star_enabled():
            return

        batch_size = int(await get_dynamic_config("star_aid_batch_size") or 5)
        now = datetime.utcnow()
        async with async_session() as session:
            result = await session.execute(
                select(StarAidMember.id)
                .where(
                    StarAidMember.status == MEMBER_STATUS_ACTIVE,
                    StarAidMember.auto_star_enabled.is_(True),
                    StarAidMember.next_scheduled_at <= now,
                )
                .limit(max(batch_size, 1))
            )
            member_ids = [int(row[0]) for row in result.all()]

        if not member_ids:
            return
        logger.info("star_aid tick: {} active member(s) due", len(member_ids))
        for member_id in member_ids:
            try:
                await self._process_member(member_id)
            except Exception as exc:
                logger.error(
                    "star_aid process member failed: member_id={}, error={}",
                    member_id,
                    exc,
                )

    async def _process_member(self, member_id: int) -> None:
        async with async_session() as session:
            member = await session.get(StarAidMember, member_id)
            if member is None or member.status != MEMBER_STATUS_ACTIVE:
                return
            now = datetime.utcnow()

            if self._needs_daily_reset(member):
                member.daily_star_used = 0
                member.last_daily_reset_at = now

            targets = await self._select_targets(
                session, member, count=random.randint(1, _MAX_TARGETS_PER_MEMBER)
            )

            rate_reset_at = None
            reauth = False
            for repo_id in targets:
                result = await star_aid_service.perform_star(
                    session,
                    actor_user_id=member.user_id,
                    repository_id=repo_id,
                    trigger="scheduler",
                    enforce_daily_limit=True,
                )
                if result.get("reauth_required"):
                    reauth = True
                    break
                if result.get("rate_limited") and result.get("rate_limit_reset_at"):
                    rate_reset_at = result["rate_limit_reset_at"]
                    break

            min_interval = int(
                await get_dynamic_config("star_aid_min_interval_minutes") or 15
            )
            max_interval = int(
                await get_dynamic_config("star_aid_max_interval_minutes") or 180
            )
            lo = min(min_interval, max_interval)
            hi = max(min_interval, max_interval, lo)

            member.last_scheduled_at = now
            if not reauth:
                if rate_reset_at is not None:
                    member.next_scheduled_at = rate_reset_at + timedelta(
                        seconds=random.randint(30, 300)
                    )
                else:
                    member.next_scheduled_at = now + timedelta(
                        minutes=random.randint(lo, hi)
                    )
            await session.commit()

    def _needs_daily_reset(self, member: StarAidMember) -> bool:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        last = member.last_daily_reset_at
        return last is None or last < today

    async def _select_targets(
        self, session, member: StarAidMember, *, count: int
    ) -> list[int]:
        """挑选可 star 的目标仓库 id 列表。"""
        result = await session.execute(
            select(StarAidRepository.id).where(
                StarAidRepository.is_displayed.is_(True),
                StarAidRepository.disabled_by_admin.is_(False),
                StarAidRepository.is_public.is_(True),
                StarAidRepository.is_archived.is_(False),
                StarAidRepository.owner_user_id != member.user_id,
            )
        )
        all_ids = [int(row[0]) for row in result.all()]
        if not all_ids:
            return []

        # 排除该 actor 已 star 的仓库
        starred_result = await session.execute(
            select(StarAidActionLog.target_repository_id).where(
                StarAidActionLog.actor_user_id == member.user_id,
                StarAidActionLog.action.in_([ACTION_STAR, ACTION_MANUAL_STAR]),
                StarAidActionLog.status.in_(
                    [ACTION_STATUS_SUCCESS, ACTION_STATUS_ALREADY_DONE]
                ),
                StarAidActionLog.target_repository_id.in_(all_ids),
            )
        )
        starred_set = {int(row[0]) for row in starred_result.all()}
        candidates = [rid for rid in all_ids if rid not in starred_set]
        if not candidates:
            return []

        # 仓库每日新增自动 star 上限
        repo_limit_raw = await get_dynamic_config("star_aid_repo_daily_limit")
        repo_limit = int(repo_limit_raw if repo_limit_raw is not None else 50)
        if repo_limit <= 0:
            return []
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        counts_result = await session.execute(
            select(
                StarAidActionLog.target_repository_id,
                func.count(StarAidActionLog.id),
            )
            .where(
                StarAidActionLog.target_repository_id.in_(candidates),
                StarAidActionLog.action.in_([ACTION_STAR, ACTION_MANUAL_STAR]),
                StarAidActionLog.status == ACTION_STATUS_SUCCESS,
                StarAidActionLog.created_star.is_(True),
                StarAidActionLog.created_at >= today_start,
            )
            .group_by(StarAidActionLog.target_repository_id)
        )
        count_map = {int(row[0]): int(row[1]) for row in counts_result.all()}
        candidates = [
            rid
            for rid in candidates
            if star_aid_service.repo_daily_limit_allows(
                repo_limit, current_count=count_map.get(rid, 0)
            )
        ]
        if not candidates:
            return []

        random.shuffle(candidates)
        return candidates[: max(count, 1)]
