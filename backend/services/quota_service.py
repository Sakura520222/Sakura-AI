"""用户配额重置服务"""

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser


class QuotaService:
    """统一处理 PR 与 Issue 配额重置逻辑。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _week_start(now: datetime) -> datetime:
        week_start = now - timedelta(days=now.weekday())
        return week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def reset_user_pr_quotas_if_expired(user: TelegramUser, now: datetime | None = None) -> bool:
        """重置单个用户过期的 PR 配额，返回是否发生变更。"""
        now = now or datetime.utcnow()
        changed = False

        if user.last_reset_daily is None or user.last_reset_daily.date() < now.date():
            user.daily_used = 0
            user.last_reset_daily = now
            changed = True

        if user.last_reset_weekly is None:
            user.weekly_used = 0
            user.last_reset_weekly = now
            changed = True
        elif user.last_reset_weekly.date() < now.date():
            if user.last_reset_weekly < QuotaService._week_start(now):
                user.weekly_used = 0
                user.last_reset_weekly = now
                changed = True

        if user.last_reset_monthly is None:
            user.monthly_used = 0
            user.last_reset_monthly = now
            changed = True
        elif (
            user.last_reset_monthly.month != now.month
            or user.last_reset_monthly.year != now.year
        ):
            user.monthly_used = 0
            user.last_reset_monthly = now
            changed = True

        return changed

    @staticmethod
    def reset_user_issue_quotas_if_expired(user: TelegramUser, now: datetime | None = None) -> bool:
        """重置单个用户过期的 Issue 配额，返回是否发生变更。"""
        now = now or datetime.utcnow()
        changed = False

        if (
            user.last_reset_issue_daily is None
            or user.last_reset_issue_daily.date() < now.date()
        ):
            user.issue_daily_used = 0
            user.last_reset_issue_daily = now
            changed = True

        if user.last_reset_issue_weekly is None:
            user.issue_weekly_used = 0
            user.last_reset_issue_weekly = now
            changed = True
        elif user.last_reset_issue_weekly.date() < now.date():
            if user.last_reset_issue_weekly < QuotaService._week_start(now):
                user.issue_weekly_used = 0
                user.last_reset_issue_weekly = now
                changed = True

        if user.last_reset_issue_monthly is None:
            user.issue_monthly_used = 0
            user.last_reset_issue_monthly = now
            changed = True
        elif (
            user.last_reset_issue_monthly.month != now.month
            or user.last_reset_issue_monthly.year != now.year
        ):
            user.issue_monthly_used = 0
            user.last_reset_issue_monthly = now
            changed = True

        return changed

    async def reset_user_quotas_if_expired(
        self,
        user: TelegramUser,
        *,
        include_pr: bool = True,
        include_issue: bool = True,
        commit: bool = True,
    ) -> bool:
        """重置单个用户过期配额。"""
        now = datetime.utcnow()
        changed = False

        if include_pr:
            changed = self.reset_user_pr_quotas_if_expired(user, now) or changed
        if include_issue:
            changed = self.reset_user_issue_quotas_if_expired(user, now) or changed

        if changed and commit:
            await self.session.commit()

        return changed

    async def reset_all_expired_quotas(self) -> int:
        """批量重置所有活跃用户的过期配额，返回发生重置的用户数。"""
        result = await self.session.execute(
            select(TelegramUser).where(TelegramUser.is_active)
        )
        users = result.scalars().all()

        reset_count = 0
        now = datetime.utcnow()
        for user in users:
            changed = self.reset_user_pr_quotas_if_expired(user, now)
            changed = self.reset_user_issue_quotas_if_expired(user, now) or changed
            if changed:
                reset_count += 1

        if reset_count:
            await self.session.commit()

        logger.info(f"配额批量重置完成，更新用户数: {reset_count}")
        return reset_count

    async def reset_all_expired_quotas_atomic(self) -> int:
        """使用批量 UPDATE 重置所有活跃用户的过期配额。"""
        now = datetime.utcnow()
        today = now.date()
        week_start = self._week_start(now)
        reset_count = 0

        statements = (
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_daily.is_(None))
                | (TelegramUser.last_reset_daily < datetime.combine(today, datetime.min.time())),
            )
            .values(daily_used=0, last_reset_daily=now),
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_weekly.is_(None))
                | (TelegramUser.last_reset_weekly < week_start),
            )
            .values(weekly_used=0, last_reset_weekly=now),
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_monthly.is_(None))
                | (TelegramUser.last_reset_monthly < datetime(now.year, now.month, 1)),
            )
            .values(monthly_used=0, last_reset_monthly=now),
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_issue_daily.is_(None))
                | (TelegramUser.last_reset_issue_daily < datetime.combine(today, datetime.min.time())),
            )
            .values(issue_daily_used=0, last_reset_issue_daily=now),
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_issue_weekly.is_(None))
                | (TelegramUser.last_reset_issue_weekly < week_start),
            )
            .values(issue_weekly_used=0, last_reset_issue_weekly=now),
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_issue_monthly.is_(None))
                | (TelegramUser.last_reset_issue_monthly < datetime(now.year, now.month, 1)),
            )
            .values(issue_monthly_used=0, last_reset_issue_monthly=now),
        )

        for stmt in statements:
            result = await self.session.execute(stmt)
            reset_count += max(result.rowcount or 0, 0)

        await self.session.commit()
        logger.info(f"配额批量原子重置完成，更新字段次数: {reset_count}")
        return reset_count