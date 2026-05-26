"""PaymentService 单元测试

覆盖：套餐管理、兑换码生成/兑换、配额发放、手动充值、边界条件。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.payment_models import (
    Plan,
    PlanType,
    OrderStatus,
    RedeemCode,
    RedeemCodeStatus,
    UserSubscription,
    SubscriptionStatus,
)
from backend.models.telegram_models import TelegramUser
from backend.services.payment_service import (
    PaymentError,
    PaymentService,
    is_payment_enabled,
)


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def svc(mock_session):
    return PaymentService(mock_session)


@pytest.fixture
def sample_user():
    user = MagicMock(spec=TelegramUser)
    user.id = 1
    user.telegram_id = 12345
    user.daily_quota = 10
    user.weekly_quota = 50
    user.monthly_quota = 200
    user.daily_used = 0
    user.weekly_used = 0
    user.monthly_used = 0
    user.issue_daily_quota = 20
    user.issue_weekly_quota = 80
    user.issue_monthly_quota = 300
    user.issue_daily_used = 0
    user.issue_weekly_used = 0
    user.issue_monthly_used = 0
    user.agent_daily_quota = 1
    user.agent_weekly_quota = 2
    user.agent_monthly_quota = 5
    user.agent_daily_used = 0
    user.agent_weekly_used = 0
    user.agent_monthly_used = 0
    return user


@pytest.fixture
def sample_plan():
    plan = Plan(
        id=1,
        name="10次PR包",
        plan_type=PlanType.ONE_TIME.value,
        price_cents=1000,
        pr_quota_bonus=10,
        is_active=True,
    )
    plan.id = 1
    return plan


@pytest.fixture
def sample_subscription_plan():
    plan = Plan(
        id=2,
        name="月卡",
        plan_type=PlanType.SUBSCRIPTION.value,
        price_cents=5000,
        duration_days=30,
        pr_daily_add=5,
        pr_monthly_add=100,
        is_active=True,
    )
    plan.id = 2
    return plan


@pytest.mark.asyncio
class TestPlanManagement:
    async def test_create_plan(self, svc, mock_session):
        plan = await svc.create_plan(
            name="测试套餐",
            plan_type="one_time",
            price_cents=1000,
            pr_quota_bonus=10,
        )
        assert plan.name == "测试套餐"
        assert plan.plan_type == "one_time"
        assert plan.price_cents == 1000
        mock_session.add.assert_called()
        mock_session.flush.assert_awaited()

    async def test_get_plan(self, svc, mock_session, sample_plan):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        result = await svc.get_plan(1)
        assert result == sample_plan

    async def test_update_plan_ignores_non_editable_fields(
        self, svc, mock_session, sample_plan
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        updated = await svc.update_plan(
            1, name="新名称", id=999, created_at=datetime.now(timezone.utc)
        )

        assert updated.name == "新名称"
        assert updated.id == 1

    async def test_update_plan_ignores_none_values(
        self, svc, mock_session, sample_plan
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        updated = await svc.update_plan(1, name=None)

        assert updated.name == "10次PR包"


@pytest.mark.asyncio
class TestRedeemCodeGeneration:
    async def test_generate_codes(self, svc, mock_session, sample_plan):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        codes = await svc.generate_redeem_codes(
            plan_id=1, count=5, batch_name="test_batch"
        )
        assert len(codes) == 5
        assert mock_session.add.call_count == 5
        for code in codes:
            assert len(code.code) == 16
            assert code.plan_id == 1
            assert code.batch_name == "test_batch"

    async def test_generate_codes_invalid_plan(self, svc, mock_session):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="Plan not found"):
            await svc.generate_redeem_codes(plan_id=999, count=5)

    async def test_generate_code_format(self):
        code = RedeemCode.generate_code()
        assert len(code) == 16
        assert code.isupper() or code.isalnum()


@pytest.mark.asyncio
class TestRedeemCodeUsage:
    async def test_redeem_success(self, svc, mock_session, sample_user, sample_plan):
        mock_session.get.return_value = sample_user

        redeem_code = RedeemCode(
            id=1,
            code="TESTCODE123456",
            plan_id=1,
            max_uses=1,
            used_count=0,
            status=RedeemCodeStatus.ACTIVE.value,
        )

        redeem_result = MagicMock()
        redeem_result.scalar_one_or_none.return_value = redeem_code
        plan_result = MagicMock()
        plan_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute = AsyncMock(side_effect=[redeem_result, plan_result])

        order = await svc.redeem_code(user_id=1, code="TESTCODE123456")

        assert order.status == OrderStatus.FULFILLED.value
        assert order.payment_provider == "redeem_code"
        assert order.provider_tx_id == "TESTCODE123456"
        assert redeem_code.used_count == 1
        assert redeem_code.status == RedeemCodeStatus.EXHAUSTED.value

    async def test_redeem_code_query_locks_redeem_row(
        self, svc, mock_session, sample_user, sample_plan
    ):
        mock_session.get.return_value = sample_user

        redeem_code = RedeemCode(
            id=1,
            code="LOCKCODE123456",
            plan_id=1,
            max_uses=1,
            used_count=0,
            status=RedeemCodeStatus.ACTIVE.value,
        )
        redeem_result = MagicMock()
        redeem_result.scalar_one_or_none.return_value = redeem_code
        plan_result = MagicMock()
        plan_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute = AsyncMock(side_effect=[redeem_result, plan_result])

        await svc.redeem_code(user_id=1, code="LOCKCODE123456")

        redeem_stmt = mock_session.execute.await_args_list[0].args[0]
        assert redeem_stmt._for_update_arg is not None

    async def test_redeem_invalid_code(self, svc, mock_session, sample_user):
        mock_session.get.return_value = sample_user

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="Invalid"):
            await svc.redeem_code(user_id=1, code="INVALID")

    async def test_redeem_expired_code(self, svc, mock_session, sample_user):
        mock_session.get.return_value = sample_user

        expired_code = RedeemCode(
            id=1,
            code="EXPIREDCODE",
            plan_id=1,
            max_uses=1,
            used_count=0,
            status=RedeemCodeStatus.ACTIVE.value,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_code
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="expired"):
            await svc.redeem_code(user_id=1, code="EXPIREDCODE")

    async def test_redeem_exhausted_code(self, svc, mock_session, sample_user):
        mock_session.get.return_value = sample_user

        exhausted_code = RedeemCode(
            id=1,
            code="USEDCODE",
            plan_id=1,
            max_uses=1,
            used_count=1,
            status=RedeemCodeStatus.ACTIVE.value,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = exhausted_code
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="fully used"):
            await svc.redeem_code(user_id=1, code="USEDCODE")


@pytest.mark.asyncio
class TestQuotaApplication:
    async def test_apply_one_time_bonus(
        self, svc, mock_session, sample_user, sample_plan
    ):
        result = await svc._apply_plan_to_user(sample_user, sample_plan)
        assert result.daily_quota == 20  # 10 + 10 bonus
        mock_session.flush.assert_awaited()

    async def test_apply_subscription_adds(
        self, svc, mock_session, sample_user, sample_subscription_plan
    ):
        result = await svc._apply_plan_to_user(sample_user, sample_subscription_plan)
        assert result.daily_quota == 15  # 10 + 5 daily_add
        assert result.monthly_quota == 300  # 200 + 100 monthly_add

    async def test_apply_issue_quota(self, svc, mock_session, sample_user):
        plan = Plan(
            id=3,
            name="Issue包",
            plan_type=PlanType.ONE_TIME.value,
            issue_quota_bonus=20,
            issue_weekly_add=10,
        )
        result = await svc._apply_plan_to_user(sample_user, plan)
        assert result.issue_daily_quota == 40  # 20 + 20 bonus
        assert result.issue_weekly_quota == 90  # 80 + 10


@pytest.mark.asyncio
class TestManualGrant:
    async def test_grant_success(self, svc, mock_session, sample_user, sample_plan):
        mock_session.get.side_effect = lambda model, pk: (
            sample_user if pk == 1 else sample_plan if pk == 1 else None
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        order = await svc.grant_plan_to_user(user_id=1, plan_id=1, operator_id=99)
        assert order.status == OrderStatus.FULFILLED.value
        assert order.payment_provider == "manual"
        assert order.amount_cents == 0

    async def test_grant_user_not_found(self, svc, mock_session, sample_plan):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result
        mock_session.get.return_value = None

        with pytest.raises(PaymentError, match="User not found"):
            await svc.grant_plan_to_user(user_id=999, plan_id=1)


@pytest.mark.asyncio
class TestSubscription:
    async def test_upsert_new_subscription(
        self, svc, mock_session, sample_subscription_plan
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        sub = await svc._upsert_subscription(1, sample_subscription_plan, 1)
        assert sub.status == SubscriptionStatus.ACTIVE.value
        mock_session.add.assert_called()
        mock_session.flush.assert_awaited()

    async def test_upsert_new_subscription_reactivates_existing_subscription(
        self, svc, mock_session, sample_subscription_plan
    ):
        existing = UserSubscription(
            user_id=1,
            plan_id=sample_subscription_plan.id,
            status=SubscriptionStatus.EXPIRED.value,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        sub = await svc._upsert_subscription(1, sample_subscription_plan, 1)

        assert sub is existing
        assert sub.status == SubscriptionStatus.ACTIVE.value
        assert sub.last_order_id == 1
        mock_session.add.assert_not_called()

    async def test_expire_subscription_keeps_zero_snapshot_without_plan_fallback(
        self, svc, mock_session, sample_user
    ):
        plan = Plan(
            id=2,
            name="变更后的月卡",
            plan_type=PlanType.SUBSCRIPTION.value,
            price_cents=5000,
            pr_daily_add=5,
            is_active=True,
        )
        subscription = UserSubscription(
            user_id=sample_user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE.value,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            applied_pr_daily_add=0,
            applied_pr_monthly_add=0,
        )
        sample_user.daily_quota = 10

        await svc._expire_subscription(subscription, sample_user, plan)

        assert sample_user.daily_quota == 10
        assert subscription.status == SubscriptionStatus.EXPIRED.value

    async def test_expire_due_subscriptions_restores_expired_user_quota(
        self, svc, mock_session, sample_user, sample_subscription_plan
    ):
        sample_user.daily_quota = 15
        sample_user.monthly_quota = 300
        subscription = UserSubscription(
            user_id=sample_user.id,
            plan_id=sample_subscription_plan.id,
            status=SubscriptionStatus.ACTIVE.value,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            applied_pr_daily_add=5,
            applied_pr_monthly_add=100,
        )
        mock_result = MagicMock()
        mock_result.all.return_value = [
            (subscription, sample_user, sample_subscription_plan)
        ]
        mock_session.execute.return_value = mock_result

        expired_count = await svc.expire_due_subscriptions(sample_user.id)

        assert expired_count == 1
        assert subscription.status == SubscriptionStatus.EXPIRED.value
        assert sample_user.daily_quota == 10
        assert sample_user.monthly_quota == 200


@pytest.mark.asyncio
class TestPaymentConfig:
    async def test_is_payment_enabled_reads_dynamic_config(self):
        with patch(
            "backend.services.payment_service.get_dynamic_config",
            new=AsyncMock(return_value=True),
        ) as mock_get_config:
            assert await is_payment_enabled() is True

        mock_get_config.assert_awaited_once_with("payment_enabled")


class TestOrderNumber:
    def test_order_no_format(self):
        order_no = PaymentService._generate_order_no()
        assert order_no.startswith("ORD")
        assert len(order_no) > 16


@pytest.mark.asyncio
class TestConfirmPayment:
    async def test_confirm_payment_success(self, svc, mock_session, sample_user):
        from backend.models.payment_models import Order

        plan = Plan(
            id=1,
            name="Test Plan",
            plan_type=PlanType.ONE_TIME.value,
            price_cents=1000,
            is_active=True,
            pr_quota_bonus=5,
        )
        order = Order(
            id=1,
            order_no="ORD20240101000000ABCD1234",
            user_id=sample_user.id,
            plan_id=plan.id,
            amount_cents=1000,
            status=OrderStatus.PENDING.value,
            payment_provider="stripe",
            provider_tx_id="cs_test_123",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = order
        mock_session.execute.return_value = mock_result
        mock_session.get = AsyncMock(side_effect=lambda model, pk: {
            id(sample_user): sample_user,
            id(plan): plan,
        }.get(id(model) if hasattr(model, '__hash__') else pk))

        # Override get to return user and plan by type
        async def mock_get(model, pk):
            if model is TelegramUser and pk == sample_user.id:
                return sample_user
            if model is Plan and pk == plan.id:
                return plan
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        svc.get_plan = AsyncMock(return_value=plan)

        confirmed = await svc.confirm_payment(
            order_no="ORD20240101000000ABCD1234",
            provider_tx_id="cs_test_123",
        )

        assert confirmed.status == OrderStatus.FULFILLED.value
        assert confirmed.paid_at is not None

    async def test_confirm_payment_idempotent(self, svc, mock_session):
        from backend.models.payment_models import Order

        order = Order(
            id=1,
            order_no="ORD_ALREADY_PAID",
            user_id=1,
            plan_id=1,
            status=OrderStatus.FULFILLED.value,
            payment_provider="stripe",
            provider_tx_id="cs_already",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = order
        mock_session.execute.return_value = mock_result

        result = await svc.confirm_payment(
            order_no="ORD_ALREADY_PAID",
            provider_tx_id="cs_already",
        )

        assert result.status == OrderStatus.FULFILLED.value

    async def test_confirm_payment_order_not_found(self, svc, mock_session):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="Order not found"):
            await svc.confirm_payment(
                order_no="ORD_NONEXISTENT",
                provider_tx_id="cs_nonexistent",
            )


@pytest.mark.asyncio
class TestCancelExpiredOrder:
    async def test_cancel_expired_order(self, svc, mock_session):
        from backend.models.payment_models import Order

        order = Order(
            id=1,
            order_no="ORD_EXPIRED",
            user_id=1,
            plan_id=1,
            status=OrderStatus.PENDING.value,
            payment_provider="stripe",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = order
        mock_session.execute.return_value = mock_result

        cancelled = await svc.cancel_expired_order("ORD_EXPIRED")

        assert cancelled.status == OrderStatus.CANCELLED.value

    async def test_cancel_non_pending_order_raises(self, svc, mock_session):
        from backend.models.payment_models import Order

        order = Order(
            id=1,
            order_no="ORD_FULFILLED",
            user_id=1,
            plan_id=1,
            status=OrderStatus.FULFILLED.value,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = order
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="Cannot cancel order"):
            await svc.cancel_expired_order("ORD_FULFILLED")


@pytest.mark.asyncio
class TestProcessRefund:
    async def test_process_refund_not_fulfilled_raises(self, svc, mock_session):
        from backend.models.payment_models import Order

        order = Order(
            id=1,
            order_no="ORD_PENDING",
            user_id=1,
            plan_id=1,
            status=OrderStatus.PENDING.value,
        )
        mock_session.get = AsyncMock(return_value=order)

        with pytest.raises(PaymentError, match="only FULFILLED orders"):
            await svc.process_refund(order_id=1)

    async def test_process_refund_order_not_found(self, svc, mock_session):

        mock_session.get = AsyncMock(return_value=None)

        with pytest.raises(PaymentError, match="Order not found"):
            await svc.process_refund(order_id=999)
