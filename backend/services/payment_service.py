"""付费配额核心服务"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from types import SimpleNamespace

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.config import get_dynamic_config, get_settings
from backend.core.time_service import now_utc
from backend.models.legacy_entitlement_models import (
    LegacyEntitlement,
    PaymentReceipt,
    PaymentRefundAttempt,
    PaymentRefundAttemptEvent,
    RedeemCodeRedemption,
)
from backend.models.payment_models import (
    Order,
    OrderStatus,
    PaymentAction,
    PaymentLog,
    Plan,
    PlanType,
    RedeemCode,
    RedeemCodeStatus,
    RefundRequest,
    RefundRequestStatus,
    SubscriptionStatus,
    UserSubscription,
)
from backend.models.telegram_models import TelegramUser
from backend.services.legacy_entitlement_service import (
    RATE_LIMIT_KEYS,
    LegacyEntitlementService,
)
from backend.services.payment.currency_units import normalize_currency


class PaymentError(Exception):
    """支付业务异常"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


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
        "credit_grant",
        "rate_limits",
        "concurrency_limit",
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
        duration_days: int | None = None,
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
        description: str | None = None,
        sort_order: int = 0,
        credit_grant: Decimal | str | int = 0,
        rate_limits: dict[str, int] | None = None,
        concurrency_limit: int | None = None,
    ) -> Plan:
        self._validate_billing_plan(credit_grant, rate_limits, concurrency_limit)
        try:
            currency = normalize_currency(currency)
        except ValueError:
            raise PaymentError("Unsupported currency", "invalid_currency") from None
        plan = Plan(
            credit_grant=Decimal(str(credit_grant)),
            rate_limits=rate_limits,
            concurrency_limit=concurrency_limit,
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
        if kwargs.get("currency") is not None:
            try:
                kwargs["currency"] = normalize_currency(kwargs["currency"])
            except ValueError:
                raise PaymentError("Unsupported currency", "invalid_currency") from None
        self._validate_billing_plan(
            kwargs.get("credit_grant", plan.credit_grant or 0),
            kwargs.get("rate_limits", plan.rate_limits),
            kwargs.get("concurrency_limit", plan.concurrency_limit),
        )
        for key, value in kwargs.items():
            if key in self.PLAN_UPDATE_FIELDS and (
                value is not None or key == "concurrency_limit"
            ):
                setattr(plan, key, value)
        await self.session.flush()
        return plan

    async def list_plans(self, active_only: bool = False) -> list[Plan]:
        stmt = select(Plan).order_by(Plan.sort_order, Plan.id)
        if active_only:
            stmt = stmt.where(Plan.is_active)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_plan(self, plan_id: int) -> Plan | None:
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

            code_count = (
                await self.session.execute(
                    select(func.count())
                    .select_from(RedeemCode)
                    .where(RedeemCode.plan_id == plan_id)
                )
            ).scalar() or 0
            if code_count:
                raise PaymentError(
                    f"Cannot hard delete plan with {code_count} issued redeem codes"
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
        batch_name: str | None = None,
        max_uses: int = 1,
        expires_at: datetime | None = None,
        created_by: int | None = None,
    ) -> list[RedeemCode]:
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")

        codes = []
        for _ in range(count):
            code = RedeemCode(
                code=RedeemCode.generate_code(),
                plan_id=plan_id,
                plan_snapshot=self._snapshot_plan(plan),
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
        batch_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RedeemCode], int]:
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

    async def get_redeem_code(self, code_id: int) -> RedeemCode | None:
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
        if new_plan_id is not None and new_plan_id != code.plan_id:
            if code.plan_snapshot:
                raise PaymentError(
                    "An issued code has an immutable purchased offer; create a new batch"
                )
            raise PaymentError(
                "Legacy code offer changes require an audited historical snapshot"
            )
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
                )
            )
            .with_for_update()
        )
        redeem = (await self.session.execute(stmt)).scalar_one_or_none()
        if not redeem:
            raise PaymentError("Invalid or inactive redeem code")

        previous = (
            await self.session.execute(
                select(RedeemCodeRedemption).where(
                    RedeemCodeRedemption.redeem_code_id == redeem.id,
                    RedeemCodeRedemption.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if previous:
            return await self.session.get(Order, previous.order_id)
        if redeem.status != RedeemCodeStatus.ACTIVE.value:
            raise PaymentError("Invalid or inactive redeem code")

        if redeem.expires_at and redeem.expires_at < now_utc():
            raise PaymentError("Redeem code has expired")

        if redeem.used_count >= redeem.max_uses:
            raise PaymentError("Redeem code has been fully used")

        plan = await self.get_plan(redeem.plan_id)
        if not plan:
            raise PaymentError("Associated plan is unavailable")
        if not redeem.plan_snapshot:
            raise PaymentError(
                "Legacy redeem code requires an audited purchased snapshot",
                code="snapshot_required",
            )
        snapshot = redeem.plan_snapshot
        result = await self.session.execute(
            update(RedeemCode)
            .where(
                RedeemCode.id == redeem.id,
                RedeemCode.status == RedeemCodeStatus.ACTIVE.value,
                RedeemCode.used_count < RedeemCode.max_uses,
            )
            .values(used_count=RedeemCode.used_count + 1)
        )
        if not result.rowcount:
            raise PaymentError("Redeem code has been fully used")
        await self.session.refresh(redeem)
        if redeem.used_count >= redeem.max_uses:
            redeem.status = RedeemCodeStatus.EXHAUSTED.value

        order = Order(
            order_no=self._generate_order_no(),
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=snapshot["price_cents"],
            currency=snapshot["currency"],
            status=OrderStatus.PAID.value,
            payment_provider="redeem_code",
            provider_tx_id=code,
            plan_snapshot=snapshot,
            paid_at=now_utc(),
        )
        self.session.add(order)
        await self.session.flush()

        self.session.add(
            RedeemCodeRedemption(
                redeem_code_id=redeem.id, user_id=user_id, order_id=order.id
            )
        )
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

        if (
            plan.price_cents > 0
            and Decimal(str(plan.credit_grant or 0)) == 0
            and any(self._plan_quota_values(plan).values())
            and await get_dynamic_config("billing_enabled", fresh=True)
        ):
            raise PaymentError(
                "Legacy paid quota plans must receive a confirmed Credits grant before new sales",
                code="legacy_plan_needs_credits",
            )

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
            expires_at=now_utc() + timedelta(minutes=expire_minutes),
            plan_snapshot=self._snapshot_plan(plan),
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

            # 虚拟币支付：提取充值信息供前端展示
            if provider in ("nowpayments", "tron") and order.metadata_json:
                try:
                    md = json.loads(order.metadata_json)
                    if md.get("is_crypto") is True:
                        order._crypto_payment_info = {
                            "pay_address": md.get("pay_address", ""),
                            "pay_amount": md.get("pay_amount", ""),
                            "pay_currency": md.get("pay_currency", ""),
                            "price_amount": md.get("price_amount", ""),
                            "price_currency": md.get("price_currency", "usd"),
                            "payment_id": md.get("payment_id", ""),
                            "order_no": order.order_no,
                        }
                except json.JSONDecodeError, TypeError:
                    pass

        return order

    async def _create_external_payment(
        self, order: Order, plan: Plan, user_id: int
    ) -> str:
        """Create payment via external gateway and return checkout URL"""
        from backend.services.payment import get_gateway

        settings = get_settings()
        domain = settings.sanitized_app_domain
        provider_currency = str(
            await self._get_provider_currency(order.payment_provider)
        ).upper()
        if order.payment_provider == "alipay" and provider_currency != "CNY":
            raise PaymentError(
                "Alipay page.pay supports CNY payments only", code="invalid_currency"
            )

        # 确定订单原始货币（套餐定价货币）
        order_currency = order.currency.upper()

        # TronGateway 使用 USDT（≈USD），provider_currency 固定为 USD，
        # 不受 payment_default_currency 影响。
        if order.payment_provider == "tron":
            provider_currency = "USD"

        # NOWPayments 支持 price_currency 直接传 CNY，由其 API 按实时汇率转换，
        # 无需自研转换。TronGateway 需要转换为 USD（USDT≈USD）。
        # 其他网关若货币不一致则需要转换。
        amount_cents = order.amount_cents
        gateway_currency = provider_currency
        if order.payment_provider == "nowpayments":
            # 直接传原始 CNY 金额，让 NOWPayments 处理汇率
            gateway_currency = order_currency
        elif order.payment_provider == "tron":
            # TronGateway 使用 USDT（≈USD），需要从 CNY 转换
            if provider_currency != order_currency:
                converted = await self._convert_currency(
                    order.amount_cents, order_currency, provider_currency
                )
                if converted != order.amount_cents:
                    logger.info(
                        "Currency conversion: {} {} cents → {} {} cents, order={}",
                        order.amount_cents,
                        order_currency,
                        converted,
                        provider_currency,
                        order.order_no,
                    )
                amount_cents = converted
        elif provider_currency != order_currency:
            converted = await self._convert_currency(
                order.amount_cents, order_currency, provider_currency
            )
            if converted != order.amount_cents:
                logger.info(
                    "Currency conversion: {} {} cents → {} {} cents, order={}",
                    order.amount_cents,
                    order_currency,
                    converted,
                    provider_currency,
                    order.order_no,
                )
            amount_cents = converted

        success_url = f"https://{domain}/billing/payment/result?order_no={order.order_no}&status=success"
        cancel_url = f"https://{domain}/billing/payment/result?order_no={order.order_no}&status=cancel"

        # Alipay: notify_url 用 webhook 回调，return_url 用前端成功页面
        if order.payment_provider == "alipay":
            success_url = f"https://{domain}/api/webhook/{order.payment_provider}"
            cancel_url = f"https://{domain}/billing/payment/result?order_no={order.order_no}&status=success"
        # NOWPayments: webhook 回调
        elif order.payment_provider == "nowpayments":
            success_url = f"https://{domain}/api/webhook/{order.payment_provider}"

        # get_gateway 会对未注册/未启用/未配置的 provider 抛出 ValueError
        # 其他意外异常（如配置读取失败）也应转为 PaymentError
        try:
            gateway = await get_gateway(order.payment_provider)
        except (ValueError, RuntimeError) as exc:
            raise PaymentError(str(exc)) from exc
        if order.payment_provider == "tron":
            from backend.services.payment.tron_gateway import TronGateway

            historical = (
                (
                    await self.session.execute(
                        select(Order.metadata_json).where(
                            Order.payment_provider == "tron",
                            Order.invoice_identity.is_(None),
                            Order.metadata_json.isnot(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            historical_fingerprints = set()
            for raw in historical:
                try:
                    historical_fingerprints.add(
                        self._tron_invoice_identity(json.loads(raw))
                    )
                except (
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                    PaymentError,
                ) as exc:
                    raise PaymentError(
                        "Historical TRON invoices require review before new issuance",
                        code="tron_invoice_audit_required",
                    ) from exc
            invoice_seed = order.order_no
            for _candidate in range(TronGateway.INVOICE_SUFFIX_VARIANTS):
                result = await gateway.create_payment(
                    order_no=order.order_no,
                    amount_cents=amount_cents,
                    currency=gateway_currency,
                    plan_name=plan.name,
                    user_id=user_id,
                    success_url=success_url,
                    cancel_url=cancel_url,
                )
                if not result.success:
                    break
                fingerprint = self._tron_invoice_identity(result.raw_data)
                used = (
                    await self.session.execute(
                        select(Order.id).where(Order.invoice_identity == fingerprint)
                    )
                ).scalar_one_or_none()
                if fingerprint in historical_fingerprints or used:
                    order.order_no = f"{invoice_seed}T{_candidate + 1:04d}"
                    continue
                try:
                    async with self.session.begin_nested():
                        order.invoice_identity = fingerprint
                        await self.session.flush()
                    break
                except IntegrityError:
                    await self.session.refresh(order)
                    order.order_no = f"{invoice_seed}T{_candidate + 1:04d}"
            else:
                raise PaymentError(
                    "TRON invoice amount range is exhausted; choose another payment channel",
                    code="tron_invoice_range_exhausted",
                )
        else:
            result = await gateway.create_payment(
                order_no=order.order_no,
                amount_cents=amount_cents,
                currency=gateway_currency,
                plan_name=plan.name,
                user_id=user_id,
                success_url=success_url,
                cancel_url=cancel_url,
            )

        if not result.success:
            raise PaymentError(f"Failed to create payment: {result.error_message}")

        order.provider_tx_id = result.provider_tx_id
        metadata = {
            "checkout_url": result.checkout_url,
            "session_id": result.provider_tx_id,
            "gateway_amount_cents": amount_cents,
            "gateway_currency": gateway_currency,
        }

        # 虚拟币支付：存储充值地址、金额、币种等额外信息
        if order.payment_provider in ("nowpayments", "tron") and result.raw_data:
            metadata["pay_address"] = result.raw_data.get("pay_address", "")
            metadata["pay_amount"] = str(result.raw_data.get("pay_amount", ""))
            metadata["pay_currency"] = result.raw_data.get("pay_currency", "")
            metadata["payment_id"] = str(result.raw_data.get("payment_id", ""))
            metadata["price_amount"] = str(result.raw_data.get("price_amount", ""))
            metadata["price_currency"] = result.raw_data.get("price_currency", "usd")
            metadata["is_crypto"] = True

        if order.metadata_json:
            try:
                existing = json.loads(order.metadata_json)
                existing.update(metadata)
                metadata = existing
            except json.JSONDecodeError, TypeError:
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

    @staticmethod
    def _tron_invoice_identity(metadata: dict) -> str:
        if (
            not isinstance(metadata, dict)
            or not metadata.get("pay_address")
            or str(metadata.get("pay_currency", "")).lower() != "usdttrc20"
        ):
            raise PaymentError(
                "TRON invoice lacks verified wallet/currency metadata",
                code="tron_invoice_audit_required",
            )
        try:
            amount = Decimal(str(metadata["pay_amount"]))
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise PaymentError(
                "TRON invoice amount requires audit", code="tron_invoice_audit_required"
            ) from exc
        if (
            not amount.is_finite()
            or amount <= 0
            or amount != amount.quantize(Decimal("0.000001"))
        ):
            raise PaymentError(
                "Invalid TRON invoice amount", code="tron_invoice_audit_required"
            )
        canonical = f"tron:usdttrc20:{metadata['pay_address']}:{amount:.6f}"
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def audit_tron_invoices(
        self, *, operator_id: int | None = None, dry_run: bool = True
    ) -> dict:
        """Backfill only unique, parseable historical invoice fingerprints."""
        if not dry_run:
            actor = await self.session.get(TelegramUser, operator_id)
            if not actor or not actor.is_active or actor.role != "super_admin":
                raise PaymentError("Invoice audit requires an active super-admin")
        statement = (
            select(Order).where(Order.payment_provider == "tron").order_by(Order.id)
        )
        if not dry_run:
            statement = statement.with_for_update()
        orders = (await self.session.execute(statement)).scalars().all()
        groups = {}
        invalid = []
        parsed = {}
        for order in orders:
            try:
                metadata = (
                    json.loads(order.metadata_json) if order.metadata_json else {}
                )
                fingerprint = self._tron_invoice_identity(metadata)
            except (PaymentError, ValueError, TypeError) as exc:
                invalid.append(
                    {
                        "order_id": order.id,
                        "status": "needs_source_evidence",
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            parsed[order.id] = metadata
            groups.setdefault(fingerprint, []).append(order)
        results = list(invalid)
        for fingerprint, group in groups.items():
            ambiguous = len(group) > 1
            for order in group:
                status = (
                    "ambiguous"
                    if ambiguous
                    else "already_identified"
                    if order.invoice_identity
                    else "dry_run"
                    if dry_run
                    else "identified"
                )
                results.append(
                    {
                        "order_id": order.id,
                        "invoice_identity": fingerprint,
                        "status": status,
                        "conflicting_order_ids": [item.id for item in group]
                        if ambiguous
                        else [],
                    }
                )
                if dry_run:
                    continue
                if ambiguous:
                    order.metadata_json = json.dumps(
                        {**parsed[order.id], "invoice_audit_status": "ambiguous"}
                    )
                elif not order.invoice_identity:
                    order.invoice_identity = fingerprint
                    await self._log_payment(
                        order_id=order.id,
                        user_id=order.user_id,
                        action="invoice_audit",
                        detail="Unique historical invoice identity restored from gateway snapshot",
                        operator_id=operator_id,
                    )
        if not dry_run:
            await self.session.flush()
        return {
            "dry_run": dry_run,
            "orders": sorted(results, key=lambda row: row["order_id"]),
            "rule": "Ambiguous or missing source invoices require manual provider reconciliation; never guess payer",
        }

    async def _get_provider_currency(self, provider: str) -> str:
        """Get currency for the given payment provider from dynamic config"""
        provider_currency_key = f"{provider}_currency"
        return str(
            await get_dynamic_config(provider_currency_key)
            or await get_dynamic_config("payment_default_currency")
            or ("CNY" if provider in ("stripe", "alipay") else "USD")
        )

    async def _get_stripe_currency(self) -> str:
        """Get Stripe currency from dynamic config (backward compat)"""
        return await self._get_provider_currency("stripe")

    async def _convert_currency(
        self, amount_cents: int, from_cur: str, to_cur: str
    ) -> int:
        """Convert exact minor units using an explicitly configured FX rate."""
        from backend.services.billing_pricing import DECIMAL_PRECISION, exact_rate

        if from_cur.upper() == to_cur.upper():
            return amount_cents
        configured = await get_dynamic_config(
            f"exchange_rate_{from_cur.upper()}_{to_cur.upper()}"
        )
        if configured is None or isinstance(configured, (float, bool)):
            raise PaymentError(
                "Confirmed decimal exchange rate is required",
                code="exchange_rate_required",
            )
        try:
            rate = exact_rate(configured)
        except (InvalidOperation, ValueError) as exc:
            raise PaymentError("Invalid exchange rate configuration") from exc
        if not rate.is_finite() or rate <= 0:
            raise PaymentError("Exchange rate must be finite and positive")
        from backend.services.payment.currency_units import currency_minor_exponent

        try:
            source_units = Decimal(10) ** currency_minor_exponent(from_cur)
            target_units = Decimal(10) ** currency_minor_exponent(to_cur)
        except ValueError as exc:
            raise PaymentError("Currency minor unit metadata is unknown") from exc
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            converted = int(
                (
                    Decimal(amount_cents) / source_units * rate * target_units
                ).to_integral_value(rounding=ROUND_HALF_EVEN)
            )
        if converted <= 0:
            raise PaymentError("Converted payment amount must be positive")
        return converted

    async def _validate_payment_amount(
        self, order: Order, paid_amount_cents: int | None, paid_currency: str | None
    ) -> None:
        """Compare signed provider results to immutable checkout amount/currency."""
        if paid_amount_cents is None or paid_currency is None:
            raise PaymentError(
                "Payment amount and currency are required",
                code="payment_evidence_required",
            )
        if isinstance(paid_amount_cents, (float, bool)) or not isinstance(
            paid_amount_cents, int
        ):
            raise PaymentError(
                "Payment amount mismatch: amount must use integer minor units"
            )
        metadata = json.loads(order.metadata_json) if order.metadata_json else {}
        expected_amount = metadata.get("gateway_amount_cents", order.amount_cents)
        expected_currency = str(
            metadata.get("gateway_currency", order.currency) or ""
        ).upper()
        if paid_amount_cents <= 0 or paid_amount_cents != expected_amount:
            raise PaymentError(f"Payment amount mismatch for order {order.order_no}")
        if paid_currency.upper() != expected_currency:
            raise PaymentError(f"Payment currency mismatch for order {order.order_no}")

    async def confirm_payment(
        self,
        order_no: str,
        provider_tx_id: str,
        paid_amount_cents: int | None = None,
        paid_currency: str | None = None,
    ) -> Order:
        """Confirm payment for a PENDING order (PENDING -> PAID -> FULFILLED)"""
        # 仅按 order_no 查询，provider_tx_id 在创建时设为 order_no，
        # webhook 回调时用实际 trade_no 覆盖，两者不一致不能用作 WHERE 条件
        stmt = select(Order).where(Order.order_no == order_no).with_for_update()
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if not order:
            raise PaymentError(f"Order not found: {order_no}")

        if order.status not in {OrderStatus.PENDING.value, OrderStatus.PAID.value}:
            logger.warning(
                "Order {} is already {}, skipping confirmation",
                order_no,
                order.status,
            )
            return order

        if order.payment_provider == "tron":
            metadata = json.loads(order.metadata_json) if order.metadata_json else {}
            if (
                metadata.get("invoice_audit_status") == "ambiguous"
                or not order.invoice_identity
                or order.invoice_identity != self._tron_invoice_identity(metadata)
            ):
                raise PaymentError(
                    "TRON invoice identity must be audited before confirmation",
                    code="tron_invoice_audit_required",
                )

        await self._validate_payment_amount(
            order,
            paid_amount_cents=paid_amount_cents,
            paid_currency=paid_currency,
        )

        user = await self.session.get(TelegramUser, order.user_id)
        if not user:
            raise PaymentError(f"User not found for order: {order_no}")

        plan = await self.get_plan(order.plan_id)
        if not order.plan_snapshot:
            # Pre-upgrade pending orders have no trustworthy entitlement snapshot.
            raise PaymentError(
                "Legacy order needs an audited plan snapshot before fulfillment",
                code="snapshot_required",
            )
        plan = self._snapshot_as_plan(order.plan_snapshot)

        receipt = (
            await self.session.execute(
                select(PaymentReceipt).where(
                    PaymentReceipt.provider == (order.payment_provider or "manual"),
                    PaymentReceipt.event_id == provider_tx_id,
                )
            )
        ).scalar_one_or_none()
        if receipt and receipt.order_id != order.id:
            raise PaymentError(
                "Payment event already belongs to another order",
                code="payment_event_conflict",
            )
        if not receipt:
            self.session.add(
                PaymentReceipt(
                    provider=order.payment_provider or "manual",
                    event_id=provider_tx_id,
                    order_id=order.id,
                )
            )
            await self.session.flush()
        order.provider_tx_id = provider_tx_id
        order.status = OrderStatus.PAID.value
        order.paid_at = now_utc()

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

    async def cancel_expired_order(self, order_no: str) -> Order | None:
        """Cancel an expired PENDING order (idempotent).

        Returns the cancelled order, or ``None`` when the order is already
        gone or already in a terminal state — callers should treat this as
        success because the desired end-state (order not active) is already
        reached.
        """
        stmt = select(Order).where(Order.order_no == order_no)
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if not order:
            logger.info(
                "cancel_expired_order: order not found, already gone: {}",
                order_no,
            )
            return None

        if order.status != OrderStatus.PENDING.value:
            logger.info(
                "cancel_expired_order: order {} already in status {}, skip",
                order_no,
                order.status,
            )
            return None

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

    async def cancel_and_commit_if_needed(self, order_no: str) -> Order | None:
        """Cancel an expired order and commit when it was actually cancelled.

        Thin wrapper around :meth:`cancel_expired_order` shared by webhook
        handlers: commits the session only when the order is genuinely
        cancelled (non-None result). When the order is already gone or in a
        terminal state the session is left uncommitted, so callers can build
        their response regardless. Returns the cancelled order, or ``None``.
        """
        if not order_no:
            return None
        result = await self.cancel_expired_order(order_no)
        if result:
            await self.session.commit()
        return result

    async def cancel_order(self, order_no: str, user_id: int) -> Order:
        """用户主动取消 pending 订单，同时通知网关"""
        stmt = select(Order).where(
            and_(
                Order.order_no == order_no,
                Order.user_id == user_id,
            )
        )
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if not order:
            raise PaymentError(f"Order not found: {order_no}")

        if order.status != OrderStatus.PENDING.value:
            raise PaymentError(f"Cannot cancel order in status: {order.status}")

        # 尝试通知支付网关取消
        if order.provider_tx_id and order.payment_provider:
            try:
                from backend.services.payment import get_gateway

                gateway = await get_gateway(order.payment_provider)
                result = await gateway.cancel_payment(order.provider_tx_id)
                if not result.success:
                    logger.warning(
                        "Gateway cancel failed for {}: {}",
                        order_no,
                        result.error_message,
                    )
            except Exception as e:
                logger.warning("Gateway cancel error for {}: {}", order_no, e)

        order.status = OrderStatus.CANCELLED.value
        await self._log_payment(
            order_id=order.id,
            user_id=order.user_id,
            action=PaymentAction.EXPIRE,
            detail="Order cancelled by user",
        )
        await self.session.flush()

        logger.info("Order cancelled by user: order_no={}", order_no)
        return order

    async def process_refund(
        self,
        order_id: int,
        amount_cents: int | None = None,
        operator_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """Refund an auditable source, durably staging external requests.

        External refunds deliberately commit their intent/hold before making
        the request and commit its observed outcome before final bookkeeping.
        An interrupted or ambiguous upstream request is quarantined for manual
        provider reconciliation; it is never silently replayed.
        """
        from backend.models.billing_models import BillingTransaction
        from backend.services.billing_pricing import (
            credits_to_units,
            proportional_units,
        )
        from backend.services.billing_service import BillingError, BillingService
        from backend.services.payment import EXTERNAL_PAYMENT_PROVIDERS, get_gateway

        order = await self.session.get(Order, order_id, with_for_update=True)
        if not order:
            raise PaymentError(f"Order not found: {order_id}")
        if order.status == OrderStatus.REFUNDED.value:
            return order
        if order.status != OrderStatus.FULFILLED.value:
            raise PaymentError(
                f"Cannot refund order in status: {order.status}, only FULFILLED orders can be refunded"
            )
        if not order.plan_snapshot:
            raise PaymentError(
                "Legacy refund requires an audited purchased entitlement snapshot",
                code="snapshot_required",
            )
        metadata = json.loads(order.metadata_json or "{}")
        if (
            order.payment_provider == "alipay"
            and str(metadata.get("gateway_currency", order.currency)).upper() != "CNY"
        ):
            raise PaymentError(
                "Alipay checkout currency requires provider reconciliation",
                code="invalid_currency",
            )
        key = idempotency_key or f"order:{order.id}:full-refund"
        if not isinstance(key, str) or not key or len(key) > 160:
            raise PaymentError("Invalid refund idempotency key")
        previous = (
            await self.session.execute(
                select(PaymentRefundAttempt)
                .where(PaymentRefundAttempt.idempotency_key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if previous:
            if previous.order_id != order.id or (
                amount_cents is not None and previous.amount_cents != amount_cents
            ):
                raise PaymentError(
                    "Refund request key conflicts with another order or amount"
                )
            if previous.status == "succeeded":
                return order
            if previous.status == "upstream_succeeded":
                return await self._finalize_refund(previous, order)
            raise PaymentError(
                "Refund outcome needs provider reconciliation",
                code="refund_reconciliation_required",
            )
        refunded_cents = order.refunded_amount_cents or 0
        requested_cents = (
            amount_cents
            if amount_cents is not None
            else order.amount_cents - refunded_cents
        )
        if (
            isinstance(requested_cents, bool)
            or not isinstance(requested_cents, int)
            or requested_cents < 0
            or requested_cents > order.amount_cents - refunded_cents
        ):
            raise PaymentError("Invalid refund amount", code="invalid_refund_amount")
        full = refunded_cents + requested_cents == order.amount_cents
        if not full:
            if (
                await get_dynamic_config("payment_partial_refund_policy")
                != "proportional_unused_credits"
            ):
                raise PaymentError(
                    "Partial refunds are disabled by the configured entitlement policy",
                    code="partial_refund_unsupported",
                )
            snapshot = order.plan_snapshot
            if (
                any(snapshot.get(key, 0) for key in self._plan_quota_values(Plan()))
                or snapshot.get("rate_limits")
                or snapshot.get("concurrency_limit")
                or Decimal(snapshot.get("credit_grant", "0")) <= 0
            ):
                raise PaymentError(
                    "Partial refunds require a Credits-only purchased snapshot",
                    code="partial_refund_unsupported",
                )
            if not idempotency_key:
                raise PaymentError(
                    "Partial refund requires a distinct audited request idempotency key",
                    code="refund_key_required",
                )
        active = (
            await self.session.execute(
                select(PaymentRefundAttempt).where(
                    PaymentRefundAttempt.active_order_id == order.id
                )
            )
        ).scalar_one_or_none()
        if active:
            raise PaymentError(
                "An earlier refund needs provider reconciliation",
                code="refund_reconciliation_required",
            )
        purchase = (
            await self.session.execute(
                select(BillingTransaction).where(
                    BillingTransaction.idempotency_key == f"order:{order.id}:credits"
                )
            )
        ).scalar_one_or_none()
        target_units = 0
        if purchase:
            prior_reversed = -(
                await self.session.execute(
                    select(
                        func.coalesce(func.sum(BillingTransaction.delta_units), 0)
                    ).where(BillingTransaction.reference_transaction_id == purchase.id)
                )
            ).scalar_one()
            target_cumulative = (
                credits_to_units(order.plan_snapshot["credit_grant"])
                if full
                else proportional_units(
                    credits_to_units(order.plan_snapshot["credit_grant"]),
                    refunded_cents + requested_cents,
                    order.amount_cents,
                )
            )
            target_units = target_cumulative - prior_reversed
            if target_units < 0:
                raise PaymentError("Refund source reconciliation mismatch")
        attempt = PaymentRefundAttempt(
            order_id=order.id,
            active_order_id=order.id,
            idempotency_key=key,
            amount_cents=requested_cents,
            currency=order.currency,
            status="pending",
            actor_id=operator_id,
            credit_transaction_id=purchase.id if purchase else None,
            credit_hold_units=target_units,
            evidence={
                "requested_total_cents": refunded_cents + requested_cents,
                "full": full,
            },
        )
        self.session.add(attempt)
        await self.session.flush()
        self.session.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{key}:intent",
                status="pending",
                actor_id=operator_id,
                evidence=attempt.evidence,
            )
        )
        if target_units:
            try:
                await BillingService(self.session).hold_purchase_refund(
                    purchase.id, f"{key}:hold", units=target_units
                )
            except BillingError as exc:
                raise PaymentError(str(exc), code=exc.code) from exc
        if (
            order.payment_provider not in EXTERNAL_PAYMENT_PROVIDERS
            or not order.provider_tx_id
        ):
            attempt.status = "upstream_succeeded"
            return await self._finalize_refund(attempt, order)
        metadata = json.loads(order.metadata_json) if order.metadata_json else {}
        gateway_total = metadata.get("gateway_amount_cents", order.amount_cents)
        gateway_prior = (
            proportional_units(
                gateway_total, refunded_cents, order.amount_cents, half_even=True
            )
            if order.amount_cents
            else 0
        )
        gateway_target = (
            proportional_units(
                gateway_total,
                refunded_cents + requested_cents,
                order.amount_cents,
                half_even=True,
            )
            if order.amount_cents
            else 0
        )
        attempt.gateway_amount_cents = gateway_target - gateway_prior
        attempt.gateway_currency = str(metadata.get("gateway_currency", order.currency))
        provider, provider_tx_id = order.payment_provider, order.provider_tx_id
        await self.session.flush()
        await self.session.commit()  # release order and wallet locks before I/O
        try:
            gateway = await get_gateway(provider)
            result = await gateway.refund(
                provider_tx_id=provider_tx_id,
                amount_cents=attempt.gateway_amount_cents,
                reason="requested_by_customer",
                idempotency_key=key,
            )
        except BaseException:
            # Cancellation may happen after bytes were sent. The durable pending
            # intent and hold remain, even if recording 'unknown' is interrupted.
            await self.session.rollback()
            raise
        order, attempt = await self._locked_refund_state(order_id, attempt.id)
        # A verified callback may have completed while the upstream request was
        # in flight. Its committed outcome supersedes a later API observation.
        if attempt.status == "succeeded":
            await self.session.commit()
            return order
        if attempt.status == "upstream_succeeded":
            result_order = await self._finalize_refund(attempt, order)
            await self.session.commit()
            return result_order
        if attempt.status == "failed":
            raise PaymentError(
                "Provider observations conflict with a resolved refund",
                code="refund_outcome_conflict",
            )
        attempt.provider_refund_id = result.refund_id or None
        if not result.success or result.status not in {"succeeded", "approved"}:
            attempt.status = "unknown"
            attempt.evidence = {
                **attempt.evidence,
                "gateway_error": "Upstream refund was not confirmed; inspect provider independently",
            }
            self.session.add(
                PaymentRefundAttemptEvent(
                    attempt_id=attempt.id,
                    event_key=f"{key}:unknown",
                    status="unknown",
                    actor_id=operator_id,
                    evidence=attempt.evidence,
                )
            )
            await self.session.commit()
            raise PaymentError(
                "Refund outcome needs provider reconciliation",
                code="refund_reconciliation_required",
            )
        attempt.status = "upstream_succeeded"
        attempt.provider_refund_id = result.refund_id
        self.session.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{key}:upstream",
                status="upstream_succeeded",
                actor_id=operator_id,
                evidence={"provider_refund_id": result.refund_id},
            )
        )
        await self.session.commit()
        order, attempt = await self._locked_refund_state(order_id, attempt.id)
        result_order = await self._finalize_refund(attempt, order)
        await self.session.commit()
        return result_order

    async def _locked_refund_state(self, order_id: int, attempt_id: int):
        from backend.services.billing_service import BillingService

        # Use the same order -> wallet -> attempt lock order as callback replay.
        # Refresh mutable ORM values after an I/O or commit boundary.
        order = await self.session.get(
            Order, order_id, with_for_update=True, populate_existing=True
        )
        await BillingService(self.session).get_wallet(order.user_id)
        attempt = await self.session.get(
            PaymentRefundAttempt,
            attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        return order, attempt

    async def _finalize_refund(
        self, attempt: PaymentRefundAttempt, order: Order
    ) -> Order:
        from backend.services.billing_service import BillingService

        if attempt.status == "succeeded":
            return order
        if attempt.status != "upstream_succeeded":
            raise PaymentError(
                "Upstream refund has not been confirmed",
                code="refund_reconciliation_required",
            )
        if attempt.credit_hold_units:
            service = BillingService(self.session)
            await service.finalize_purchase_refund(
                attempt.credit_transaction_id,
                attempt.credit_hold_units,
                f"{attempt.idempotency_key}:hold",
                f"{attempt.idempotency_key}:ledger",
                actor_id=attempt.actor_id,
                reason="Verified order refund",
            )
        if attempt.gateway_amount_cents is not None:
            metadata = json.loads(order.metadata_json or "{}")
            metadata["refunded_gateway_amount_cents"] = (
                metadata.get("refunded_gateway_amount_cents", 0)
                + attempt.gateway_amount_cents
            )
            order.metadata_json = json.dumps(metadata)
        order.refunded_amount_cents = (
            order.refunded_amount_cents or 0
        ) + attempt.amount_cents
        if order.refunded_amount_cents == order.amount_cents:
            await LegacyEntitlementService(self.session).revoke_order(
                order.id, actor_id=attempt.actor_id
            )
            order.status = OrderStatus.REFUNDED.value
            subscription = (
                await self.session.execute(
                    select(UserSubscription)
                    .where(
                        UserSubscription.user_id == order.user_id,
                        UserSubscription.plan_id == order.plan_id,
                        UserSubscription.quota_application_version == 2,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if subscription:
                remaining_ends = (
                    (
                        await self.session.execute(
                            select(LegacyEntitlement.expires_at)
                            .where(
                                LegacyEntitlement.user_id == order.user_id,
                                LegacyEntitlement.revoked_at.is_(None),
                                LegacyEntitlement.snapshot["id"].as_integer()
                                == order.plan_id,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                latest = max(
                    (value for value in remaining_ends if value is not None),
                    default=None,
                )
                if latest and latest > now_utc():
                    subscription.expires_at = latest
                else:
                    subscription.status = SubscriptionStatus.EXPIRED.value
        attempt.status = "succeeded"
        attempt.active_order_id = None
        self.session.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{attempt.idempotency_key}:settled",
                status="succeeded",
                actor_id=attempt.actor_id,
                evidence={"refunded_total_cents": order.refunded_amount_cents},
            )
        )
        await self._log_payment(
            order_id=order.id,
            user_id=order.user_id,
            action=PaymentAction.REFUND,
            detail=f"Verified refund attempt {attempt.id}; amount={attempt.amount_cents} {attempt.currency}",
            operator_id=attempt.actor_id,
        )
        await self.session.flush()
        return order

    async def reconcile_refund(
        self,
        attempt_id: int,
        *,
        operator_id: int,
        upstream_status: str,
        evidence: str,
        provider_refund_id: str | None = None,
    ) -> Order:
        from backend.services.billing_service import BillingService

        actor = await self.session.get(TelegramUser, operator_id)
        if not actor or not actor.is_active or actor.role != "super_admin":
            raise PaymentError("Refund reconciliation requires an active super-admin")
        if not evidence.strip() or upstream_status not in {"refunded", "not_refunded"}:
            raise PaymentError(
                "A confirmed provider outcome and audit evidence are required"
            )
        attempt = await self.session.get(
            PaymentRefundAttempt, attempt_id, with_for_update=True
        )
        if not attempt:
            raise PaymentError("Refund attempt not found")
        order = await self.session.get(Order, attempt.order_id, with_for_update=True)
        if attempt.status == "succeeded":
            return order
        if attempt.status not in {"pending", "unknown", "upstream_succeeded"}:
            raise PaymentError("Refund attempt is already resolved")
        attempt.evidence = {
            **attempt.evidence,
            "reconciliation": evidence,
            "reconciled_by": operator_id,
        }
        attempt.actor_id = operator_id
        self.session.add(
            PaymentRefundAttemptEvent(
                attempt_id=attempt.id,
                event_key=f"{attempt.idempotency_key}:reconciled",
                status=upstream_status,
                actor_id=operator_id,
                evidence=attempt.evidence,
            )
        )
        if upstream_status == "refunded":
            attempt.status = "upstream_succeeded"
            attempt.provider_refund_id = (
                provider_refund_id or attempt.provider_refund_id
            )
            return await self._finalize_refund(attempt, order)
        if attempt.status == "upstream_succeeded":
            raise PaymentError(
                "A recorded successful upstream refund cannot be declared unrefunded"
            )
        if attempt.credit_hold_units:
            await BillingService(self.session).release_purchase_refund(
                attempt.credit_transaction_id,
                attempt.credit_hold_units,
                f"{attempt.idempotency_key}:hold",
            )
        attempt.status = "failed"
        attempt.active_order_id = None
        await self.session.flush()
        return order

    async def submit_refund_request(
        self,
        order_id: int,
        user_id: int,
        reason: str = "",
    ) -> RefundRequest:
        """Create a pending refund request for a user's fulfilled paid order."""
        order = await self.session.get(
            Order, order_id, with_for_update=True, populate_existing=True
        )
        if not order or order.user_id != user_id:
            raise PaymentError("Order not found or not refundable")

        if order.status != OrderStatus.FULFILLED.value:
            raise PaymentError("Only fulfilled orders can request refund")

        if order.amount_cents <= 0:
            raise PaymentError("Free or manual grant orders cannot be refunded")
        remaining_cents = order.amount_cents - (order.refunded_amount_cents or 0)
        if remaining_cents <= 0:
            raise PaymentError(
                "No refundable amount remains", code="invalid_refund_amount"
            )

        existing_stmt = select(RefundRequest).where(
            and_(
                RefundRequest.order_id == order_id,
                RefundRequest.user_id == user_id,
                # 仅阻止待审核中的重复请求；FAILED 表示执行失败，允许用户重新提交。
                RefundRequest.status == RefundRequestStatus.PENDING.value,
            )
        )
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            raise PaymentError(
                "Refund request already exists",
                code="DUPLICATE_REFUND_REQUEST",
            )

        user = await self.session.get(TelegramUser, user_id)
        refund_request = RefundRequest(
            order_id=order.id,
            user_id=user_id,
            amount_cents=remaining_cents,
            currency=order.currency,
            status=RefundRequestStatus.PENDING.value,
            reason=(reason or "").strip() or None,
        )
        refund_request.order = order
        if user:
            refund_request.user = user

        self.session.add(refund_request)
        await self.session.flush()
        logger.info(
            "Refund request submitted: request_id={}, order_id={}, user_id={}",
            refund_request.id,
            order_id,
            user_id,
        )
        return refund_request

    async def list_refund_requests(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RefundRequest], int]:
        """List refund requests for admin review."""
        conditions = []
        if status:
            conditions.append(RefundRequest.status == status)

        count_stmt = select(func.count()).select_from(RefundRequest)
        stmt = (
            select(RefundRequest)
            .options(
                selectinload(RefundRequest.order).selectinload(Order.plan),
                selectinload(RefundRequest.user),
                selectinload(RefundRequest.reviewer),
            )
            .order_by(RefundRequest.requested_at.desc(), RefundRequest.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
            stmt = stmt.where(and_(*conditions))

        total = (await self.session.execute(count_stmt)).scalar() or 0
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def list_refund_requests_for_orders(
        self,
        user_id: int,
        order_ids: list[int],
    ) -> dict[int, RefundRequest]:
        """Return the latest refund request per order for the user's order list."""
        if not order_ids:
            return {}
        stmt = (
            select(RefundRequest)
            .where(
                and_(
                    RefundRequest.user_id == user_id,
                    RefundRequest.order_id.in_(order_ids),
                )
            )
            .order_by(RefundRequest.requested_at.desc(), RefundRequest.id.desc())
        )
        result = await self.session.execute(stmt)
        requests_by_order: dict[int, RefundRequest] = {}
        for refund_request in result.scalars().all():
            requests_by_order.setdefault(refund_request.order_id, refund_request)
        return requests_by_order

    async def get_refund_request(
        self,
        request_id: int,
    ) -> RefundRequest | None:
        stmt = (
            select(RefundRequest)
            .options(
                selectinload(RefundRequest.order).selectinload(Order.plan),
                selectinload(RefundRequest.user),
                selectinload(RefundRequest.reviewer),
            )
            .where(RefundRequest.id == request_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def approve_refund_request(
        self,
        request_id: int,
        reviewer_id: int,
        review_note: str = "",
    ) -> RefundRequest:
        """Approve a refund request and execute the real refund."""
        refund_request = await self.get_refund_request(request_id)
        if not refund_request:
            raise PaymentError(f"Refund request not found: {request_id}")

        if refund_request.status not in {
            RefundRequestStatus.PENDING.value,
            RefundRequestStatus.FAILED.value,
        }:
            raise PaymentError(
                f"Cannot approve refund request in status: {refund_request.status}"
            )

        now = now_utc()
        refund_request.reviewed_by = reviewer_id
        refund_request.reviewed_at = now
        refund_request.review_note = (review_note or "").strip() or None

        try:
            key = f"refund-request:{refund_request.id}"
            order = await self.session.get(
                Order,
                refund_request.order_id,
                with_for_update=True,
                populate_existing=True,
            )
            if order is None:
                raise PaymentError("Order not found or not refundable")
            attempt = (
                await self.session.execute(
                    select(PaymentRefundAttempt)
                    .where(PaymentRefundAttempt.idempotency_key == key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if attempt is None:
                remaining_cents = order.amount_cents - (
                    order.refunded_amount_cents or 0
                )
                if remaining_cents <= 0:
                    raise PaymentError(
                        "No refundable amount remains", code="invalid_refund_amount"
                    )
                # A separately verified refund may arrive while review is pending.
                # Reduce the review request before creating its financial intent;
                # an existing attempt's amount and idempotency identity never change.
                refund_request.amount_cents = min(
                    refund_request.amount_cents, remaining_cents
                )
            order = await self.process_refund(
                order_id=refund_request.order_id,
                amount_cents=refund_request.amount_cents,
                operator_id=reviewer_id,
                idempotency_key=key,
            )
            refund_request.order = order
            refund_request.status = RefundRequestStatus.APPROVED.value
            refund_request.processed_at = now_utc()
            refund_request.error_message = None
            await self.session.flush()
            logger.info(
                "Refund request approved: request_id={}, reviewer_id={}",
                request_id,
                reviewer_id,
            )
        except Exception as exc:
            refund_request.status = RefundRequestStatus.FAILED.value
            refund_request.error_message = str(exc)
            logger.warning(
                "Refund request failed during approval: request_id={}, error={}",
                request_id,
                exc,
            )
            try:
                await self.session.flush()
            except Exception as flush_exc:
                logger.error(
                    "Failed to flush refund failure state: request_id={}, error={}",
                    request_id,
                    flush_exc,
                )
                raise

        return refund_request

    async def reject_refund_request(
        self,
        request_id: int,
        reviewer_id: int,
        review_note: str = "",
    ) -> RefundRequest:
        """Reject a pending refund request without executing refund."""
        refund_request = await self.get_refund_request(request_id)
        if not refund_request:
            raise PaymentError(f"Refund request not found: {request_id}")

        if refund_request.status not in {
            RefundRequestStatus.PENDING.value,
            RefundRequestStatus.FAILED.value,
        }:
            raise PaymentError(
                f"Cannot reject refund request in status: {refund_request.status}"
            )

        refund_request.status = RefundRequestStatus.REJECTED.value
        refund_request.reviewed_by = reviewer_id
        refund_request.reviewed_at = now_utc()
        refund_request.review_note = (review_note or "").strip() or None
        refund_request.error_message = None
        await self.session.flush()
        logger.info(
            "Refund request rejected: request_id={}, reviewer_id={}",
            request_id,
            reviewer_id,
        )
        return refund_request

    async def grant_plan_to_user(
        self,
        user_id: int,
        plan_id: int,
        operator_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        """管理员手动为用户充值"""
        if idempotency_key:
            if not isinstance(idempotency_key, str) or len(idempotency_key) > 191:
                raise PaymentError("Invalid admin grant idempotency key")
            previous = (
                await self.session.execute(
                    select(Order)
                    .where(Order.grant_idempotency_key == idempotency_key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if previous:
                previous_operator = json.loads(previous.metadata_json or "{}").get(
                    "grant_operator_id"
                )
                if (
                    previous.user_id != user_id
                    or previous.plan_id != plan_id
                    or previous_operator != operator_id
                ):
                    raise PaymentError(
                        "Grant key conflicts with another financial intent",
                        code="idempotency_conflict",
                    )
                return previous
        plan = await self.get_plan(plan_id)
        if not plan:
            raise PaymentError(f"Plan not found: {plan_id}")

        user = await self.session.get(TelegramUser, user_id)
        if not user:
            raise PaymentError("User not found")

        order = Order(
            order_no=self._generate_order_no(),
            grant_idempotency_key=idempotency_key,
            metadata_json=json.dumps({"grant_operator_id": operator_id}),
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=0,
            currency=plan.currency,
            status=OrderStatus.PAID.value,
            payment_provider="manual",
            paid_at=now_utc(),
            plan_snapshot=self._snapshot_plan(plan),
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

    async def get_active_subscription(self, user_id: int) -> UserSubscription | None:
        await self.expire_due_subscriptions(user_id)
        stmt = select(UserSubscription).where(
            and_(
                UserSubscription.user_id == user_id,
                UserSubscription.status == SubscriptionStatus.ACTIVE.value,
                UserSubscription.expires_at > now_utc(),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def expire_due_subscriptions(self, user_id: int | None = None) -> int:
        conditions = [
            UserSubscription.status == SubscriptionStatus.ACTIVE.value,
            UserSubscription.expires_at <= now_utc(),
        ]
        if user_id is not None:
            conditions.append(UserSubscription.user_id == user_id)

        stmt = (
            select(UserSubscription, TelegramUser, Plan)
            .join(TelegramUser, UserSubscription.user_id == TelegramUser.id)
            .join(Plan, UserSubscription.plan_id == Plan.id)
            .where(and_(*conditions))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        for subscription, user, plan in rows:
            await self._expire_subscription(subscription, user, plan)

        await LegacyEntitlementService(self.session).expire_due(user_id)
        return len(rows)

    async def list_user_orders(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Order], int]:
        count_stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.user_id == user_id, Order.hidden_by_user_at.is_(None))
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(Order)
            .where(Order.user_id == user_id, Order.hidden_by_user_at.is_(None))
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
        operator_id: int | None = None,
    ) -> Order:
        """Fulfill a purchased snapshot in the caller's transaction."""
        if order.status == OrderStatus.FULFILLED.value:
            return order
        from backend.services.billing_service import BillingService

        await BillingService(self.session).get_wallet(user.id)
        await self.session.flush()
        await self.session.refresh(user, with_for_update=True)
        snapshot = order.plan_snapshot
        if not snapshot:
            raise PaymentError(
                "Order has no purchased plan snapshot", code="snapshot_required"
            )
        purchased = self._snapshot_as_plan(snapshot)
        starts_at = now_utc()
        expires_at = None
        if purchased.plan_type == PlanType.SUBSCRIPTION.value:
            legacy_subscription = (
                await self.session.execute(
                    select(UserSubscription)
                    .where(
                        UserSubscription.user_id == user.id,
                        UserSubscription.plan_id == purchased.id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                legacy_subscription
                and legacy_subscription.quota_application_version != 2
            ):
                await self._preserve_legacy_subscription(legacy_subscription, user)
            # Each paid period has one source. Renewals of the same plan queue
            # behind paid periods, while distinct plans can overlap.
            paid_ends = (
                (
                    await self.session.execute(
                        select(LegacyEntitlement.expires_at)
                        .where(
                            LegacyEntitlement.user_id == user.id,
                            LegacyEntitlement.revoked_at.is_(None),
                            LegacyEntitlement.snapshot["id"].as_integer()
                            == purchased.id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            previous_end = max(
                (value for value in paid_ends if value is not None), default=None
            )
            if previous_end and previous_end > starts_at:
                starts_at = previous_end
            expires_at = starts_at + timedelta(days=purchased.duration_days)
        await LegacyEntitlementService(self.session).grant(
            user.id,
            {
                **snapshot,
                "funding_amount_cents": order.amount_cents,
                "funding_kind": order.payment_provider,
            },
            f"order:{order.id}",
            order_id=order.id,
            starts_at=starts_at,
            expires_at=expires_at,
            actor_id=operator_id,
        )
        if Decimal(snapshot.get("credit_grant", "0")) > 0:
            from backend.services.billing_service import BillingService

            await BillingService(self.session).grant(
                user.id,
                snapshot["credit_grant"],
                f"order:{order.id}:credits",
                kind="grant" if order.payment_provider == "manual" else "purchase",
                order_id=order.id,
                actor_id=operator_id,
                reason=f"Plan {snapshot['name']}",
                snapshot=snapshot,
            )
        order.status = OrderStatus.FULFILLED.value
        order.fulfilled_at = now_utc()
        if order.hidden_by_user_at is not None:
            order.hidden_by_user_at = None
            await self._log_payment(
                order_id=order.id,
                user_id=user.id,
                action="restore_visibility",
                detail="Hidden order restored after verified fulfillment",
                operator_id=operator_id,
            )
        if purchased.plan_type == PlanType.SUBSCRIPTION.value:
            await self._upsert_subscription(
                user.id, purchased, order.id, expires_at=expires_at
            )
        await self._log_payment(
            order_id=order.id,
            user_id=user.id,
            action=PaymentAction.FULFILL,
            detail=f"Plan {snapshot['name']} fulfilled from snapshot",
            operator_id=operator_id,
        )
        await self.session.flush()
        return order

    @staticmethod
    def _validate_billing_plan(credit_grant, rate_limits, concurrency_limit):
        from backend.services.billing_pricing import credits_to_units

        try:
            units = credits_to_units(credit_grant)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PaymentError(
                "credit_grant must be exact and within the wallet range"
            ) from exc
        if units < 0:
            raise PaymentError("credit_grant must be nonnegative")
        if rate_limits is not None and (
            not isinstance(rate_limits, dict)
            or any(
                key not in RATE_LIMIT_KEYS
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in rate_limits.items()
            )
        ):
            raise PaymentError("Invalid rate limit configuration")
        if concurrency_limit is not None and (
            isinstance(concurrency_limit, bool)
            or not isinstance(concurrency_limit, int)
            or concurrency_limit < 1
        ):
            raise PaymentError("concurrency_limit must be a positive integer")

    def _snapshot_plan(self, plan: Plan) -> dict:
        self._validate_billing_plan(
            plan.credit_grant or 0, plan.rate_limits, plan.concurrency_limit
        )
        if plan.plan_type == PlanType.SUBSCRIPTION.value and (
            not plan.duration_days or plan.duration_days <= 0
        ):
            raise PaymentError(
                "Subscription requires a positive confirmed duration_days"
            )
        snapshot = {
            "id": plan.id,
            "name": plan.name,
            "plan_type": plan.plan_type,
            "price_cents": plan.price_cents,
            "currency": plan.currency,
            "duration_days": plan.duration_days,
            "credit_grant": str(plan.credit_grant or 0),
            "rate_limits": plan.rate_limits or {},
            "concurrency_limit": plan.concurrency_limit,
            "version": 2,
        }
        quota_values = self._plan_quota_values(plan)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in quota_values.values()
        ):
            raise PaymentError("Legacy allowance fields must be nonnegative integers")
        snapshot.update(quota_values)
        return snapshot

    @staticmethod
    def _snapshot_as_plan(snapshot: dict):
        return SimpleNamespace(**snapshot)

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
        """Compatibility guard: grants must have an order-linked snapshot.

        Never infer a purchased source or silently discard a bonus in this old
        private API. All callers must use _fulfill_order instead.
        """
        raise PaymentError(
            "Plan application requires a source-linked order", code="order_required"
        )

    async def _preserve_legacy_subscription(
        self, subscription: UserSubscription, user: TelegramUser
    ):
        """Separate an existing applied snapshot before extending its summary.

        Only persisted applied_* values are reliable. Historical unlimited
        bonus use cannot reveal an exact remaining count; conservatively
        preserve the originally purchased one-time bonus without backbilling.
        """
        values = {
            key: getattr(subscription, f"applied_{key}")
            for key in self._plan_quota_values(Plan())
        }
        if any(value is None or value < 0 for value in values.values()):
            raise PaymentError(
                "Legacy subscription snapshot requires audit",
                code="legacy_audit_required",
            )
        for prefix, user_prefix in (
            ("pr", ""),
            ("issue", "issue_"),
            ("agent", "agent_"),
        ):
            for period in ("daily", "weekly", "monthly"):
                amount = values[f"{prefix}_{period}_add"] + (
                    values[f"{prefix}_quota_bonus"] if period == "daily" else 0
                )
                field = f"{user_prefix}{period}_quota"
                if getattr(user, field) < amount:
                    raise PaymentError(
                        "Legacy quota baseline conflicts with applied snapshot; audit required",
                        code="legacy_audit_required",
                    )
        for prefix, user_prefix in (
            ("pr", ""),
            ("issue", "issue_"),
            ("agent", "agent_"),
        ):
            for period in ("daily", "weekly", "monthly"):
                field = f"{user_prefix}{period}_quota"
                setattr(
                    user,
                    field,
                    getattr(user, field)
                    - values[f"{prefix}_{period}_add"]
                    - (values[f"{prefix}_quota_bonus"] if period == "daily" else 0),
                )
        snapshot = {
            "id": subscription.plan_id,
            "name": "Preserved historical subscription",
            "credit_grant": "0",
            "rate_limits": {},
            "version": 2,
            "source": "persisted_subscription_snapshot",
            **values,
        }
        await LegacyEntitlementService(self.session).grant(
            user.id,
            snapshot,
            f"subscription:{subscription.id}:opening",
            starts_at=subscription.started_at or now_utc(),
            expires_at=subscription.expires_at,
            actor_id=None,
        )
        subscription.quota_application_version = 2
        subscription.granted_snapshot = snapshot
        for key in values:
            setattr(subscription, f"applied_{key}", 0)
        await self.session.flush()

    async def _upsert_subscription(
        self,
        user_id: int,
        plan: Plan,
        order_id: int,
        *,
        expires_at: datetime | None = None,
    ) -> UserSubscription:
        stmt = (
            select(UserSubscription)
            .where(
                and_(
                    UserSubscription.user_id == user_id,
                    UserSubscription.plan_id == plan.id,
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()

        values = {key: 0 for key in self._plan_quota_values(plan)}
        if existing:
            existing.status = SubscriptionStatus.ACTIVE.value
            existing.expires_at = expires_at or now_utc() + timedelta(
                days=plan.duration_days
            )
            existing.quota_application_version = 2
            existing.granted_snapshot = self._snapshot_plan(plan)
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
            expires_at=expires_at or now_utc() + timedelta(days=plan.duration_days),
            quota_application_version=2,
            granted_snapshot=self._snapshot_plan(plan),
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

    async def _clawback_plan_quotas(
        self, user: TelegramUser, plan: Plan
    ) -> TelegramUser:
        raise PaymentError(
            "Quota revocation requires its source order", code="order_required"
        )

    async def _expire_subscription(
        self, subscription: UserSubscription, user: TelegramUser, plan: Plan
    ) -> UserSubscription:
        """订阅过期时扣回已发放的套餐配额"""
        if subscription.status != SubscriptionStatus.ACTIVE.value:
            return subscription
        if subscription.quota_application_version == 2:
            subscription.status = SubscriptionStatus.EXPIRED.value
            await self.session.flush()
            return subscription
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
            "agent_weekly_add": getattr(subscription, "applied_agent_weekly_add", None),
            "agent_monthly_add": getattr(
                subscription, "applied_agent_monthly_add", None
            ),
        }

        if all(value is None for value in applied_values.values()):
            logger.warning(
                "Subscription {} has no historical grant snapshot; quota repair requires audit",
                subscription.id,
            )
            subscription.status = SubscriptionStatus.EXPIRED.value
            await self.session.flush()
            return subscription

        for prefix, user_prefix in (
            ("pr", ""),
            ("issue", "issue_"),
            ("agent", "agent_"),
        ):
            for period in ("daily", "weekly", "monthly"):
                source_amount = (applied_values[f"{prefix}_{period}_add"] or 0) + (
                    (applied_values[f"{prefix}_quota_bonus"] or 0)
                    if period == "daily"
                    else 0
                )
                if source_amount > getattr(user, f"{user_prefix}{period}_quota"):
                    raise PaymentError(
                        "Historical subscription snapshot conflicts with current baseline; audit required",
                        code="legacy_audit_required",
                    )

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
        detail: str | None = None,
        operator_id: int | None = None,
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
        now = now_utc()
        short_uuid = uuid.uuid4().hex[:8].upper()
        return f"ORD{now.strftime('%Y%m%d%H%M%S')}{short_uuid}"
