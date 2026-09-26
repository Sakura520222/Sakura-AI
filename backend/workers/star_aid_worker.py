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

import asyncio
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from backend.core.config import get_dynamic_config
from backend.core.time_service import (
    format_rfc3339,
    get_time_service,
    local_date,
    now_utc,
    parse_rfc3339,
    start_of_local_day,
)
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

# Redis cooldown 键名
_REDIS_COOLDOWN_KEY = "star_aid:worker:cooldown"

# Both the value and expiration are replaced in one operation only when the
# new UTC deadline extends the current one. format_rfc3339 emits fixed-width
# microseconds, so its UTC timestamps compare lexicographically.
_EXTEND_COOLDOWN_LUA = """
local current = redis.call('GET', KEYS[1])
if not current or current < ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return ARGV[1]
end
return current
"""

# 进程内内存 cooldown 时间戳（aware UTC）
_in_process_cooldown_until: datetime | None = None
_coordination_failed_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class CooldownState:
    """Shared cooldown read result and coordination health."""

    until: datetime | None
    shared_state_healthy: bool


class StarAidWorker:
    """单轮自动 star 执行器。"""

    @classmethod
    async def get_cooldown_state(cls) -> CooldownState:
        now = now_utc()
        redis_until = None
        shared_state_healthy = not (
            _coordination_failed_until and _coordination_failed_until > now
        )
        # 1. 优先检查 Redis 中的全局冷却时间
        try:
            from backend.core.redis import get_async_redis

            r = await get_async_redis()
            val = await r.get(_REDIS_COOLDOWN_KEY)
            if val:
                redis_until = parse_rfc3339(val)
        except Exception:
            shared_state_healthy = False
            logger.warning(
                "star_aid shared cooldown read failed; failing closed for this tick",
                exc_info=True,
            )

        # A local extension must still be honored if Redis became unavailable
        # before the write, or contains an older deadline.
        until = max(
            (candidate for candidate in (redis_until, _in_process_cooldown_until) if candidate),
            default=None,
        )
        return CooldownState(
            until=until if until and until > now else None,
            shared_state_healthy=shared_state_healthy,
        )

    @classmethod
    async def get_cooldown_until(cls) -> datetime | None:
        """Compatibility accessor returning only the active deadline."""
        return (await cls.get_cooldown_state()).until

    @classmethod
    async def set_cooldown_until(cls, until: datetime) -> bool:
        global _coordination_failed_until
        global _in_process_cooldown_until
        if _in_process_cooldown_until is None or until > _in_process_cooldown_until:
            _in_process_cooldown_until = until
        now = now_utc()
        ttl = max(1, math.ceil((until - now).total_seconds()))
        try:
            from backend.core.redis import get_async_redis

            r = await get_async_redis()
            effective = await r.eval(
                _EXTEND_COOLDOWN_LUA, 1, _REDIS_COOLDOWN_KEY,
                format_rfc3339(until), ttl,
            )
            effective_until = parse_rfc3339(effective)
            _in_process_cooldown_until = max(_in_process_cooldown_until, effective_until)
            if _coordination_failed_until is None or effective_until >= _coordination_failed_until:
                _coordination_failed_until = None
            return True
        except Exception:
            if _coordination_failed_until is None or until > _coordination_failed_until:
                _coordination_failed_until = until
            logger.warning(
                "star_aid global cooldown persistence failed: until={} "
                "current replica will pause, but cross-replica coordination is unavailable",
                until,
                exc_info=True,
            )
            return False

    async def run_tick(self) -> None:
        if async_session is None:
            logger.debug("star_aid tick skipped: db session not ready")
            return
        if not await star_aid_service.is_feature_enabled():
            return
        if not await star_aid_service.is_auto_star_enabled():
            return

        cooldown_state = await self.get_cooldown_state()
        if not cooldown_state.shared_state_healthy:
            logger.warning(
                "star_aid tick skipped: shared cooldown state is unavailable"
            )
            return
        cooldown_until = cooldown_state.until
        now = now_utc()
        if cooldown_until and cooldown_until > now:
            logger.warning("star_aid tick skipped: worker in cooldown until {}", cooldown_until)
            return

        batch_size = int(await get_dynamic_config("star_aid_batch_size") or 5)
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
                short_circuit = await self._process_member(member_id)
                if short_circuit:
                    logger.warning("star_aid tick: aborting remaining batch members due to rate limit")
                    break
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
            now = now_utc()

            if self._needs_daily_reset(member):
                member.daily_star_used = 0
                member.last_daily_reset_at = now

            targets = await self._select_targets(
                session, member, count=random.randint(1, _MAX_TARGETS_PER_MEMBER)
            )

            rate_reset_at = None
            reauth = False
            hit_rate_limit = False
            rate_limit_kind = None
            for repo_id in targets:
                # Preserve the gap between attempts across member boundaries.
                if getattr(self, "_last_star_attempt_finished", False):
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                try:
                    result = await star_aid_service.perform_star(
                        session,
                        actor_user_id=member.user_id,
                        repository_id=repo_id,
                        trigger="scheduler",
                        enforce_daily_limit=True,
                    )
                finally:
                    # The attempt may have reached GitHub before raising.
                    self._last_star_attempt_finished = True
                if result.get("reauth_required"):
                    reauth = True
                    break
                if result.get("rate_limited"):
                    rate_reset_at = result.get("rate_limit_reset_at")
                    rate_limit_kind = (
                        result.get("rate_limit_kind") or "primary"
                    )
                    if rate_reset_at is None:
                        rate_reset_at = now + timedelta(seconds=60)
                    if rate_limit_kind == "secondary":
                        # Secondary limits can apply to the shared API entry
                        # point, so pause this worker and later batch members.
                        hit_rate_limit = True
                        await self.set_cooldown_until(rate_reset_at)
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
            return hit_rate_limit

    def _needs_daily_reset(self, member: StarAidMember) -> bool:
        service = get_time_service()
        today = start_of_local_day(local_date(now_utc(), service.zone), service.zone)
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
        service = get_time_service()
        today_start = start_of_local_day(
            local_date(now_utc(), service.zone), service.zone
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
