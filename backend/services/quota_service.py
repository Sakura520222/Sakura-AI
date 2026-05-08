"""用户配额重置服务"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser


class QuotaService:
    """统一处理 PR 与 Issue 配额重置逻辑。"""

    PR_QUOTA_FIELDS = {
        "daily_used": "daily_used",
        "weekly_used": "weekly_used",
        "monthly_used": "monthly_used",
        "last_reset_daily": "last_reset_daily",
        "last_reset_weekly": "last_reset_weekly",
        "last_reset_monthly": "last_reset_monthly",
    }
    ISSUE_QUOTA_FIELDS = {
        "daily_used": "issue_daily_used",
        "weekly_used": "issue_weekly_used",
        "monthly_used": "issue_monthly_used",
        "last_reset_daily": "last_reset_issue_daily",
        "last_reset_weekly": "last_reset_issue_weekly",
        "last_reset_monthly": "last_reset_issue_monthly",
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _utcnow() -> datetime:
        """返回无时区 UTC 时间，保持与现有 TIMESTAMP 字段兼容。"""
        return datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _week_start(now: datetime) -> datetime:
        # 配额周期统一按 UTC 周一 00:00 计算，与每日 UTC 重置策略保持一致。
        week_start = now - timedelta(days=now.weekday())
        return week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _reset_quota_group(
        user: TelegramUser,
        fields: dict[str, str],
        now: datetime | None = None,
    ) -> bool:
        """按字段映射重置一组日/周/月配额，返回是否发生变更。"""
        now = now or QuotaService._utcnow()
        changed = False

        last_reset_daily = getattr(user, fields["last_reset_daily"])
        if last_reset_daily is None or last_reset_daily.date() < now.date():
            setattr(user, fields["daily_used"], 0)
            setattr(user, fields["last_reset_daily"], now)
            changed = True

        last_reset_weekly = getattr(user, fields["last_reset_weekly"])
        if last_reset_weekly is None:
            setattr(user, fields["weekly_used"], 0)
            setattr(user, fields["last_reset_weekly"], now)
            changed = True
        elif last_reset_weekly.date() < now.date():
            if last_reset_weekly < QuotaService._week_start(now):
                setattr(user, fields["weekly_used"], 0)
                setattr(user, fields["last_reset_weekly"], now)
                changed = True

        last_reset_monthly = getattr(user, fields["last_reset_monthly"])
        if last_reset_monthly is None:
            setattr(user, fields["monthly_used"], 0)
            setattr(user, fields["last_reset_monthly"], now)
            changed = True
        elif (
            last_reset_monthly.month != now.month
            or last_reset_monthly.year != now.year
        ):
            setattr(user, fields["monthly_used"], 0)
            setattr(user, fields["last_reset_monthly"], now)
            changed = True

        return changed

    @staticmethod
    def reset_user_pr_quotas_if_expired(
        user: TelegramUser, now: datetime | None = None
    ) -> bool:
        """重置单个用户过期的 PR 配额，返回是否发生变更。"""
        return QuotaService._reset_quota_group(user, QuotaService.PR_QUOTA_FIELDS, now)

    @staticmethod
    def reset_user_issue_quotas_if_expired(
        user: TelegramUser, now: datetime | None = None
    ) -> bool:
        """重置单个用户过期的 Issue 配额，返回是否发生变更。"""
        return QuotaService._reset_quota_group(
            user, QuotaService.ISSUE_QUOTA_FIELDS, now
        )

    async def reset_user_quotas_if_expired(
        self,
        user: TelegramUser,
        *,
        include_pr: bool = True,
        include_issue: bool = True,
        commit: bool = True,
    ) -> bool:
        """重置单个用户过期配额。"""
        now = self._utcnow()
        changed = False

        if include_pr:
            changed = self.reset_user_pr_quotas_if_expired(user, now) or changed
        if include_issue:
            changed = self.reset_user_issue_quotas_if_expired(user, now) or changed

        if changed and commit:
            await self.session.commit()

        return changed

    async def reset_all_expired_quotas_atomic(self) -> int:
        """使用批量 UPDATE 重置过期配额，返回字段维度更新次数。"""
        now = self._utcnow()
        today = now.date()
        week_start = self._week_start(now)
        reset_count = 0

        statements = (
            update(TelegramUser)
            .where(
                TelegramUser.is_active,
                (TelegramUser.last_reset_daily.is_(None))
                | (
                    TelegramUser.last_reset_daily
                    < datetime.combine(today, datetime.min.time())
                ),
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
                | (
                    TelegramUser.last_reset_issue_daily
                    < datetime.combine(today, datetime.min.time())
                ),
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

        if reset_count:
            await self.session.commit()
        logger.info(f"配额批量原子重置完成，更新字段次数: {reset_count}")
        return reset_count