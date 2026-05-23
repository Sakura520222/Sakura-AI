"""付费配额核心服务"""

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select, and_, func
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
from backend.core.config import get_dynamic_config, get_settings


class PaymentError(Exception):
    """支付业务异常"""

    pass


async def is_payment_enabled() -> bool:
    return bool(await get_dynamic_config("payment_enabled"))


class PaymentService:
    PLAN_UPDATE_FIELDS = {
        "name",
        "plan_type",
        "price_cents",
        "currency",
        "duration_days",
        "pr_quota_bonus",
        "pr_daily_add",
        "pr_weekly_add",
        "pr_monthly_add",
        "issue_quota_bonus",
        "issue_daily_add",
        "issue_weekly_add",
        "issue_monthly_add",
        "agent_quota_bonus",
        "agent_daily_add",
        "agent_weekly_add",
        "agent_monthly_add",
        "is_active",
        "sort_order",
        "description",
    }

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
        agent_quota_bonus: int = 0,
        agent_daily_add: int = 0,
        agent_weekly_add: int = 0,
        agent_monthly_add: int = 0,
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
            agent_quota_bonus=agent_quota_bonus,
            agent_daily_add=agent_daily_add,
            agent_weekly_add=agent_weekly_add,
            agent_monthly_add=agent_monthly_add,
            description=description,
            sort_order=sort_order,
        )
        self.session.add(plan)
        await self.session.flush()
        logger.info(
            "Created plan: {} (type={}, price={})", name, plan_type, price_cents
        )
        return plan

    async def update_plan(self, plan_id: int, **kwargs) -> Plan:
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")
        for key, value in kwargs.items():
            if key in self.PLAN_UPDATE_FIELDS and value is not None:
                setattr(plan, key, value)
        await self.session.flush()
        return plan

    async def list_plans(self, active_only: bool = False) -> List[Plan]:
        stmt = select(Plan).order_by(Plan.sort_order, Plan.id)
        if active_only:
            stmt = stmt.where(Plan.is_active)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_plan(self, plan_id: int) -> Optional[Plan]:
        stmt = select(Plan).where(Plan.id == plan_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_plan(self, plan_id: int, hard_delete: bool = False) -> Plan:
        """删除套餐（默认软删除，可选硬删除）

        软删除: 设置 is_active=False
        硬删除: 从数据库删除，但必须无关联订单和活跃订阅
        """
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")

        if hard_delete:
            # 检查关联订单
            order_count = (
                await self.session.execute(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.plan_id == plan_id)
                )
            ).scalar() or 0
            if order_count > 0:
                raise PaymentError(
                    f"Cannot hard delete plan with {order_count} associated orders"
                )

            # 检查活跃订阅
            active_sub_count = (
                await self.session.execute(
                    select(func.count())
                    .select_from(UserSubscription)
                    .where(
                        and_(
                            UserSubscription.plan_id == plan_id,
                            UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                        )
                    )
                )
            ).scalar() or 0
            if active_sub_count > 0:
                raise PaymentError(
                    f"Cannot hard delete plan with {active_sub_count} active subscriptions"
                )

            await self.session.delete(plan)
            await self.session.flush()
            logger.info("Hard deleted plan: {} (id={})", plan.name, plan_id)
        else:
            plan.is_active = False
            await self.session.flush()
            logger.info("Soft deleted plan: {} (id={})", plan.name, plan_id)

        return plan

    async def batch_delete_plans(
        self, plan_ids: list[int], hard_delete: bool = False
    ) -> dict:
        """批量删除套餐

        使用 savepoint 模式：单个失败不影响其他操作。

        Returns:
            dict: {"success": list[Plan], "failed": list[dict]}
        """
        results: list[Plan] = []
        failed: list[dict] = []
        for pid in plan_ids:
            async with self.session.begin_nested():
                try:
                    plan = await self.delete_plan(pid, hard_delete=hard_delete)
                    results.append(plan)
                except PaymentError as e:
                    logger.warning("batch_delete: plan {} failed: {}", pid, e)
                    failed.append({"id": pid, "reason": str(e)})
        return {"success": results, "failed": failed}

    async def batch_toggle_plans(self, plan_ids: list[int]) -> dict:
        """批量切换套餐启用/禁用状态

        Returns:
            dict: {"success": list[Plan], "skipped": list[dict]}
        """
        results: list[Plan] = []
        skipped: list[dict] = []
        for pid in plan_ids:
            plan = await self.get_plan(pid)
            if plan:
                plan.is_active = not plan.is_active
                results.append(plan)
            else:
                logger.warning("batch_toggle: plan {} not found, skipped", pid)
                skipped.append({"id": pid, "reason": "Plan not found"})
        if results:
            await self.session.flush()
        return {"success": results, "skipped": skipped}

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

    async def get_redeem_code(self, code_id: int) -> Optional[RedeemCode]:
        """按 ID 获取单个兑换码"""
        stmt = select(RedeemCode).where(RedeemCode.id == code_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    REDEEM_CODE_UPDATE_FIELDS = {"status", "expires_at", "max_uses", "plan_id"}

    async def update_redeem_code(self, code_id: int, **kwargs) -> RedeemCode:
        """更新兑换码信息（状态、有效期、最大使用次数、关联套餐）"""
        code = await self.get_redeem_code(code_id)
        if not code:
            raise PaymentError(f"Redeem code not found: {code_id}")

        new_plan_id = kwargs.get("plan_id")
        if new_plan_id is not None:
            plan = await self.get_plan(new_plan_id)
            if not plan or not plan.is_active:
                raise PaymentError(f"Plan not found or inactive: {new_plan_id}")

        new_max_uses = kwargs.get("max_uses")
        if new_max_uses is not None and new_max_uses < code.used_count:
            raise PaymentError(
                f"max_uses ({new_max_uses}) cannot be less than used_count ({code.used_count})"
            )

        new_status = kwargs.get("status")
        if new_status and new_status not in (
            RedeemCodeStatus.ACTIVE.value,
            RedeemCodeStatus.DISABLED.value,
        ):
            raise PaymentError(f"Invalid status: {new_status}")

        for key, value in kwargs.items():
            if key in self.REDEEM_CODE_UPDATE_FIELDS and value is not None:
                setattr(code, key, value)
        await self.session.flush()
        logger.info("Updated redeem code {} (id={})", code.code, code_id)
        return code

    async def delete_redeem_code(self, code_id: int) -> RedeemCode:
        """删除兑换码（仅允许删除未使用的兑换码）"""
        code = await self.get_redeem_code(code_id)
        if not code:
            raise PaymentError(f"Redeem code not found: {code_id}")

        if code.used_count > 0:
            raise PaymentError(
                f"Cannot delete redeem code that has been used {code.used_count} time(s)"
            )

        await self.session.delete(code)
        await self.session.flush()
        logger.info("Deleted redeem code {} (id={})", code.code, code_id)
        return code

    async def batch_delete_redeem_codes(self, code_ids: list[int]) -> dict:
        """批量删除兑换码（仅删除未使用的）

        不使用 savepoint：所有异常情况（已使用、未找到）均被优雅处理为 skipped
        而非 raise，因此无需 savepoint 保护。若未来 session.delete() 可能
        触发数据库约束异常，需重新评估是否引入 begin_nested()。

        Returns:
            dict: {"success": list[RedeemCode], "skipped": list[dict]}
        """
        results: list[RedeemCode] = []
        skipped: list[dict] = []
        for cid in code_ids:
            code = await self.get_redeem_code(cid)
            if code and code.used_count == 0:
                await self.session.delete(code)
                results.append(code)
            elif code:
                skipped.append({"id": cid, "reason": f"already_used:{code.used_count}"})
                logger.info("Skipped deleting used code {} (id={})", code.code, cid)
            else:
                skipped.append({"id": cid, "reason": "not_found"})
        if results:
            await self.session.flush()
        return {"success": results, "skipped": skipped}

    async def batch_update_redeem_codes(self, code_ids: list[int], **kwargs) -> dict:
        """批量更新兑换码状态

        Returns:
            dict: {"success": list[RedeemCode], "skipped": list[dict]}
        """
        results: list[RedeemCode] = []
        skipped: list[dict] = []
        for cid in code_ids:
            try:
                code = await self.update_redeem_code(cid, **kwargs)
                results.append(code)
            except PaymentError as e:
                logger.info("Skipped code {}: {}", cid, e)
                skipped.append({"id": cid, "reason": str(e)})
        return {"success": results, "skipped": skipped}

    # ========== 用户兑换/购买 ==========

    async def redeem_code(self, user_id: int, code: str) -> Order:
        user = await self.session.get(TelegramUser, user_id)
        if not user:
            raise PaymentError("User not found")

        stmt = (
            select(RedeemCode)
            .where(
                and_(
                    RedeemCode.code == code,
                    RedeemCode.status == RedeemCodeStatus.ACTIVE.value,
                )
            )
            .with_for_update()
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

        expire_minutes = getattr(get_settings(), "payment_order_expire_minutes", 30)
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

        # For external payment providers, create payment via gateway
        from backend.services.payment import EXTERNAL_PAYMENT_PROVIDERS

        if provider in EXTERNAL_PAYMENT_PROVIDERS:
            checkout_url = await self._create_external_payment(order, plan, user_id)
            order._checkout_url = checkout_url

        return order

    async def _create_external_payment(
        self, order: Order, plan: Plan, user_id: int
    ) -> str:
        """Create payment via external gateway and return checkout URL"""
        import json

        from backend.services.payment import get_gateway

        settings = get_settings()
        domain = settings.sanitized_app_domain
        currency = str(await self._get_stripe_currency())

        # Stripe minimum amount validation (per currency)
        stripe_minimums = {
            "usd": 50, "eur": 50, "gbp": 30, "jpy": 50, "cny": 320,
            "cad": 60, "aud": 60, "hkd": 400, "sgd": 50, "twd": 200,
        }
        min_cents = stripe_minimums.get(currency.lower(), 50)
        if order.amount_cents < min_cents:
            min_display = min_cents / 100
            raise PaymentError(
                f"Payment amount too low. Stripe requires a minimum of "
                f"{min_display:.2f} {currency.upper()} "
                f"(current: {order.amount_cents / 100:.2f} {currency.upper()})"
            )

        success_url = f"https://{domain}/billing/payment/result?order_no={order.order_no}&status=success"
        cancel_url = f"https://{domain}/billing/payment/result?order_no={order.order_no}&status=cancel"

        gateway = await get_gateway(order.payment_provider)
        result = await gateway.create_payment(
            order_no=order.order_no,
            amount_cents=order.amount_cents,
            currency=currency,
            plan_name=plan.name,
            user_id=user_id,
            success_url=success_url,
            cancel_url=cancel_url,
        )

        if not result.success:
            raise PaymentError(
                f"Failed to create payment: {result.error_message}"
            )

        order.provider_tx_id = result.provider_tx_id
        metadata = {
            "checkout_url": result.checkout_url,
            "session_id": result.provider_tx_id,
        }
        if order.metadata_json:
            try:
                existing = json.loads(order.metadata_json)
                existing.update(metadata)
                metadata = existing
            except (json.JSONDecodeError, TypeError):
                pass
        order.metadata_json = json.dumps(metadata)
        await self.session.flush()

        await self._log_payment(
            order_id=order.id,
            user_id=order.user_id,
            action=PaymentAction.CREATE,
            detail=f"External payment created via {order.payment_provider}, "
            f"tx_id={result.provider_tx_id}",
        )

        return result.checkout_url

    async def _get_stripe_currency(self) -> str:
        """Get Stripe currency from dynamic config, fallback to plan currency"""
        from backend.core.config import get_dynamic_config

        return str(
            await get_dynamic_config("stripe_currency")
            or await get_dynamic_config("payment_default_currency")
            or "CNY"
        )

    async def confirm_payment(
        self,
        order_no: str,
        provider_tx_id: str,
    ) -> Order:
        """Confirm payment for a PENDING order (PENDING -> PAID -> FULFILLED)"""
        stmt = select(Order).where(
            and_(
                Order.order_no == order_no,
                Order.provider_tx_id == provider_tx_id,
            )
        )
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if not order:
            raise PaymentError(f"Order not found: {order_no}")

        if order.status != OrderStatus.PENDING.value:
            logger.warning(
                "Order {} is already {}, skipping confirmation",
                order_no,
                order.status,
            )
            return order

        user = await self.session.get(TelegramUser, order.user_id)
        if not user:
            raise PaymentError(f"User not found for order: {order_no}")

        plan = await self.get_plan(order.plan_id)
        if not plan:
            raise PaymentError(f"Plan not found for order: {order_no}")

        order.status = OrderStatus.PAID.value
        order.paid_at = datetime.utcnow()

        await self._log_payment(
            order_id=order.id,
            user_id=order.user_id,
            action=PaymentAction.PAY,
            detail=f"Payment confirmed via webhook, tx_id={provider_tx_id}",
        )

        order = await self._fulfill_order(order, user, plan)

        logger.info(
            "Payment confirmed and fulfilled: order_no={}, tx_id={}",
            order_no,
            provider_tx_id,
        )
        return order

    async def cancel_expired_order(self, order_no: str) -> Order:
        """Cancel an expired PENDING order"""
        stmt = select(Order).where(Order.order_no == order_no)
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if not order:
            raise PaymentError(f"Order not found: {order_no}")

        if order.status != OrderStatus.PENDING.value:
            raise PaymentError(
                f"Cannot cancel order in status: {order.status}"
            )

        order.status = OrderStatus.CANCELLED.value
        await self._log_payment(
            order_id=order.id,
            user_id=order.user_id,
            action=PaymentAction.EXPIRE,
            detail="Order cancelled (checkout session expired)",
        )
        await self.session.flush()

        logger.info("Order cancelled: order_no={}", order_no)
        return order

    async def process_refund(
        self,
        order_id: int,
        amount_cents: Optional[int] = None,
        operator_id: Optional[int] = None,
    ) -> Order:
        """Process refund for a FULFILLED order"""
        order = await self.session.get(Order, order_id)
        if not order:
            raise PaymentError(f"Order not found: {order_id}")

        if order.status != OrderStatus.FULFILLED.value:
            raise PaymentError(
                f"Cannot refund order in status: {order.status}, "
                "only FULFILLED orders can be refunded"
            )

        user = await self.session.get(TelegramUser, order.user_id)
        if not user:
            raise PaymentError(f"User not found for order: {order_id}")

        plan = await self.get_plan(order.plan_id)

        from backend.services.payment import EXTERNAL_PAYMENT_PROVIDERS, get_gateway

        if (
            order.payment_provider in EXTERNAL_PAYMENT_PROVIDERS
            and order.provider_tx_id
        ):
            gateway = await get_gateway(order.payment_provider)
            refund_result = await gateway.refund(
                provider_tx_id=order.provider_tx_id,
                amount_cents=amount_cents,
                reason="requested_by_customer",
            )
            if not refund_result.success:
                raise PaymentError(
                    f"Refund failed via gateway: {refund_result.error_message}"
                )

            await self._log_payment(
                order_id=order.id,
                user_id=order.user_id,
                action=PaymentAction.REFUND,
                detail=f"Refund processed via {order.payment_provider}, "
                f"refund_id={refund_result.refund_id}, "
                f"amount={refund_result.amount_cents}",
                operator_id=operator_id,
            )
        else:
            await self._log_payment(
                order_id=order.id,
                user_id=order.user_id,
                action=PaymentAction.REFUND,
                detail=f"Manual refund by operator {operator_id}",
                operator_id=operator_id,
            )

        # Claw back quotas for subscription plans
        if plan and plan.plan_type == PlanType.SUBSCRIPTION.value:
            stmt = select(UserSubscription).where(
                and_(
                    UserSubscription.user_id == order.user_id,
                    UserSubscription.last_order_id == order.id,
                    UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                )
            )
            subscription = (
                await self.session.execute(stmt)
            ).scalar_one_or_none()
            if subscription:
                await self._expire_subscription(subscription, user, plan)

        order.status = OrderStatus.REFUNDED.value
        await self.session.flush()

        logger.info(
            "Order refunded: order_id={}, provider={}, operator={}",
            order_id,
            order.payment_provider,
            operator_id,
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
            "Admin {} granted plan {} to user {}", operator_id, plan.name, user_id
        )
        return order

    # ========== 订阅管理 ==========

    async def get_active_subscription(self, user_id: int) -> Optional[UserSubscription]:
        await self.expire_due_subscriptions(user_id)
        stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                UserSubscription.expires_at > datetime.utcnow(),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def expire_due_subscriptions(self, user_id: Optional[int] = None) -> int:
        conditions = [
            UserSubscription.status == SubscriptionStatus.ACTIVE.value,
            UserSubscription.expires_at <= datetime.utcnow(),
        ]
        if user_id is not None:
            conditions.append(UserSubscription.user_id == user_id)

        stmt = (
            select(UserSubscription, TelegramUser, Plan)
            .join(TelegramUser, UserSubscription.user_id == TelegramUser.id)
            .join(Plan, UserSubscription.plan_id == Plan.id)
            .where(and_(*conditions))
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        for subscription, user, plan in rows:
            await self._expire_subscription(subscription, user, plan)

        return len(rows)

    async def list_user_orders(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[Order], int]:
        count_stmt = (
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
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

    def _plan_quota_values(self, plan: Plan) -> dict[str, int]:
        return {
            "pr_quota_bonus": plan.pr_quota_bonus or 0,
            "pr_daily_add": plan.pr_daily_add or 0,
            "pr_weekly_add": plan.pr_weekly_add or 0,
            "pr_monthly_add": plan.pr_monthly_add or 0,
            "issue_quota_bonus": plan.issue_quota_bonus or 0,
            "issue_daily_add": plan.issue_daily_add or 0,
            "issue_weekly_add": plan.issue_weekly_add or 0,
            "issue_monthly_add": plan.issue_monthly_add or 0,
            "agent_quota_bonus": plan.agent_quota_bonus or 0,
            "agent_daily_add": plan.agent_daily_add or 0,
            "agent_weekly_add": plan.agent_weekly_add or 0,
            "agent_monthly_add": plan.agent_monthly_add or 0,
        }

    async def _apply_plan_to_user(self, user: TelegramUser, plan: Plan) -> TelegramUser:
        """将套餐配额增量应用到用户"""
        values = self._plan_quota_values(plan)
        if values["pr_quota_bonus"] > 0:
            user.daily_quota += values["pr_quota_bonus"]
        if values["pr_daily_add"] > 0:
            user.daily_quota += values["pr_daily_add"]
        if values["pr_weekly_add"] > 0:
            user.weekly_quota += values["pr_weekly_add"]
        if values["pr_monthly_add"] > 0:
            user.monthly_quota += values["pr_monthly_add"]

        if values["issue_quota_bonus"] > 0:
            user.issue_daily_quota += values["issue_quota_bonus"]
        if values["issue_daily_add"] > 0:
            user.issue_daily_quota += values["issue_daily_add"]
        if values["issue_weekly_add"] > 0:
            user.issue_weekly_quota += values["issue_weekly_add"]
        if values["issue_monthly_add"] > 0:
            user.issue_monthly_quota += values["issue_monthly_add"]

        if values["agent_quota_bonus"] > 0:
            user.agent_daily_quota += values["agent_quota_bonus"]
        if values["agent_daily_add"] > 0:
            user.agent_daily_quota += values["agent_daily_add"]
        if values["agent_weekly_add"] > 0:
            user.agent_weekly_quota += values["agent_weekly_add"]
        if values["agent_monthly_add"] > 0:
            user.agent_monthly_quota += values["agent_monthly_add"]

        await self.session.flush()
        return user

    async def _upsert_subscription(
        self, user_id: int, plan: Plan, order_id: int
    ) -> UserSubscription:
        stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.plan_id == plan.id,
            )
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()

        values = self._plan_quota_values(plan)
        if existing:
            existing.status = SubscriptionStatus.ACTIVE.value
            existing.expires_at = datetime.utcnow() + timedelta(
                days=plan.duration_days or 30
            )
            existing.applied_pr_quota_bonus = values["pr_quota_bonus"]
            existing.applied_pr_daily_add = values["pr_daily_add"]
            existing.applied_pr_weekly_add = values["pr_weekly_add"]
            existing.applied_pr_monthly_add = values["pr_monthly_add"]
            existing.applied_issue_quota_bonus = values["issue_quota_bonus"]
            existing.applied_issue_daily_add = values["issue_daily_add"]
            existing.applied_issue_weekly_add = values["issue_weekly_add"]
            existing.applied_issue_monthly_add = values["issue_monthly_add"]
            existing.applied_agent_quota_bonus = values["agent_quota_bonus"]
            existing.applied_agent_daily_add = values["agent_daily_add"]
            existing.applied_agent_weekly_add = values["agent_weekly_add"]
            existing.applied_agent_monthly_add = values["agent_monthly_add"]
            existing.last_order_id = order_id
            await self.session.flush()
            return existing

        sub = UserSubscription(
            user_id=user_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE.value,
            expires_at=datetime.utcnow() + timedelta(days=plan.duration_days or 30),
            applied_pr_quota_bonus=values["pr_quota_bonus"],
            applied_pr_daily_add=values["pr_daily_add"],
            applied_pr_weekly_add=values["pr_weekly_add"],
            applied_pr_monthly_add=values["pr_monthly_add"],
            applied_issue_quota_bonus=values["issue_quota_bonus"],
            applied_issue_daily_add=values["issue_daily_add"],
            applied_issue_weekly_add=values["issue_weekly_add"],
            applied_issue_monthly_add=values["issue_monthly_add"],
            applied_agent_quota_bonus=values["agent_quota_bonus"],
            applied_agent_daily_add=values["agent_daily_add"],
            applied_agent_weekly_add=values["agent_weekly_add"],
            applied_agent_monthly_add=values["agent_monthly_add"],
            last_order_id=order_id,
        )
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def _expire_subscription(
        self, subscription: UserSubscription, user: TelegramUser, plan: Plan
    ) -> UserSubscription:
        """订阅过期时扣回已发放的套餐配额"""
        applied_values = {
            "pr_quota_bonus": getattr(subscription, "applied_pr_quota_bonus", None),
            "pr_daily_add": getattr(subscription, "applied_pr_daily_add", None),
            "pr_weekly_add": getattr(subscription, "applied_pr_weekly_add", None),
            "pr_monthly_add": getattr(subscription, "applied_pr_monthly_add", None),
            "issue_quota_bonus": getattr(
                subscription, "applied_issue_quota_bonus", None
            ),
            "issue_daily_add": getattr(subscription, "applied_issue_daily_add", None),
            "issue_weekly_add": getattr(subscription, "applied_issue_weekly_add", None),
            "issue_monthly_add": getattr(
                subscription, "applied_issue_monthly_add", None
            ),
            "agent_quota_bonus": getattr(
                subscription, "applied_agent_quota_bonus", None
            ),
            "agent_daily_add": getattr(subscription, "applied_agent_daily_add", None),
            "agent_weekly_add": getattr(
                subscription, "applied_agent_weekly_add", None
            ),
            "agent_monthly_add": getattr(
                subscription, "applied_agent_monthly_add", None
            ),
        }

        if all(value is None for value in applied_values.values()):
            applied_values = {
                "pr_quota_bonus": plan.pr_quota_bonus,
                "pr_daily_add": plan.pr_daily_add,
                "pr_weekly_add": plan.pr_weekly_add,
                "pr_monthly_add": plan.pr_monthly_add,
                "issue_quota_bonus": plan.issue_quota_bonus,
                "issue_daily_add": plan.issue_daily_add,
                "issue_weekly_add": plan.issue_weekly_add,
                "issue_monthly_add": plan.issue_monthly_add,
                "agent_quota_bonus": plan.agent_quota_bonus,
                "agent_daily_add": plan.agent_daily_add,
                "agent_weekly_add": plan.agent_weekly_add,
                "agent_monthly_add": plan.agent_monthly_add,
            }

        user.daily_quota = max(
            0,
            user.daily_quota
            - (applied_values["pr_quota_bonus"] or 0)
            - (applied_values["pr_daily_add"] or 0),
        )
        user.weekly_quota = max(
            0, user.weekly_quota - (applied_values["pr_weekly_add"] or 0)
        )
        user.monthly_quota = max(
            0, user.monthly_quota - (applied_values["pr_monthly_add"] or 0)
        )
        user.issue_daily_quota = max(
            0,
            user.issue_daily_quota
            - (applied_values["issue_quota_bonus"] or 0)
            - (applied_values["issue_daily_add"] or 0),
        )
        user.issue_weekly_quota = max(
            0,
            user.issue_weekly_quota - (applied_values["issue_weekly_add"] or 0),
        )
        user.issue_monthly_quota = max(
            0,
            user.issue_monthly_quota - (applied_values["issue_monthly_add"] or 0),
        )
        user.agent_daily_quota = max(
            0,
            user.agent_daily_quota
            - (applied_values["agent_quota_bonus"] or 0)
            - (applied_values["agent_daily_add"] or 0),
        )
        user.agent_weekly_quota = max(
            0,
            user.agent_weekly_quota - (applied_values["agent_weekly_add"] or 0),
        )
        user.agent_monthly_quota = max(
            0,
            user.agent_monthly_quota - (applied_values["agent_monthly_add"] or 0),
        )
        quota_checks = [
            ("PR daily", user.daily_used, user.daily_quota),
            ("PR weekly", user.weekly_used, user.weekly_quota),
            ("PR monthly", user.monthly_used, user.monthly_quota),
            ("Issue daily", user.issue_daily_used, user.issue_daily_quota),
            ("Issue weekly", user.issue_weekly_used, user.issue_weekly_quota),
            ("Issue monthly", user.issue_monthly_used, user.issue_monthly_quota),
            ("Agent daily", user.agent_daily_used, user.agent_daily_quota),
            ("Agent weekly", user.agent_weekly_used, user.agent_weekly_quota),
            ("Agent monthly", user.agent_monthly_used, user.agent_monthly_quota),
        ]
        for label, used, quota in quota_checks:
            if used > quota:
                logger.warning(
                    f"Subscription expiry left {label} usage above quota: "
                    f"user_id={user.id}, used={used}, quota={quota}"
                )
        subscription.status = SubscriptionStatus.EXPIRED.value
        await self.session.flush()
        return subscription

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
