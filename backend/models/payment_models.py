"""付费配额系统数据模型"""

import enum
import secrets

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.models.database import Base, utc_now
from backend.models.time_types import UTCDateTime


class PlanType(str, enum.Enum):
    """套餐类型"""

    ONE_TIME = "one_time"
    SUBSCRIPTION = "subscription"


class OrderStatus(str, enum.Enum):
    """订单状态"""

    PENDING = "pending"
    PAID = "paid"
    FULFILLED = "fulfilled"
    REFUNDED = "refunded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RedeemCodeStatus(str, enum.Enum):
    """兑换码状态"""

    ACTIVE = "active"
    DISABLED = "disabled"
    EXHAUSTED = "exhausted"


class SubscriptionStatus(str, enum.Enum):
    """订阅状态"""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentAction(str, enum.Enum):
    """支付流水动作"""

    CREATE = "create"
    PAY = "pay"
    FULFILL = "fulfill"
    REFUND = "refund"
    EXPIRE = "expire"


class RefundRequestStatus(str, enum.Enum):
    """退款申请状态"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class Plan(Base):
    """套餐计划表"""

    __tablename__ = "plan_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    plan_type = Column(String(20), nullable=False)
    price_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="CNY", nullable=False)
    duration_days = Column(Integer, nullable=True)

    # PR 配额增量
    pr_quota_bonus = Column(Integer, default=0, nullable=False)
    pr_daily_add = Column(Integer, default=0, nullable=False)
    pr_weekly_add = Column(Integer, default=0, nullable=False)
    pr_monthly_add = Column(Integer, default=0, nullable=False)

    # Issue 配额增量
    issue_quota_bonus = Column(Integer, default=0, nullable=False)
    issue_daily_add = Column(Integer, default=0, nullable=False)
    issue_weekly_add = Column(Integer, default=0, nullable=False)
    issue_monthly_add = Column(Integer, default=0, nullable=False)

    # Agent 配额增量
    agent_quota_bonus = Column(Integer, default=0, nullable=False)
    agent_daily_add = Column(Integer, default=0, nullable=False)
    agent_weekly_add = Column(Integer, default=0, nullable=False)
    agent_monthly_add = Column(Integer, default=0, nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    description = Column(Text, nullable=True)

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    orders = relationship("Order", back_populates="plan", lazy="selectin")
    redeem_codes = relationship("RedeemCode", back_populates="plan", lazy="selectin")

    def __repr__(self):
        return (
            f"<Plan(name={self.name}, type={self.plan_type}, price={self.price_cents})>"
        )


class Order(Base):
    """订单表"""

    __tablename__ = "payment_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_no = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    plan_id = Column(
        Integer, ForeignKey("plan_plans.id", ondelete="SET NULL"), nullable=True
    )
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="CNY", nullable=False)
    status = Column(String(20), default=OrderStatus.PENDING.value, nullable=False)
    payment_provider = Column(String(50), nullable=True)
    provider_tx_id = Column(String(255), nullable=True)
    paid_at = Column(UTCDateTime, nullable=True)
    fulfilled_at = Column(UTCDateTime, nullable=True)
    expires_at = Column(UTCDateTime, nullable=True)
    metadata_json = Column(Text, nullable=True)

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    plan = relationship("Plan", back_populates="orders", lazy="selectin")

    def __repr__(self):
        return f"<Order(order_no={self.order_no}, status={self.status})>"


class RedeemCode(Base):
    """兑换码表"""

    __tablename__ = "payment_redeem_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    plan_id = Column(
        Integer, ForeignKey("plan_plans.id", ondelete="CASCADE"), nullable=False
    )
    batch_name = Column(String(100), nullable=True)
    max_uses = Column(Integer, default=1, nullable=False)
    used_count = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default=RedeemCodeStatus.ACTIVE.value, nullable=False)
    expires_at = Column(UTCDateTime, nullable=True)
    created_by = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True
    )

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    plan = relationship("Plan", back_populates="redeem_codes", lazy="selectin")

    def __repr__(self):
        return f"<RedeemCode(code={self.code}, status={self.status})>"

    @staticmethod
    def generate_code(length: int = 16) -> str:
        return secrets.token_urlsafe(length).upper()[:length]


class UserSubscription(Base):
    """用户订阅表"""

    __tablename__ = "payment_user_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    plan_id = Column(
        Integer, ForeignKey("plan_plans.id", ondelete="CASCADE"), nullable=False
    )
    status = Column(String(20), default=SubscriptionStatus.ACTIVE.value, nullable=False)
    started_at = Column(UTCDateTime, default=utc_now, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    auto_renew = Column(Boolean, default=False, nullable=False)
    applied_pr_quota_bonus = Column(Integer, default=0, nullable=False)
    applied_pr_daily_add = Column(Integer, default=0, nullable=False)
    applied_pr_weekly_add = Column(Integer, default=0, nullable=False)
    applied_pr_monthly_add = Column(Integer, default=0, nullable=False)
    applied_issue_quota_bonus = Column(Integer, default=0, nullable=False)
    applied_issue_daily_add = Column(Integer, default=0, nullable=False)
    applied_issue_weekly_add = Column(Integer, default=0, nullable=False)
    applied_issue_monthly_add = Column(Integer, default=0, nullable=False)
    applied_agent_quota_bonus = Column(Integer, default=0, nullable=False)
    applied_agent_daily_add = Column(Integer, default=0, nullable=False)
    applied_agent_weekly_add = Column(Integer, default=0, nullable=False)
    applied_agent_monthly_add = Column(Integer, default=0, nullable=False)
    last_order_id = Column(
        Integer, ForeignKey("payment_orders.id", ondelete="SET NULL"), nullable=True
    )

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (UniqueConstraint("user_id", "plan_id", name="uq_user_plan_sub"),)

    def __repr__(self):
        return f"<UserSubscription(user={self.user_id}, plan={self.plan_id}, status={self.status})>"


class PaymentLog(Base):
    """支付流水日志"""

    __tablename__ = "payment_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer, ForeignKey("payment_orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    action = Column(String(50), nullable=False)
    detail = Column(Text, nullable=True)
    operator_id = Column(Integer, nullable=True)

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)

    def __repr__(self):
        return f"<PaymentLog(order={self.order_id}, action={self.action})>"


class RefundRequest(Base):
    """退款申请表（用户申请，超级管理员审核后执行）"""

    __tablename__ = "payment_refund_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer, ForeignKey("payment_orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False
    )
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="CNY", nullable=False)
    status = Column(
        String(20), default=RefundRequestStatus.PENDING.value, nullable=False
    )
    reason = Column(Text, nullable=True)
    review_note = Column(Text, nullable=True)
    requested_at = Column(UTCDateTime, default=utc_now, nullable=False)
    reviewed_by = Column(
        Integer, ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at = Column(UTCDateTime, nullable=True)
    processed_at = Column(UTCDateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    order = relationship("Order", lazy="selectin")
    user = relationship("TelegramUser", foreign_keys=[user_id], lazy="selectin")
    reviewer = relationship("TelegramUser", foreign_keys=[reviewed_by], lazy="selectin")

    __table_args__ = (
        Index("idx_refund_request_order", "order_id"),
        Index("idx_refund_request_user", "user_id"),
        Index("idx_refund_request_status", "status"),
    )

    def __repr__(self):
        return (
            f"<RefundRequest(order={self.order_id}, status={self.status}, "
            f"amount={self.amount_cents})>"
        )
