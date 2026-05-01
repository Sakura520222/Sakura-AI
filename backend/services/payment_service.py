"""付费配额核心服务"""

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select, and_, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models.payment_models import (
    Plan,
    PlanType,
    Order,
    OrderStatus,
    RedeemCode,
    RedeemCodeStatus,
    UserSubscription,
    SubscriptionStatus,
    PaymentLog,
    PaymentAction,
)
from backend.models.telegram_models import TelegramUser
from backend.core.config import get_settings

settings = get_settings()


class PaymentError(Exception):
    """支付业务异常"""

    pass


class PaymentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ========== 套餐管理 ==========

    async def create_plan(
        self,
        name: str,
        plan_type: str,
        price_cents: int,
        currency: str = "CNY",
        duration_days: Optional[int] = None,
        pr_quota_bonus: int = 0,
        pr_daily_add: int = 0,
        pr_weekly_add: int = 0,
        pr_monthly_add: int = 0,
        issue_quota_bonus: int = 0,
        issue_daily_add: int = 0,
        issue_weekly_add: int = 0,
        issue_monthly_add: int = 0,
        description: Optional[str] = None,
        sort_order: int = 0,
    ) -> Plan:
        plan = Plan(
            name=name,
            plan_type=plan_type,
            price_cents=price_cents,
            currency=currency,
            duration_days=duration_days,
            pr_quota_bonus=pr_quota_bonus,
            pr_daily_add=pr_daily_add,
            pr_weekly_add=pr_weekly_add,
            pr_monthly_add=pr_monthly_add,
            issue_quota_bonus=issue_quota_bonus,
            issue_daily_add=issue_daily_add,
            issue_weekly_add=issue_weekly_add,
            issue_monthly_add=issue_monthly_add,
            description=description,
            sort_order=sort_order,
        )
        self.session.add(plan)
        await self.session.flush()
        logger.info(f"Created plan: {name} (type={plan_type}, price={price_cents})")
        return plan

    async def update_plan(self, plan_id: int, **kwargs) -> Plan:
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")
        for key, value in kwargs.items():
            if hasattr(plan, key) and value is not None:
                setattr(plan, key, value)
        await self.session.flush()
        return plan

    async def list_plans(self, active_only: bool = False) -> List[Plan]:
        stmt = select(Plan).order_by(Plan.sort_order, Plan.id)
        if active_only:
            stmt = stmt.where(Plan.is_active == True)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_plan(self, plan_id: int) -> Optional[Plan]:
        stmt = select(Plan).where(Plan.id == plan_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ========== 兑换码管理 ==========

    async def generate_redeem_codes(
        self,
        plan_id: int,
        count: int,
        batch_name: Optional[str] = None,
        max_uses: int = 1,
        expires_at: Optional[datetime] = None,
        created_by: Optional[int] = None,
    ) -> List[RedeemCode]:
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")

        codes = []
        for _ in range(count):
            code = RedeemCode(
                code=RedeemCode.generate_code(),
                plan_id=plan_id,
                batch_name=batch_name,
                max_uses=max_uses,
                expires_at=expires_at,
                created_by=created_by,
            )
            self.session.add(code)
            codes.append(code)

        await self.session.flush()
        logger.info(
            f"Generated {count} redeem codes for plan {plan_id}, batch={batch_name}"
        )
        return codes

    async def list_redeem_codes(
        self,
        batch_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[RedeemCode], int]:
        conditions = []
        if batch_name:
            conditions.append(RedeemCode.batch_name == batch_name)
        if status:
            conditions.append(RedeemCode.status == status)

        where = and_(*conditions) if conditions else True

        count_stmt = select(func.count()).select_from(RedeemCode).where(where)
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(RedeemCode)
            .where(where)
            .order_by(RedeemCode.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    # ========== 用户兑换/购买 ==========

    async def redeem_code(self, user_id: int, code: str) -> Order:
        user = await self.session.get(TelegramUser, user_id)
        if not user:
            raise PaymentError("User not found")

        stmt = select(RedeemCode).where(
            and_(RedeemCode.code == code, RedeemCode.status == RedeemCodeStatus.ACTIVE.value)
        )
        redeem = (await self.session.execute(stmt)).scalar_one_or_none()
        if not redeem:
            raise PaymentError("Invalid or inactive redeem code")

        if redeem.expires_at and redeem.expires_at < datetime.utcnow():
            raise PaymentError("Redeem code has expired")

        if redeem.used_count >= redeem.max_uses:
            raise PaymentError("Redeem code has been fully used")

        plan = await self.get_plan(redeem.plan_id)
        if not plan or not plan.is_active:
            raise PaymentError("Associated plan is unavailable")

        redeem.used_count += 1
        if redeem.used_count >= redeem.max_uses:
            redeem.status = RedeemCodeStatus.EXHAUSTED.value

        order = Order(
            order_no=self._generate_order_no(),
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=plan.price_cents,
            currency=plan.currency,
            status=OrderStatus.PAID.value,
            payment_provider="redeem_code",
            provider_tx_id=code,
            paid_at=datetime.utcnow(),
        )
        self.session.add(order)
        await self.session.flush()

        await self._log_payment(
            order_id=order.id,
            user_id=user_id,
            action=PaymentAction.CREATE,
            detail=f"Order created via redeem code: {code}",
        )
        await self._log_payment(
            order_id=order.id,
            user_id=user_id,
            action=PaymentAction.PAY,
            detail="Paid via redeem code",
        )

        order = await self._fulfill_order(order, user, plan)

        logger.info(
            f"User {user_id} redeemed code {code} for plan {plan.name}, order {order.order_no}"
        )
        return order

    async def create_order(
        self, user_id: int, plan_id: int, provider: str = "manual"
    ) -> Order:
        plan = await self.get_plan(plan_id)
        if not plan or not plan.is_active:
            raise PaymentError("Plan not found or inactive")

        user = await self.session.get(TelegramUser, user_id)
        if not user:
            raise PaymentError("User not found")

        expire_minutes = getattr(settings, "payment_order_expire_minutes", 30)
        order = Order(
            order_no=self._generate_order_no(),
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=plan.price_cents,
            currency=plan.currency,
            status=OrderStatus.PENDING.value,
            payment_provider=provider,
            expires_at=datetime.utcnow() + timedelta(minutes=expire_minutes),
        )
        self.session.add(order)
        await self.session.flush()

        await self._log_payment(
            order_id=order.id,
            user_id=user_id,
            action=PaymentAction.CREATE,
            detail=f"Order created, provider={provider}",
        )

        return order

    async def grant_plan_to_user(
        self,
        user_id: int,
        plan_id: int,
        operator_id: Optional[int] = None,
    ) -> Order:
        """管理员手动为用户充值"""
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")

        user = await self.session.get(TelegramUser, user_id)
        if not user:
            raise PaymentError("User not found")

        order = Order(
            order_no=self._generate_order_no(),
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=0,
            currency=plan.currency,
            status=OrderStatus.PAID.value,
            payment_provider="manual",
            paid_at=datetime.utcnow(),
        )
        self.session.add(order)
        await self.session.flush()

        await self._log_payment(
            order_id=order.id,
            user_id=user_id,
            action=PaymentAction.CREATE,
            detail=f"Manual grant by operator {operator_id}",
            operator_id=operator_id,
        )
        await self._log_payment(
            order_id=order.id,
            user_id=user_id,
            action=PaymentAction.PAY,
            detail="Manual grant, marked as paid",
            operator_id=operator_id,
        )

        order = await self._fulfill_order(order, user, plan, operator_id)
        logger.info(
            f"Admin {operator_id} granted plan {plan.name} to user {user_id}"
        )
        return order

    # ========== 订阅管理 ==========

    async def get_active_subscription(
        self, user_id: int
    ) -> Optional[UserSubscription]:
        stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                UserSubscription.expires_at > datetime.utcnow(),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_user_orders(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[Order], int]:
        count_stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.user_id == user_id)
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    # ========== 内部方法 ==========

    async def _fulfill_order(
        self,
        order: Order,
        user: TelegramUser,
        plan: Plan,
        operator_id: Optional[int] = None,
    ) -> Order:
        """发放套餐配额到用户"""
        user = await self._apply_plan_to_user(user, plan)

        order.status = OrderStatus.FULFILLED.value
        order.fulfilled_at = datetime.utcnow()

        if plan.plan_type == PlanType.SUBSCRIPTION.value:
            await self._upsert_subscription(user.id, plan, order.id)

        await self._log_payment(
            order_id=order.id,
            user_id=user.id,
            action=PaymentAction.FULFILL,
            detail=f"Plan {plan.name} fulfilled",
            operator_id=operator_id,
        )

        await self.session.flush()
        return order

    async def _apply_plan_to_user(
        self, user: TelegramUser, plan: Plan
    ) -> TelegramUser:
        """将套餐配额增量应用到用户"""
        if plan.pr_quota_bonus > 0:
            user.daily_quota += plan.pr_quota_bonus
        if plan.pr_daily_add > 0:
            user.daily_quota += plan.pr_daily_add
        if plan.pr_weekly_add > 0:
            user.weekly_quota += plan.pr_weekly_add
        if plan.pr_monthly_add > 0:
            user.monthly_quota += plan.pr_monthly_add

        if plan.issue_quota_bonus > 0:
            user.issue_daily_quota += plan.issue_quota_bonus
        if plan.issue_daily_add > 0:
            user.issue_daily_quota += plan.issue_daily_add
        if plan.issue_weekly_add > 0:
            user.issue_weekly_quota += plan.issue_weekly_add
        if plan.issue_monthly_add > 0:
            user.issue_monthly_quota += plan.issue_monthly_add

        await self.session.flush()
        return user

    async def _upsert_subscription(
        self, user_id: int, plan: Plan, order_id: int
    ) -> UserSubscription:
        stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.plan_id == plan.id,
                UserSubscription.status == SubscriptionStatus.ACTIVE.value,
            )
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()

        if existing:
            existing.expires_at = datetime.utcnow() + timedelta(
                days=plan.duration_days or 30
            )
            existing.last_order_id = order_id
            await self.session.flush()
            return existing

        sub = UserSubscription(
            user_id=user_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE.value,
            expires_at=datetime.utcnow() + timedelta(days=plan.duration_days or 30),
            last_order_id=order_id,
        )
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def _log_payment(
        self,
        order_id: int,
        user_id: int,
        action: str,
        detail: Optional[str] = None,
        operator_id: Optional[int] = None,
    ):
        log = PaymentLog(
            order_id=order_id,
            user_id=user_id,
            action=action,
            detail=detail,
            operator_id=operator_id,
        )
        self.session.add(log)

    @staticmethod
    def _generate_order_no() -> str:
        now = datetime.utcnow()
        short_uuid = uuid.uuid4().hex[:8].upper()
        return f"ORD{now.strftime('%Y%m%d%H%M%S')}{short_uuid}"
