"""WebUI 付费配额路由"""

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import (
    DateTimeLocalError,
    format_rfc3339,
    get_time_service,
    now_utc,
)
from backend.models.payment_models import Order, RefundRequestStatus
from backend.services.billing_account_pricing_service import (
    configured_pricing_sources,
    get_pricing_account,
    pricing_account_models,
)
from backend.services.billing_price_identity import (
    PRICING_CALL_KINDS,
    canonical_price_call_kind,
)
from backend.services.billing_pricing import (
    DECIMAL_PRECISION,
    exact_rate,
    validate_price_config,
)
from backend.services.billing_service import BillingError, BillingService
from backend.services.billing_view_service import (
    BILLING_FEATURES,
    TRANSACTION_KINDS,
    BillingViewService,
    add_billing_admin_audit,
    parse_pricing_json,
    pending_payment_events,
)
from backend.services.legacy_entitlement_service import LegacyEntitlementService
from backend.services.payment.currency_units import (
    normalize_currency,
    supported_currencies,
)
from backend.services.payment_service import (
    PaymentError,
    PaymentService,
    RedeemCodeStatus,
)
from backend.services.quota_service import QuotaService
from backend.webui.config_feedback import config_issue, config_save_response
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_templates,
    get_user_preferences,
    render_template,
    require_auth,
    require_csrf,
    require_payment_enabled,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language, i18n

router = APIRouter(
    prefix="/billing",
    tags=["WebUI Billing"],
    dependencies=[Depends(require_payment_enabled)],
)
templates = get_templates()


def _format_crypto_currency_display(raw_currency: str) -> str:
    """格式化虚拟币显示名，如 usdttrc20 → USDT (TRC20)"""
    if raw_currency.lower().startswith("usdt"):
        currency_display = "USDT"
        network = raw_currency.upper().replace("USDT", "").strip()
        if network:
            currency_display = f"USDT ({network})"
        return currency_display
    return raw_currency.upper()


def _get_order_expires_at(order: Order) -> str:
    """获取订单过期时间的 ISO 格式字符串（带 UTC 后缀），无则默认 1 小时后"""
    if order.expires_at:
        return format_rfc3339(order.expires_at)
    return format_rfc3339(get_time_service().now_utc() + timedelta(hours=1))


def _parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except TypeError, ValueError:
        return 1


@router.get("/")
async def billing_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """套餐中心首页"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)

    from backend.models.telegram_models import TelegramUser

    db_user = await db.get(TelegramUser, user["user_id"])
    if db_user:
        await QuotaService(db).reset_user_quotas_if_expired(db_user)

    entitlement_service = LegacyEntitlementService(db)
    effective_limits = (
        {
            feature: await entitlement_service.effective_limits(db_user, feature)
            for feature in ("pr_review", "issue_analysis", "agent")
        }
        if db_user
        else {}
    )
    legacy_remaining = await entitlement_service.remaining(user["user_id"])

    page = _parse_page(request.query_params.get("page"))
    per_page = user_prefs.get("items_per_page", 20)
    offset = (page - 1) * per_page
    orders, total = await svc.list_user_orders(
        user["user_id"], limit=per_page, offset=offset
    )
    refund_requests_by_order = await svc.list_refund_requests_for_orders(
        user["user_id"], [order.id for order in orders]
    )

    from backend.services.payment.gateway_factory import get_configured_providers

    available_providers = await get_configured_providers()

    return render_template(
        "billing/index.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing",
        plans=plans,
        db_user=db_user,
        effective_limits=effective_limits,
        legacy_remaining=legacy_remaining,
        orders=orders,
        refund_requests_by_order=refund_requests_by_order,
        total=total,
        page=page,
        per_page=per_page,
        available_providers=available_providers,
        wallet=await BillingViewService(db).wallet(user["user_id"]),
    )


@router.post("/redeem")
async def redeem_code(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    code: str = Form(...),
):
    """兑换码兑换"""
    code = code.strip().upper()
    if not code:
        return toast_redirect(
            "/billing/", "toast.code_required", "error", lang=detect_language()
        )

    svc = PaymentService(db)
    try:
        order = await svc.redeem_code(user["user_id"], code)
        await db.commit()
        logger.info(f"User {user['sub']} redeemed code {code}, order {order.order_no}")
        return toast_redirect(
            "/billing/",
            "toast.redeem_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/purchase/{plan_id}")
async def purchase_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
):
    """Create a payment order and redirect to payment provider checkout"""
    # 支持通过表单选择 provider
    form = await request.form()
    provider = str(form.get("provider", ""))

    # 验证 provider 是否为已配置且已启用的外部支付提供商
    # 延迟导入避免循环引用
    from backend.services.payment import EXTERNAL_PAYMENT_PROVIDERS
    from backend.services.payment.gateway_factory import get_configured_providers

    configured = await get_configured_providers()
    configured_ids = {p["id"] for p in configured}

    if provider not in EXTERNAL_PAYMENT_PROVIDERS or provider not in configured_ids:
        # 选择第一个已配置的 provider 作为默认
        if configured:
            provider = configured[0]["id"]
        else:
            return toast_redirect(
                "/billing/",
                "toast.payment_error",
                "error",
                lang=detect_language(),
                error="No payment provider configured",
            )

    svc = PaymentService(db)
    try:
        order = await svc.create_order(
            user_id=user["user_id"],
            plan_id=plan_id,
            provider=provider,
        )
        await db.commit()

        checkout_url = getattr(order, "_checkout_url", "")
        crypto_info = getattr(order, "_crypto_payment_info", None)

        # 虚拟币支付：渲染加密货币支付页面（含 QR 码）
        if crypto_info and order.payment_provider in ("nowpayments", "tron"):
            lang = detect_language()
            expires_at = _get_order_expires_at(order)
            # 币种显示名
            currency_display = _format_crypto_currency_display(
                crypto_info.get("pay_currency", "usdttrc20")
            )

            return render_template(
                "billing/crypto_payment.html",
                request,
                lang=lang,
                current_user=user,
                order_no=crypto_info.get("order_no", order.order_no),
                order_id=order.id,
                csrf_token=get_csrf_serializer().dumps({}),
                pay_address=crypto_info.get("pay_address", ""),
                pay_amount=crypto_info.get("pay_amount", ""),
                pay_currency_display=currency_display,
                price_amount=crypto_info.get("price_amount", ""),
                price_currency=crypto_info.get("price_currency", "usd"),
                expires_at=expires_at,
                user_prefs=user_prefs,
            )

        if checkout_url:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url=checkout_url, status_code=303)

        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="No checkout URL returned",
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.get("/crypto-payment/{order_no}")
async def reopen_crypto_payment(
    order_no: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """重新打开虚拟币支付页面（用户点击'先返回'后可再次进入）"""
    import json

    from sqlalchemy import select

    from backend.models.payment_models import Order

    stmt = select(Order).where(
        Order.order_no == order_no,
        Order.user_id == user["user_id"],
    )
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()

    if not order or order.status != "pending":
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="Order not found or already processed",
        )

    if order.payment_provider not in ("nowpayments", "tron"):
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="Not a crypto payment order",
        )

    # 从 order metadata 中提取支付信息
    try:
        md = json.loads(order.metadata_json) if order.metadata_json else {}
    except json.JSONDecodeError, TypeError:
        md = {}

    pay_address = md.get("pay_address", "")
    pay_amount = md.get("pay_amount", "")
    pay_currency = md.get("pay_currency", "usdttrc20")
    price_amount = md.get("price_amount", "")
    price_currency = md.get("price_currency", "usd")

    if not pay_address:
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="Payment info not available",
        )

    # 币种显示名
    currency_display = _format_crypto_currency_display(pay_currency)

    lang = detect_language()
    expires_at = _get_order_expires_at(order)

    return render_template(
        "billing/crypto_payment.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        lang=lang,
        order_no=order_no,
        order_id=order.id,
        csrf_token=get_csrf_serializer().dumps({}),
        pay_address=pay_address,
        pay_amount=pay_amount,
        pay_currency_display=currency_display,
        price_amount=price_amount,
        price_currency=price_currency,
        expires_at=expires_at,
    )


@router.get("/crypto-status")
async def crypto_payment_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """轮询虚拟币支付状态（直接查询 NOWPayments API 获取实时状态）"""
    order_no = request.query_params.get("order_no", "")
    if not order_no:
        return JSONResponse({"status": "unknown"}, status_code=400)

    from sqlalchemy import select

    from backend.models.payment_models import Order

    stmt = select(Order).where(
        Order.order_no == order_no,
        Order.user_id == user["user_id"],
    )
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        return JSONResponse({"status": "unknown"}, status_code=404)

    # 如果订单已经完成/失败/取消，直接返回数据库状态
    if order.status in ("completed", "failed", "cancelled", "refunded"):
        db_status_map = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "expired",
            "refunded": "failed",
        }
        return JSONResponse({"status": db_status_map.get(order.status, order.status)})

    # 对于 pending 状态，查询链上状态
    if order.payment_provider == "nowpayments" and order.provider_tx_id:
        try:
            from backend.services.payment import get_gateway

            gateway = await get_gateway("nowpayments")
            api_result = await gateway.get_payment_status(order.provider_tx_id)
            if api_result.success and api_result.raw_data:
                raw_status = api_result.raw_data.get("payment_status", "")
                # NOWPayments 状态 → 前端状态
                nowpay_map = {
                    "waiting": "waiting",
                    "confirming": "confirming",
                    "confirmed": "confirming",
                    "sending": "confirming",
                    "partially_paid": "confirming",
                    "finished": "completed",
                    "expired": "expired",
                    "failed": "failed",
                    "refunded": "failed",
                }
                front_status = nowpay_map.get(raw_status, "waiting")
                return JSONResponse({"status": front_status})
        except Exception:
            pass  # 查询失败时 fallback 到数据库状态

    elif order.payment_provider == "tron" and order.metadata_json:
        try:
            import json as _json

            from backend.services.payment import get_gateway
            from backend.services.payment.tron_gateway import TronGateway

            md = _json.loads(order.metadata_json)
            expected_usdt = Decimal(str(md.get("pay_amount", "0")))
            if expected_usdt.is_finite() and expected_usdt > 0:
                gateway = await get_gateway("tron")
                if isinstance(gateway, TronGateway):
                    api_result = await gateway.check_payment_by_amount(
                        order.order_no,
                        expected_usdt,
                        min_block_timestamp=int(order.created_at.timestamp() * 1000),
                    )
                    if api_result.success and api_result.status == "completed":
                        # 到账确认，触发订单完成
                        try:
                            svc = PaymentService(db)
                            await svc.confirm_payment(
                                order_no=order.order_no,
                                provider_tx_id=api_result.provider_tx_id,
                                paid_amount_cents=int(md["gateway_amount_cents"]),
                                paid_currency=str(md["gateway_currency"]),
                            )
                            await db.commit()
                        except Exception as exc:
                            await db.rollback()
                            logger.warning(
                                "Tron payment fulfillment requires reconciliation: order={} error={}",
                                order.order_no,
                                type(exc).__name__,
                            )
                            return JSONResponse(
                                {
                                    "status": "confirming",
                                    "error": "payment_reconciliation_required",
                                }
                            )
                        return JSONResponse({"status": "completed"})
        except Exception:
            pass  # 查询失败时 fallback 到数据库状态

    # fallback: 返回数据库的 pending → waiting
    return JSONResponse({"status": "waiting"})


@router.get("/payment/result")
async def payment_result(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """Payment result page (payment provider redirects back here)"""
    order_no = request.query_params.get("order_no", "")
    status = request.query_params.get("status", "failed")

    # If user cancelled, mark the order as cancelled (idempotent — no-op if already gone)
    if status == "cancel" and order_no:
        try:
            svc = PaymentService(db)
            await svc.cancel_and_commit_if_needed(order_no)
        except Exception as e:
            logger.warning("Failed to cancel order {}: {}", order_no, e)
            await db.rollback()

    return render_template(
        "billing/payment_result.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="billing",
        order_no=order_no,
        status=status,
    )


@router.post("/orders/{order_id}/refund")
async def user_refund_order(
    order_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    reason: str = Form(""),
):
    """User submits a refund request for super-admin review."""
    svc = PaymentService(db)
    try:
        refund_request = await svc.submit_refund_request(
            order_id=order_id,
            user_id=user["user_id"],
            reason=reason,
        )
        await db.commit()
        return toast_redirect(
            "/billing/",
            "toast.refund_request_submitted",
            lang=detect_language(),
            order_no=refund_request.order.order_no
            if refund_request.order
            else order_id,
        )
    except PaymentError as e:
        await db.rollback()
        toast_key = (
            "toast.refund_request_exists"
            if e.code == "DUPLICATE_REFUND_REQUEST"
            else "toast.payment_error"
        )
        return toast_redirect(
            "/billing/",
            toast_key,
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/orders/{order_id}/cancel")
async def user_cancel_order(
    order_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """用户主动取消 pending 订单"""
    from sqlalchemy import select

    from backend.models.payment_models import Order

    stmt = select(Order).where(
        Order.id == order_id,
        Order.user_id == user["user_id"],
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order or order.status != "pending":
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="Order not found or not cancellable",
        )

    svc = PaymentService(db)
    try:
        await svc.cancel_order(order.order_no, user["user_id"])
        await db.commit()
        return toast_redirect(
            "/billing/",
            "toast.payment_cancelled",
            "success",
            lang=detect_language(),
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/orders/{order_id}/delete")
async def user_delete_order(
    order_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """Hide a non-active order from its user's list, preserving financial evidence."""
    from sqlalchemy import and_, select

    from backend.models.payment_models import Order, OrderStatus

    deletable_statuses = [
        OrderStatus.PENDING.value,
        OrderStatus.CANCELLED.value,
        OrderStatus.EXPIRED.value,
        OrderStatus.REFUNDED.value,
    ]

    stmt = (
        select(Order)
        .where(
            and_(
                Order.id == order_id,
                Order.user_id == user["user_id"],
                Order.status.in_(deletable_statuses),
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order:
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error="Order not found or cannot be deleted",
        )

    # The invoice fingerprint must survive cancellation, expiry and refund.
    # Late signed events and immutable ledger links still resolve this row.
    if order.hidden_by_user_at is None:
        order.hidden_by_user_at = now_utc()
    await db.commit()

    return toast_redirect(
        "/billing/",
        "billing.order_deleted",
        lang=detect_language(),
    )


# ========== 管理员：套餐管理 ==========


@router.get("/admin/refund-requests")
async def admin_refund_requests(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """超级管理员退款审核列表"""
    status = request.query_params.get("status") or None
    page = _parse_page(request.query_params.get("page"))
    per_page = user_prefs.get("items_per_page", 20)
    offset = (page - 1) * per_page

    svc = PaymentService(db)
    refund_requests, total = await svc.list_refund_requests(
        status=status,
        limit=per_page,
        offset=offset,
    )

    return render_template(
        "billing/admin_refund_requests.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing_admin_refunds",
        refund_requests=refund_requests,
        status=status or "",
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/admin/refund-requests/{request_id}/approve")
async def admin_approve_refund_request(
    request_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    review_note: str = Form(""),
):
    """批准退款申请并执行真实退款。"""
    svc = PaymentService(db)
    try:
        refund_request = await svc.approve_refund_request(
            request_id=request_id,
            reviewer_id=user["user_id"],
            review_note=review_note,
        )
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="approve_refund_request",
            target_type="refund_request",
            target_id=str(request_id),
            detail={
                "order_no": refund_request.order.order_no
                if refund_request.order
                else None,
                "amount_cents": refund_request.amount_cents,
                "status": refund_request.status,
                "review_note": review_note,
                "error_message": refund_request.error_message,
            },
        )
        toast_key = (
            "toast.refund_failed"
            if refund_request.status == RefundRequestStatus.FAILED.value
            else "toast.refund_approved"
        )
        toast_type = (
            "error"
            if refund_request.status == RefundRequestStatus.FAILED.value
            else "success"
        )
        return toast_redirect(
            "/billing/admin/refund-requests",
            toast_key,
            toast_type,
            lang=detect_language(),
            error=refund_request.error_message or "",
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/refund-requests",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/refund-requests/{request_id}/reject")
async def admin_reject_refund_request(
    request_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    review_note: str = Form(""),
):
    """驳回退款申请。"""
    svc = PaymentService(db)
    try:
        refund_request = await svc.reject_refund_request(
            request_id=request_id,
            reviewer_id=user["user_id"],
            review_note=review_note,
        )
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="reject_refund_request",
            target_type="refund_request",
            target_id=str(request_id),
            detail={
                "order_no": refund_request.order.order_no
                if refund_request.order
                else None,
                "amount_cents": refund_request.amount_cents,
                "status": refund_request.status,
                "review_note": review_note,
            },
        )
        return toast_redirect(
            "/billing/admin/refund-requests",
            "toast.refund_rejected",
            lang=detect_language(),
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/refund-requests",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.get("/admin/plans")
async def admin_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """管理员套餐管理页面"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=False)

    return render_template(
        "billing/admin_plans.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing_admin_plans",
        plans=plans,
        currency_codes=supported_currencies(),
    )


def _plan_configuration_error(error, lang):
    """Keep config errors localized without exposing raw persistence exceptions."""
    message = str(error)
    if getattr(error, "code", None) == "invalid_currency":
        return i18n.t(
            "billing.form.invalid_currency",
            lang=lang,
            field_key=i18n.t("billing.plan_currency", lang=lang),
        )
    if message.startswith("Plan not found:"):
        return i18n.t("toast.plan_not_found", lang=lang)
    if message.startswith("Cannot hard delete plan with"):
        return i18n.t("billing.form.plan_delete_blocked", lang=lang)
    if message.startswith("credit_grant"):
        label = "billing.credit_grant"
    elif message.startswith("concurrency_limit"):
        label = "billing.concurrency_limit"
    elif message == "Invalid rate limit configuration" or isinstance(error, ValueError):
        label = "billing.rate_limits_config"
    else:
        return i18n.t("billing.invalid_config", lang=lang)
    return i18n.t("toast.value_invalid", lang=lang, field_key=i18n.t(label, lang=lang))


@router.post("/admin/plans")
async def admin_create_plan(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    name: str = Form(...),
    plan_type: str = Form(...),
    price_cents: int = Form(0),
    currency: str = Form("CNY", min_length=3, max_length=10),
    duration_days: int = Form(None),
    credit_grant: Decimal = Form(Decimal(0), ge=0),
    rate_limits: str = Form("{}"),
    concurrency_limit: int | None = Form(None, ge=1),
    pr_quota_bonus: int = Form(0),
    pr_daily_add: int = Form(0),
    pr_weekly_add: int = Form(0),
    pr_monthly_add: int = Form(0),
    issue_quota_bonus: int = Form(0),
    issue_daily_add: int = Form(0),
    issue_weekly_add: int = Form(0),
    issue_monthly_add: int = Form(0),
    agent_quota_bonus: int = Form(0),
    agent_daily_add: int = Form(0),
    agent_weekly_add: int = Form(0),
    agent_monthly_add: int = Form(0),
    description: str = Form(None),
    sort_order: int = Form(0),
    user_prefs: dict = Depends(get_user_preferences),
):
    """创建套餐"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    svc = PaymentService(db)
    try:
        plan = await svc.create_plan(
            name=name,
            plan_type=plan_type,
            price_cents=price_cents,
            currency=currency.upper(),
            duration_days=duration_days if duration_days else None,
            credit_grant=credit_grant,
            rate_limits=parse_pricing_json(rate_limits),
            concurrency_limit=concurrency_limit,
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
            description=description if description else None,
            sort_order=sort_order,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_create_plan",
            target_id=str(plan.id),
            detail={
                "credit_grant": str(plan.credit_grant),
                "rate_limits": plan.rate_limits,
                "concurrency_limit": plan.concurrency_limit,
            },
        )
        await db.commit()
        return toast_redirect("/billing/admin/plans", "toast.plan_created", lang=lang)
    except (PaymentError, ValueError) as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=lang,
            error=_plan_configuration_error(e, lang),
        )
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create plan: {e}")
        return toast_redirect(
            "/billing/admin/plans",
            "toast.save_failed",
            "error",
            lang=lang,
        )


@router.post("/admin/plans/{plan_id}/toggle")
async def admin_toggle_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """启用/禁用套餐"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    svc = PaymentService(db)
    plan = await svc.get_plan(plan_id)
    if not plan:
        return toast_redirect(
            "/billing/admin/plans",
            "toast.plan_not_found",
            "error",
            lang=lang,
        )
    plan.is_active = not plan.is_active
    await db.commit()
    status = i18n.t(
        "common.enabled" if plan.is_active else "common.disabled", lang=lang
    )
    return toast_redirect(
        "/billing/admin/plans",
        "toast.plan_toggled",
        lang=lang,
        status=status,
    )


# ========== 管理员：兑换码管理 ==========


@router.get("/admin/codes")
async def admin_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """管理员兑换码管理页面"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)
    page = _parse_page(request.query_params.get("page"))
    per_page = user_prefs.get("items_per_page", 20)
    offset = (page - 1) * per_page
    codes, total = await svc.list_redeem_codes(limit=per_page, offset=offset)

    return render_template(
        "billing/admin_codes.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing_admin_codes",
        plans=plans,
        codes=codes,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/admin/codes/generate")
async def admin_generate_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    plan_id: int = Form(...),
    count: int = Form(...),
    batch_name: str = Form(None),
    max_uses: int = Form(1),
):
    """批量生成兑换码"""
    if count < 1 or count > 100:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.code_count_range",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        codes = await svc.generate_redeem_codes(
            plan_id=plan_id,
            count=count,
            batch_name=batch_name if batch_name else None,
            max_uses=max_uses,
            created_by=user["user_id"],
        )
        await db.commit()
        logger.info(f"Admin {user['sub']} generated {count} codes for plan {plan_id}")
        return toast_redirect(
            "/billing/admin/codes",
            "toast.code_generated",
            lang=detect_language(),
            count=len(codes),
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：手动充值 ==========


@router.post("/admin/grant")
async def admin_grant(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_id: int = Form(..., ge=1),
    plan_id: int = Form(..., ge=1),
    idempotency_key: str = Form(..., min_length=1, max_length=191),
):
    """手动为用户充值套餐"""
    svc = PaymentService(db)
    try:
        order = await svc.grant_plan_to_user(
            user_id=user_id,
            plan_id=plan_id,
            operator_id=user["user_id"],
            idempotency_key=idempotency_key,
        )
        await db.commit()
        logger.info(f"Admin {user['sub']} granted plan {plan_id} to user {user_id}")
        return toast_redirect(
            f"/users/{user_id}",
            "toast.grant_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            f"/users/{user_id}",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：套餐编辑/删除 ==========


@router.post("/admin/plans/{plan_id}/edit")
async def admin_edit_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    name: str = Form(None),
    plan_type: str = Form(None),
    price_cents: int = Form(None),
    currency: str | None = Form(None, min_length=3, max_length=10),
    duration_days: int = Form(None),
    credit_grant: Decimal | None = Form(None, ge=0),
    rate_limits: str | None = Form(None),
    concurrency_limit: int | None = Form(None, ge=1),
    pr_quota_bonus: int = Form(None),
    pr_daily_add: int = Form(None),
    pr_weekly_add: int = Form(None),
    pr_monthly_add: int = Form(None),
    issue_quota_bonus: int = Form(None),
    issue_daily_add: int = Form(None),
    issue_weekly_add: int = Form(None),
    issue_monthly_add: int = Form(None),
    agent_quota_bonus: int = Form(None),
    agent_daily_add: int = Form(None),
    agent_weekly_add: int = Form(None),
    agent_monthly_add: int = Form(None),
    description: str = Form(None),
    sort_order: int = Form(None),
    user_prefs: dict = Depends(get_user_preferences),
):
    """编辑套餐"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    try:
        parsed_limits = (
            parse_pricing_json(rate_limits) if rate_limits is not None else None
        )
    except ValueError:
        return toast_redirect(
            "/billing/admin/plans",
            "billing.invalid_config",
            "error",
            lang=lang,
        )
    update_data = {}
    form_fields = {
        "name": name,
        "plan_type": plan_type,
        "price_cents": price_cents,
        "currency": currency.upper() if currency else None,
        "duration_days": duration_days,
        "credit_grant": credit_grant,
        "rate_limits": parsed_limits,
        "concurrency_limit": concurrency_limit,
        "pr_quota_bonus": pr_quota_bonus,
        "pr_daily_add": pr_daily_add,
        "pr_weekly_add": pr_weekly_add,
        "pr_monthly_add": pr_monthly_add,
        "issue_quota_bonus": issue_quota_bonus,
        "issue_daily_add": issue_daily_add,
        "issue_weekly_add": issue_weekly_add,
        "issue_monthly_add": issue_monthly_add,
        "agent_quota_bonus": agent_quota_bonus,
        "agent_daily_add": agent_daily_add,
        "agent_weekly_add": agent_weekly_add,
        "agent_monthly_add": agent_monthly_add,
        "description": description,
        "sort_order": sort_order,
    }
    for field, value in form_fields.items():
        if value is not None:
            update_data[field] = value

    svc = PaymentService(db)
    try:
        plan = await svc.update_plan(plan_id, **update_data)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_update_plan",
            target_id=str(plan.id),
            detail={
                "credit_grant": str(plan.credit_grant),
                "rate_limits": plan.rate_limits,
                "concurrency_limit": plan.concurrency_limit,
                "updated_fields": list(update_data.keys()),
            },
        )
        await db.commit()
        return toast_redirect("/billing/admin/plans", "toast.plan_updated", lang=lang)
    except (PaymentError, ValueError) as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=lang,
            error=_plan_configuration_error(e, lang),
        )


@router.post("/admin/plans/{plan_id}/delete")
async def admin_delete_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    hard_delete: str = Form(None),
    user_prefs: dict = Depends(get_user_preferences),
):
    """删除套餐（默认软删除，勾选 hard_delete 时硬删除）"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    is_hard = hard_delete == "on"
    svc = PaymentService(db)
    try:
        plan = await svc.delete_plan(plan_id, hard_delete=is_hard)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="delete_plan",
            target_type="plan",
            target_id=str(plan_id),
            detail={"name": plan.name, "hard_delete": is_hard},
        )
        toast_key = "toast.plan_hard_deleted" if is_hard else "toast.plan_deleted"
        return toast_redirect("/billing/admin/plans", toast_key, lang=lang)
    except (PaymentError, ValueError) as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=lang,
            error=_plan_configuration_error(e, lang),
        )


# ========== 管理员：兑换码编辑/删除 ==========


@router.post("/admin/codes/{code_id}/edit")
async def admin_edit_code(
    code_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    status: str = Form(None),
    expires_at: str = Form(None),
    expires_at_fold: str = Form(None),
    max_uses: int = Form(None),
    plan_id: int = Form(None),
):
    """编辑兑换码"""
    update_data = {}
    if status is not None:
        update_data["status"] = status
    if expires_at is not None and expires_at.strip():
        try:
            fold = None if expires_at_fold in (None, "") else int(expires_at_fold)
            update_data["expires_at"] = get_time_service().parse_datetime_local(
                expires_at.strip(), fold=fold
            )
        except DateTimeLocalError, TypeError, ValueError:
            return toast_redirect(
                "/billing/admin/codes",
                "toast.invalid_param",
                "error",
                lang=detect_language(),
            )
    if max_uses is not None:
        update_data["max_uses"] = max_uses
    if plan_id is not None:
        update_data["plan_id"] = plan_id

    if not update_data:
        return toast_redirect(
            "/billing/admin/codes", "toast.no_changes", lang=detect_language()
        )

    svc = PaymentService(db)
    try:
        code = await svc.update_redeem_code(code_id, **update_data)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="edit_redeem_code",
            target_type="redeem_code",
            target_id=str(code_id),
            detail={"code": code.code, "updated_fields": list(update_data.keys())},
        )
        return toast_redirect(
            "/billing/admin/codes", "toast.code_updated", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/{code_id}/delete")
async def admin_delete_code(
    code_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """删除兑换码"""
    svc = PaymentService(db)
    try:
        code = await svc.delete_redeem_code(code_id)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="delete_redeem_code",
            target_type="redeem_code",
            target_id=str(code_id),
            detail={"code": code.code},
        )
        return toast_redirect(
            "/billing/admin/codes", "toast.code_deleted", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：订单退款 ==========


@router.post("/admin/orders/{order_id}/refund")
async def admin_refund_order(
    order_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """管理员发起退款"""
    svc = PaymentService(db)
    try:
        order = await svc.process_refund(
            order_id=order_id,
            operator_id=user["user_id"],
        )
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="refund_order",
            target_type="order",
            target_id=str(order_id),
            detail={
                "order_no": order.order_no,
                "status": order.status,
            },
        )
        return toast_redirect(
            "/billing/",
            "toast.refund_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/plans/batch-toggle")
async def admin_batch_toggle_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """批量切换套餐启用/禁用状态"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    form = await request.form()
    raw = form.get("plan_ids", "")
    plan_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not plan_ids:
        return toast_redirect(
            "/billing/admin/plans",
            "toast.batch_no_selection",
            "error",
            lang=lang,
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_toggle_plans(plan_ids)
        await db.commit()
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_toggle_plans",
            target_type="plan",
            detail={
                "plan_ids": plan_ids,
                "success_count": success_count,
                "skipped_count": skipped_count,
            },
        )
        toast_key = (
            "toast.batch_partial_success"
            if skipped_count > 0
            else "toast.batch_toggle_success"
        )
        return toast_redirect(
            "/billing/admin/plans",
            toast_key,
            lang=lang,
            count=success_count,
            skipped=skipped_count,
        )
    except (PaymentError, ValueError) as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=lang,
            error=_plan_configuration_error(e, lang),
        )


@router.post("/admin/plans/batch-delete")
async def admin_batch_delete_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    hard_delete: str = Form(None),
    user_prefs: dict = Depends(get_user_preferences),
):
    """批量删除套餐"""
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    form = await request.form()
    raw = form.get("plan_ids", "")
    plan_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not plan_ids:
        return toast_redirect(
            "/billing/admin/plans",
            "toast.batch_no_selection",
            "error",
            lang=lang,
        )

    is_hard = hard_delete == "on"
    svc = PaymentService(db)
    try:
        result = await svc.batch_delete_plans(plan_ids, hard_delete=is_hard)
        success_count = len(result["success"])
        failed_count = len(result["failed"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_delete_plans",
            target_type="plan",
            detail={
                "plan_ids": plan_ids,
                "hard_delete": is_hard,
                "success_count": success_count,
                "failed_count": failed_count,
            },
        )
        toast_key = (
            "toast.batch_partial_success"
            if failed_count > 0
            else ("toast.batch_hard_deleted" if is_hard else "toast.batch_deleted")
        )
        return toast_redirect(
            "/billing/admin/plans",
            toast_key,
            lang=lang,
            count=success_count,
            skipped=failed_count,
        )
    except (PaymentError, ValueError) as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=lang,
            error=_plan_configuration_error(e, lang),
        )


# ========== 管理员：兑换码批量操作 ==========


@router.post("/admin/codes/batch-disable")
async def admin_batch_disable_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量禁用兑换码"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_update_redeem_codes(
            code_ids, status=RedeemCodeStatus.DISABLED.value
        )
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_disable_codes",
            target_type="redeem_code",
            detail={
                "code_ids": code_ids,
                "success_count": success_count,
                "skipped_count": skipped_count,
            },
        )
        toast_key = (
            "toast.batch_partial_success"
            if skipped_count > 0
            else "toast.batch_codes_disabled"
        )
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/batch-enable")
async def admin_batch_enable_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量启用兑换码"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_update_redeem_codes(
            code_ids, status=RedeemCodeStatus.ACTIVE.value
        )
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_enable_codes",
            target_type="redeem_code",
            detail={
                "code_ids": code_ids,
                "success_count": success_count,
                "skipped_count": skipped_count,
            },
        )
        toast_key = (
            "toast.batch_partial_success"
            if skipped_count > 0
            else "toast.batch_codes_enabled"
        )
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/batch-delete")
async def admin_batch_delete_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量删除兑换码（仅删除未使用的）"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_delete_redeem_codes(code_ids)
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_delete_codes",
            target_type="redeem_code",
            detail={
                "total_count": len(code_ids),
                "success_count": success_count,
                "skipped_count": skipped_count,
            },
        )
        toast_key = (
            "toast.batch_codes_partial_deleted"
            if skipped_count > 0
            else "toast.batch_codes_deleted"
        )
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.get("/credits")
async def credits_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    feature: str | None = Query(
        None, pattern="^(|" + "|".join(BILLING_FEATURES) + ")$"
    ),
    kind: str | None = Query(None, pattern="^(|" + "|".join(TRANSACTION_KINDS) + ")$"),
    page: int = Query(1, ge=1),
):
    view = BillingViewService(db)
    per_page = min(100, max(1, int(user_prefs.get("items_per_page", 20))))
    transactions = await view.transactions(
        user,
        offset=(page - 1) * per_page,
        limit=per_page,
        feature=feature,
        kind=kind,
    )
    operations = await view.operations(user, limit=per_page)
    return render_template(
        "billing/credits.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="billing_credits",
        csrf_token=get_csrf_serializer().dumps({}),
        wallet=await view.wallet(user["user_id"]),
        transactions=transactions,
        operations=operations,
        notices=await view.notices(
            user["user_id"], offset=(page - 1) * per_page, limit=per_page
        ),
        features=BILLING_FEATURES,
        kinds=TRANSACTION_KINDS,
        feature=feature or "",
        kind=kind or "",
        page=page,
    )


@router.post("/credits/threshold")
async def update_credit_threshold(
    request: Request,
    credits: Decimal = Form(..., ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    try:
        await BillingService(db).set_low_balance_threshold(user["user_id"], credits)
        await db.commit()
        return toast_redirect(
            "/billing/credits", "billing.threshold_saved", lang=detect_language()
        )
    except BillingError, ValueError:
        await db.rollback()
        return toast_redirect(
            "/billing/credits",
            "billing.invalid_config",
            "error",
            lang=detect_language(),
        )


_PRICING_CALL_KINDS = PRICING_CALL_KINDS
_PRICING_UNITS = ("tokens", "requests", "documents", "search_units")
_PRICING_FIELDS = (
    "currency",
    "settlement_currency",
    "fx_rate",
    "markup",
    "credits_per_currency_unit",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "unit_price",
    "cache_read_supported",
    "cache_creation_supported",
    "reasoning_supported",
    "meter",
)


def _pricing_issue(field, message_key, lang):
    return config_issue(
        field,
        "invalid_price_field",
        "billing.form." + message_key,
        lang=lang,
        field_label_key="billing.form." + field,
        anchor="pricing-editor",
    )


def _pricing_fields_config(form, lang):
    """Normalize explicit form prices to canonical per-million-token strings."""
    errors = []
    config = {}
    for field in ("currency", "settlement_currency"):
        try:
            config[field] = normalize_currency(form.get(field, ""))
        except ValueError:
            errors.append(_pricing_issue(field, "invalid_currency", lang))
    for field in ("fx_rate", "markup", "credits_per_currency_unit"):
        try:
            amount = exact_rate(str(form.get(field, "")).strip())
            if amount <= 0:
                raise ValueError
            config[field] = str(amount)
        except ValueError, TypeError, InvalidOperation:
            errors.append(_pricing_issue(field, "positive_decimal", lang))
    unit = str(form.get("unit", "")).strip()
    if unit not in _PRICING_UNITS:
        errors.append(_pricing_issue("unit", "invalid_choice", lang))
        return None, errors
    config["unit"] = unit
    scale = str(form.get("token_scale", "1000000")).strip()
    if unit == "tokens" and scale not in {"1", "1000", "1000000"}:
        errors.append(_pricing_issue("token_scale", "invalid_choice", lang))
        scale = "1000000"
    rates = (
        (
            "input_price",
            "output_price",
            "cached_input_price",
            "cache_creation_price",
            "reasoning_price",
        )
        if unit == "tokens"
        else ("unit_price",)
    )
    for field in rates:
        value = str(form.get(field, "")).strip()
        if not value and field in {
            "cached_input_price",
            "cache_creation_price",
            "reasoning_price",
        }:
            continue
        try:
            amount = exact_rate(value)
            if amount < 0:
                raise ValueError
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                canonical = (
                    amount * Decimal(1_000_000) / Decimal(scale)
                    if unit == "tokens"
                    else amount
                )
                normalized = exact_rate(canonical)
                config[field] = str(
                    normalized.normalize() if unit == "tokens" else normalized
                )
        except ValueError, TypeError, InvalidOperation:
            errors.append(_pricing_issue(field, "nonnegative_decimal", lang))
    if unit == "tokens":
        for field in (
            "cache_read_supported",
            "cache_creation_supported",
            "reasoning_supported",
        ):
            value = str(form.get(field, "")).strip()
            if value:
                if value not in {"true", "false"}:
                    errors.append(_pricing_issue(field, "invalid_choice", lang))
                else:
                    config[field] = value == "true"
    else:
        meter = str(form.get("meter", unit)).strip() or unit
        if meter not in {"requests", "documents", "search_units"}:
            errors.append(_pricing_issue("meter", "invalid_choice", lang))
        else:
            config["meter"] = meter
    return config, errors


def _pricing_editor_values(profile):
    config = profile.config if profile else {}
    values = {
        "provider_id": profile.provider_id if profile else "",
        "account_id": getattr(profile, "account_id", None) or "",
        "source_scope": (
            profile.call_kind
            if profile
            and not getattr(profile, "account_id", None)
            and profile.call_kind in {"embedding", "rerank"}
            else "account"
        ),
        "model_id": profile.model_id if profile else "",
        "call_kind": canonical_price_call_kind(profile.call_kind)
        if profile
        else "chat",
        "unit": config.get("unit", "tokens"),
        "token_scale": "1000000",
    }
    for field in _PRICING_FIELDS:
        value = config.get(field, "")
        if field in {"currency", "settlement_currency"} and value:
            try:
                value = normalize_currency(value)
            except ValueError:
                pass  # Keep unknown historical codes visible for explicit review.
        values[field] = (
            "true" if value is True else "false" if value is False else str(value)
        )
    return values


@router.get("/admin/pricing")
async def admin_pricing_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    page: int = Query(1, ge=1),
    edit_price: int | None = Query(None, ge=1),
):
    from backend.models.billing_models import BillingPriceProfile

    prices = await BillingViewService(db).prices(offset=(page - 1) * 50, limit=50)
    edit_price = edit_price if isinstance(edit_price, int) else None
    profile = await db.get(BillingPriceProfile, edit_price) if edit_price else None
    if edit_price and profile is None:
        return toast_redirect(
            "/billing/admin/pricing",
            "billing.form.version_not_found",
            "error",
            lang=detect_language(user_prefs),
        )
    accounts, auxiliary = await configured_pricing_sources()
    return render_template(
        "billing/admin_pricing.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="billing_admin_pricing",
        csrf_token=get_csrf_serializer().dumps({}),
        prices=prices,
        price_form=_pricing_editor_values(profile),
        editing_profile={"id": profile.id, "version": profile.version}
        if profile
        else None,
        price_json=json.dumps(profile.config, ensure_ascii=False, indent=2)
        if profile
        else "",
        pricing_accounts=accounts,
        pricing_auxiliary=auxiliary,
        pricing_call_kinds=_PRICING_CALL_KINDS,
        pricing_units=_PRICING_UNITS,
        currency_codes=supported_currencies(),
        page=page,
        payment_events=await pending_payment_events(
            db, offset=(page - 1) * 20, limit=20
        ),
        grant_plans=await PaymentService(db).list_plans(active_only=True),
        grant_idempotency_key="admin-grant:" + uuid4().hex,
    )


@router.get("/admin/pricing/accounts/{account_id}/models")
async def admin_pricing_account_models(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    refresh: bool = Query(False),
):
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    account = await get_pricing_account(account_id)
    if account is None or not account.enabled:
        return JSONResponse(
            {
                "success": False,
                "code": "account_unavailable",
                "error": _account_pricing_message("account_unavailable", lang),
            },
            status_code=404,
        )
    try:
        result = await pricing_account_models(account, refresh=refresh is True)
    except Exception:
        result = {
            "account_id": account.id,
            "models": list(account.models),
            "default_model": account.default_model,
            "source": "saved",
            "discovery_failed": True,
        }
    return JSONResponse({"success": True, "data": result})


def _account_pricing_message(code, lang):
    from backend.webui.i18n import i18n

    return i18n.t("billing.account_validation." + code, lang=lang)


@router.post("/admin/pricing")
async def admin_publish_pricing(
    request: Request,
    provider_id: str = Form(""),
    model_id: str = Form(""),
    call_kind: str = Form(""),
    config: str = Form(""),
    account_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    lang = (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )
    try:
        form = await request.form()
        scope = form.get("source_scope")
        identity = {
            "provider_id": provider_id.strip(),
            "model_id": model_id.strip(),
            "call_kind": canonical_price_call_kind(call_kind.strip()),
        }
        errors = []
        selected_account = account_id.strip() if isinstance(account_id, str) else ""
        if scope == "account" or selected_account:
            account = await get_pricing_account(selected_account)
            if account is None or not account.enabled:
                errors.append(
                    config_issue(
                        "account_id",
                        "account_unavailable",
                        "billing.account_validation.account_unavailable",
                        lang=lang,
                        help_url="/config/ai",
                        help_label_key="config.ai_title",
                    )
                )
            else:
                identity["provider_id"] = account.provider_id
        elif scope in {"embedding", "rerank"}:
            _, auxiliary = await configured_pricing_sources()
            configured = next(
                (item for item in auxiliary if item["feature"] == scope), None
            )
            if configured is None:
                errors.append(
                    config_issue(
                        "source_scope",
                        "auxiliary_unavailable",
                        "billing.account_validation.auxiliary_unavailable",
                        lang=lang,
                    )
                )
            else:
                identity.update(
                    provider_id=configured["provider_id"],
                    model_id=configured["model_id"],
                    call_kind=scope,
                )
        elif scope is not None:
            errors.append(
                config_issue(
                    "source_scope",
                    "invalid_pricing_source",
                    "billing.account_validation.invalid_source",
                    lang=lang,
                )
            )
        errors.extend(
            [
                _pricing_issue(field, "identity_required", lang)
                for field, maximum in (
                    ("provider_id", 128),
                    ("model_id", 255),
                    ("call_kind", 32),
                )
                if not identity[field] or len(identity[field]) > maximum
            ]
        )
        mode = str(form.get("config_mode", "json"))
        if mode == "fields":
            price_config, field_errors = _pricing_fields_config(form, lang)
            errors.extend(field_errors)
            if identity["call_kind"] not in _PRICING_CALL_KINDS:
                errors.append(_pricing_issue("call_kind", "invalid_choice", lang))
        elif mode == "json":
            try:
                price_config = validate_price_config(parse_pricing_json(config))
            except ValueError, TypeError, InvalidOperation:
                errors.append(_pricing_issue("config", "invalid_json_prices", lang))
                price_config = None
        else:
            errors.append(_pricing_issue("config_mode", "invalid_choice", lang))
            price_config = None
        if errors:
            return config_save_response(
                request,
                "/billing/admin/pricing",
                "toast.config_validation_failed",
                lang=lang,
                errors=errors,
            )
        profile = await BillingService(db).publish_price(
            identity["provider_id"],
            identity["model_id"],
            identity["call_kind"],
            price_config,
            actor_id=user["user_id"],
            account_id=selected_account or None,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_publish_price",
            target_id=str(profile.id),
            detail={
                **identity,
                "call_kind": profile.call_kind,
                "account_id": selected_account or None,
                "version": profile.version,
            },
        )
        await db.commit()
        return config_save_response(
            request, "/billing/admin/pricing", "billing.price_published", lang=lang
        )
    except BillingError, ValueError, TypeError, InvalidOperation:
        await db.rollback()
        return config_save_response(
            request,
            "/billing/admin/pricing",
            "toast.config_validation_failed",
            lang=lang,
            errors=[_pricing_issue("config", "invalid_json_prices", lang)],
        )


@router.post("/admin/credits/adjust")
async def admin_adjust_credits(
    request: Request,
    target_user_id: int = Form(..., ge=1),
    credits: Decimal = Form(...),
    idempotency_key: str = Form(..., min_length=1, max_length=191),
    reason: str = Form(..., min_length=1, max_length=1000),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    from backend.models.telegram_models import TelegramUser

    if await db.get(TelegramUser, target_user_id) is None:
        return toast_redirect(
            "/billing/admin/pricing",
            "billing.invalid_config",
            "error",
            lang=detect_language(),
        )
    try:
        transaction = await BillingService(db).adjust(
            target_user_id,
            credits,
            idempotency_key=idempotency_key,
            actor_id=user["user_id"],
            reason=reason,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_adjust_wallet",
            target_id=str(target_user_id),
            detail={
                "transaction_id": transaction.id,
                "credits": str(credits),
                "reason": reason,
            },
        )
        await db.commit()
        return toast_redirect(
            "/billing/admin/pricing", "billing.adjustment_saved", lang=detect_language()
        )
    except BillingError, ValueError:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/pricing",
            "billing.invalid_config",
            "error",
            lang=detect_language(),
        )


@router.post("/credits/notices/{notice_id}/read")
async def mark_credit_notice_read(
    notice_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
):
    if not await BillingViewService(db).read_notice(user["user_id"], notice_id):
        return JSONResponse({"error": "notice_not_found"}, status_code=404)
    await db.commit()
    return toast_redirect(
        "/billing/credits", "billing.notice_read", lang=detect_language(user_prefs)
    )


@router.post("/admin/payment-events/{event_id}/replay")
async def admin_replay_payment_event(
    event_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
):
    from backend.services.payment_event_service import PaymentEventService

    try:
        record = await PaymentEventService(db).replay(event_id)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_replay_payment",
            target_id=str(event_id),
            detail={"status": record.status},
        )
        await db.commit()
        key = (
            "billing.payment_event_processed"
            if record.status == "processed"
            else "billing.payment_event_pending"
        )
        return toast_redirect(
            "/billing/admin/pricing", key, lang=detect_language(user_prefs)
        )
    except PaymentError, BillingError, ValueError:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/pricing",
            "billing.invalid_payment_evidence",
            "error",
            lang=detect_language(user_prefs),
        )


@router.post("/admin/payment-events/{event_id}/resolve")
async def admin_resolve_payment_event(
    event_id: int,
    request: Request,
    evidence: str = Form(..., min_length=1, max_length=2000),
    order_id: int | None = Form(None, ge=1),
    checkout_amount_cents: int | None = Form(None, ge=1),
    checkout_currency: str | None = Form(None, min_length=3, max_length=10),
    refund_reference_id: str | None = Form(None, min_length=1, max_length=191),
    refund_amount_cents: int | None = Form(None, ge=1),
    refund_currency: str | None = Form(None, min_length=3, max_length=10),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    csrf_token: str = Depends(require_csrf),
):
    from backend.services.payment_event_service import PaymentEventService

    try:
        kwargs = {
            "order_id": order_id,
            "checkout_amount_cents": checkout_amount_cents,
            "checkout_currency": checkout_currency,
            "refund_reference_id": refund_reference_id,
            "refund_amount_cents": refund_amount_cents,
            "refund_currency": refund_currency,
        }
        for field in ("checkout_currency", "refund_currency"):
            if kwargs[field] is not None:
                kwargs[field] = normalize_currency(kwargs[field])
        record = await PaymentEventService(db).resolve(
            event_id,
            operator_id=user["user_id"],
            evidence=evidence,
            **{key: value for key, value in kwargs.items() if value is not None},
        )
        await db.commit()
        key = (
            "billing.payment_event_processed"
            if record.status == "processed"
            else "billing.payment_event_pending"
        )
        return toast_redirect(
            "/billing/admin/pricing", key, lang=detect_language(user_prefs)
        )
    except PaymentError, BillingError, ValueError:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/pricing",
            "billing.invalid_payment_evidence",
            "error",
            lang=detect_language(user_prefs),
        )
