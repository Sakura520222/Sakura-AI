"""PaymentService 单元测试

覆盖：套餐管理、兑换码生成/兑换、配额发放、手动充值、边界条件。
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
)
from backend.models.telegram_models import TelegramUser
from backend.services.payment_service import PaymentService, PaymentError


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
    user.issue_daily_quota = 20
    user.issue_weekly_quota = 80
    user.issue_monthly_quota = 300
    return user


@pytest.fixture
def sample_plan():
    plan = Plan(
        id=1,
        name="10次PR包",
        plan_type=PlanType.ONE_TIME.value,
        price_cents=1000,
        pr_quota_bonus=10,
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
    )
    plan.id = 2
    return plan


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

    async def test_update_plan(self, svc, mock_session, sample_plan):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        updated = await svc.update_plan(1, name="新名称")
        assert updated.name == "新名称"


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

    def test_generate_code_format(self):
        code = RedeemCode.generate_code()
        assert len(code) == 16
        assert code.isupper() or code.isalnum()


class TestRedeemCodeUsage:
    async def test_redeem_success(
        self, svc, mock_session, sample_user, sample_plan
    ):
        mock_session.get.return_value = sample_user

        redeem_code = RedeemCode(
            id=1,
            code="TESTCODE123456",
            plan_id=1,
            max_uses=1,
            used_count=0,
            status=RedeemCodeStatus.ACTIVE.value,
        )

        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = redeem_code
            return result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        order = await svc.redeem_code(user_id=1, code="TESTCODE123456")

        assert order.status == OrderStatus.FULFILLED.value
        assert order.payment_provider == "redeem_code"
        assert order.provider_tx_id == "TESTCODE123456"
        assert redeem_code.used_count == 1
        assert redeem_code.status == RedeemCodeStatus.EXHAUSTED.value

    async def test_redeem_invalid_code(self, svc, mock_session, sample_user):
        mock_session.get.return_value = sample_user

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="Invalid"):
            await svc.redeem_code(user_id=1, code="INVALID")

    async def test_redeem_expired_code(
        self, svc, mock_session, sample_user
    ):
        mock_session.get.return_value = sample_user

        expired_code = RedeemCode(
            id=1,
            code="EXPIREDCODE",
            plan_id=1,
            max_uses=1,
            used_count=0,
            status=RedeemCodeStatus.ACTIVE.value,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_code
        mock_session.execute.return_value = mock_result

        with pytest.raises(PaymentError, match="expired"):
            await svc.redeem_code(user_id=1, code="EXPIREDCODE")

    async def test_redeem_exhausted_code(
        self, svc, mock_session, sample_user
    ):
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
        result = await svc._apply_plan_to_user(
            sample_user, sample_subscription_plan
        )
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


class TestManualGrant:
    async def test_grant_success(
        self, svc, mock_session, sample_user, sample_plan
    ):
        mock_session.get.side_effect = lambda model, pk: (
            sample_user if pk == 1 else sample_plan if pk == 1 else None
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_plan
        mock_session.execute.return_value = mock_result

        order = await svc.grant_plan_to_user(
            user_id=1, plan_id=1, operator_id=99
        )
        assert order.status == OrderStatus.FULFILLED.value
        assert order.payment_provider == "manual"
        assert order.amount_cents == 0

    async def test_grant_user_not_found(self, svc, mock_session):
        mock_session.get.return_value = None

        with pytest.raises(PaymentError, match="User not found"):
            await svc.grant_plan_to_user(user_id=999, plan_id=1)


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


class TestOrderNumber:
    def test_order_no_format(self):
        order_no = PaymentService._generate_order_no()
        assert order_no.startswith("ORD")
        assert len(order_no) > 16
