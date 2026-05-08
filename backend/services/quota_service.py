"""用户配额重置服务"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser


@dataclass(frozen=True)
class QuotaResetResult:
    """批量配额重置结果。

    affected_users: 至少有一个配额周期被重置的去重用户数。
    affected_fields: 所有 UPDATE 语句 rowcount 之和，表示字段维度影响行数；
        例如同一用户的 PR 日配额和 Issue 日配额同时重置时会计为 2。
    """

    affected_users: int = 0
    affected_fields: int = 0


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
        """返回无时区 UTC 时间，保持与现有 TIMESTAMP 字段兼容。

        TODO: 迁移到 timezone-aware TIMESTAMP 列后移除 replace(tzinfo=None)。
        """
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
        if last_reset_weekly is None or last_reset_weekly < QuotaService._week_start(now):
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

    async def reset_all_expired_quotas_atomic(self) -> QuotaResetResult:
        """使用批量 UPDATE 重置过期配额，返回结构化统计。

        affected_fields 是 6 条 UPDATE 语句 rowcount 的累加值，不是去重用户数；
        例如 3 个用户同时重置 PR/Issue 日/周/月配额时，最大可能返回 18。
        """
        now = self._utcnow()
        today = now.date()
        week_start = self._week_start(now)
        affected_fields = 0
        affected_user_ids: set[int] = set()

        daily_cutoff = datetime.combine(today, datetime.min.time())
        monthly_cutoff = datetime(now.year, now.month, 1)
        reset_specs = (
            (
                (TelegramUser.last_reset_daily.is_(None))
                | (TelegramUser.last_reset_daily < daily_cutoff),
                {"daily_used": 0, "last_reset_daily": now},
            ),
            (
                (TelegramUser.last_reset_weekly.is_(None))
                | (TelegramUser.last_reset_weekly < week_start),
                {"weekly_used": 0, "last_reset_weekly": now},
            ),
            (
                (TelegramUser.last_reset_monthly.is_(None))
                | (TelegramUser.last_reset_monthly < monthly_cutoff),
                {"monthly_used": 0, "last_reset_monthly": now},
            ),
            (
                (TelegramUser.last_reset_issue_daily.is_(None))
                | (TelegramUser.last_reset_issue_daily < daily_cutoff),
                {"issue_daily_used": 0, "last_reset_issue_daily": now},
            ),
            (
                (TelegramUser.last_reset_issue_weekly.is_(None))
                | (TelegramUser.last_reset_issue_weekly < week_start),
                {"issue_weekly_used": 0, "last_reset_issue_weekly": now},
            ),
            (
                (TelegramUser.last_reset_issue_monthly.is_(None))
                | (TelegramUser.last_reset_issue_monthly < monthly_cutoff),
                {"issue_monthly_used": 0, "last_reset_issue_monthly": now},
            ),
        )

        for where_clause, values in reset_specs:
            id_result = await self.session.execute(
                select(TelegramUser.id).where(TelegramUser.is_active, where_clause)
            )
            affected_user_ids.update(id_result.scalars().all())
            stmt = (
                update(TelegramUser)
                .where(TelegramUser.is_active, where_clause)
                .values(**values)
            )
            result = await self.session.execute(stmt)
            affected_fields += max(result.rowcount or 0, 0)

        if affected_fields:
            await self.session.commit()
        reset_result = QuotaResetResult(
            affected_users=len(affected_user_ids),
            affected_fields=affected_fields,
        )
        logger.info(
            "配额批量原子重置完成，影响用户数: "
            f"{reset_result.affected_users}, 字段维度影响行数: {reset_result.affected_fields}"
        )
        return reset_result