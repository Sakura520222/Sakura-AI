"""Legacy entitlement accounting and rate-limit headroom.

Periodic limits are consumed first. Exhausted periodic headroom can use a
source's one-time allowance, which is consumed exactly once and never reset.
This changes request admission only; it never bypasses Credits charging.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import now_utc
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    LegacyEntitlementEvent,
    RateLimitAdmission,
)
from backend.models.telegram_models import TelegramUser

FEATURE_FIELDS = {
    "pr_review": (
        "pr",
        "daily_quota",
        "weekly_quota",
        "monthly_quota",
        "daily_used",
        "weekly_used",
        "monthly_used",
    ),
    "issue_analysis": (
        "issue",
        "issue_daily_quota",
        "issue_weekly_quota",
        "issue_monthly_quota",
        "issue_daily_used",
        "issue_weekly_used",
        "issue_monthly_used",
    ),
    "agent": (
        "agent",
        "agent_daily_quota",
        "agent_weekly_quota",
        "agent_monthly_quota",
        "agent_daily_used",
        "agent_weekly_used",
        "agent_monthly_used",
    ),
}
RATE_LIMIT_KEYS = {
    f"{prefix}_{period}"
    for prefix in ("pr", "issue", "agent")
    for period in ("daily", "weekly", "monthly")
} | {"repo_scan_daily"}


class LegacyEntitlementService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def active_conditions(user_id: int, at: datetime | None = None):
        at = at or now_utc()
        return (
            LegacyEntitlement.user_id == user_id,
            LegacyEntitlement.starts_at <= at,
            LegacyEntitlement.revoked_at.is_(None),
            or_(
                LegacyEntitlement.expires_at.is_(None),
                LegacyEntitlement.expires_at > at,
            ),
        )

    async def grant(
        self,
        user_id: int,
        snapshot: dict,
        source_key: str,
        *,
        order_id: int | None = None,
        starts_at: datetime | None = None,
        expires_at: datetime | None = None,
        actor_id: int | None = None,
    ):
        # Caller locks the owning order/user. The unique source key additionally
        # rejects a concurrent duplicate, rolling back the entire entitlement.
        existing = (
            await self.session.execute(
                select(LegacyEntitlement).where(
                    LegacyEntitlement.source_key == source_key
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        limits = dict(snapshot.get("rate_limits") or {})
        for prefix in ("pr", "issue", "agent"):
            for period in ("daily", "weekly", "monthly"):
                key = f"{prefix}_{period}"
                limits[key] = limits.get(key, 0) + snapshot.get(
                    f"{prefix}_{period}_add", 0
                )
        entry = LegacyEntitlement(
            user_id=user_id,
            order_id=order_id,
            source_key=source_key,
            snapshot=snapshot,
            pr_remaining=snapshot.get("pr_quota_bonus", 0),
            issue_remaining=snapshot.get("issue_quota_bonus", 0),
            agent_remaining=snapshot.get("agent_quota_bonus", 0),
            rate_limits=limits,
            starts_at=starts_at or now_utc(),
            expires_at=expires_at,
        )
        self.session.add(entry)
        await self.session.flush()
        self.session.add(
            LegacyEntitlementEvent(
                entitlement_id=entry.id,
                event_key=f"grant:{source_key}",
                kind="grant",
                actor_id=actor_id,
                reason="Purchased plan snapshot",
                detail=snapshot,
            )
        )
        await self.session.flush()
        return entry

    async def effective_limits(
        self, user: TelegramUser, feature: str, *, current_read: bool = False
    ) -> tuple[int, int, int]:
        prefix, *fields = FEATURE_FIELDS[feature]
        statement = select(LegacyEntitlement).where(*self.active_conditions(user.id))
        if current_read:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        entries = (await self.session.execute(statement)).scalars().all()
        return tuple(
            (getattr(user, fields[i]) or 0)
            + sum(
                (entry.rate_limits or {}).get(f"{prefix}_{period}", 0)
                for entry in entries
            )
            for i, period in enumerate(("daily", "weekly", "monthly"))
        )

    async def consume(
        self,
        user: TelegramUser,
        feature: str,
        *,
        repo_name: str,
        number: int,
        event_key: str | None = None,
    ) -> bool:
        event_key = event_key or f"admission:{uuid.uuid4().hex}"
        if not isinstance(event_key, str) or not event_key or len(event_key) > 184:
            raise ValueError("Invalid rate admission event key")
        admitted = (
            await self.session.execute(
                select(RateLimitAdmission)
                .where(RateLimitAdmission.event_key == event_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if admitted:
            if (
                admitted.user_id,
                admitted.feature,
                admitted.repo_name,
                admitted.number,
            ) != (user.id, feature, repo_name, number):
                raise ValueError("Rate admission key conflicts with trusted source")
            return True
        prefix, *fields = FEATURE_FIELDS[feature]
        limits = await self.effective_limits(user, feature, current_read=True)
        used_fields = fields[3:]
        # Conditional UPDATE gives the same concurrent admission guarantees as
        # the old limiter without folding one-time balances into daily limits.
        result = await self.session.execute(
            update(TelegramUser)
            .where(
                TelegramUser.id == user.id,
                *(
                    getattr(TelegramUser, field) < limits[i]
                    for i, field in enumerate(used_fields)
                ),
            )
            .values(
                **{field: getattr(TelegramUser, field) + 1 for field in used_fields}
            )
        )
        if result.rowcount:
            self.session.add(
                RateLimitAdmission(
                    event_key=event_key,
                    user_id=user.id,
                    feature=feature,
                    source_kind="periodic",
                    repo_name=repo_name,
                    number=number,
                )
            )
            await self.session.flush()
            return True
        remaining = getattr(LegacyEntitlement, f"{prefix}_remaining")
        entries = (
            (
                await self.session.execute(
                    select(LegacyEntitlement)
                    .where(*self.active_conditions(user.id), remaining > 0)
                    .order_by(LegacyEntitlement.expires_at.asc(), LegacyEntitlement.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for entry in entries:
            result = await self.session.execute(
                update(LegacyEntitlement)
                .where(
                    LegacyEntitlement.id == entry.id,
                    remaining > 0,
                    *self.active_conditions(user.id),
                )
                .values({remaining.key: remaining - 1})
            )
            if result.rowcount:
                self.session.add(
                    LegacyEntitlementEvent(
                        entitlement_id=entry.id,
                        event_key=f"legacy:{event_key}",
                        kind="consumption",
                        feature=feature,
                        units=-1,
                        detail={"repo_name": repo_name, "number": number},
                    )
                )
                self.session.add(
                    RateLimitAdmission(
                        event_key=event_key,
                        user_id=user.id,
                        feature=feature,
                        source_kind="legacy",
                        entitlement_id=entry.id,
                        repo_name=repo_name,
                        number=number,
                    )
                )
                await self.session.flush()
                return True
        return False

    async def revoke_order(
        self,
        order_id: int,
        *,
        actor_id: int | None = None,
        reason: str = "Order refund",
    ):
        entry = (
            await self.session.execute(
                select(LegacyEntitlement)
                .where(LegacyEntitlement.order_id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not entry or entry.revoked_at:
            return
        entry.revoked_at = now_utc()
        self.session.add(
            LegacyEntitlementEvent(
                entitlement_id=entry.id,
                event_key=f"revoke:order:{order_id}",
                kind="revocation",
                actor_id=actor_id,
                reason=reason,
                detail={
                    "unused_pr": entry.pr_remaining,
                    "unused_issue": entry.issue_remaining,
                    "unused_agent": entry.agent_remaining,
                },
            )
        )
        await self.session.flush()

    async def expire_due(self, user_id: int | None = None) -> int:
        conditions = [
            LegacyEntitlement.expires_at <= now_utc(),
            LegacyEntitlement.revoked_at.is_(None),
        ]
        if user_id is not None:
            conditions.append(LegacyEntitlement.user_id == user_id)
        entries = (
            (
                await self.session.execute(
                    select(LegacyEntitlement).where(*conditions).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for entry in entries:
            entry.revoked_at = now_utc()
            self.session.add(
                LegacyEntitlementEvent(
                    entitlement_id=entry.id,
                    event_key=f"expire:{entry.source_key}",
                    kind="expiration",
                    reason="Purchased period expired",
                    detail={
                        "unused_pr": entry.pr_remaining,
                        "unused_issue": entry.issue_remaining,
                        "unused_agent": entry.agent_remaining,
                    },
                )
            )
        await self.session.flush()
        return len(entries)

    async def remaining(self, user_id: int) -> dict[str, int]:
        result = await self.session.execute(
            select(
                func.sum(LegacyEntitlement.pr_remaining),
                func.sum(LegacyEntitlement.issue_remaining),
                func.sum(LegacyEntitlement.agent_remaining),
            ).where(*self.active_conditions(user_id))
        )
        values = result.one()
        return dict(
            zip(("pr", "issue", "agent"), (value or 0 for value in values), strict=True)
        )
