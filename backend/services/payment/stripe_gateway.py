"""Stripe 支付网关实现

使用 Stripe Checkout Session 模式（Stripe 托管支付页面）。
"""

import asyncio
from typing import Optional

import stripe
from loguru import logger

from backend.services.payment.gateway_base import (
    PaymentGateway,
    PaymentIntentResult,
    PaymentStatusResult,
    RefundResult,
    WebhookEvent,
    WebhookEventType,
)


class StripeGateway(PaymentGateway):
    """Stripe Checkout Session 网关"""

    def __init__(self, api_key: str, webhook_secret: str):
        self._api_key = api_key
        self._webhook_secret = webhook_secret

    async def create_payment(
        self,
        order_no: str,
        amount_cents: int,
        currency: str,
        plan_name: str,
        user_id: int,
        success_url: str,
        cancel_url: str,
        metadata: Optional[dict[str, str]] = None,
    ) -> PaymentIntentResult:
        try:
            session_meta = {"order_no": order_no, "user_id": str(user_id)}
            if metadata:
                session_meta.update(metadata)

            session = await asyncio.to_thread(
                stripe.checkout.Session.create,
                api_key=self._api_key,
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency.lower(),
                            "product_data": {
                                "name": plan_name,
                            },
                            "unit_amount": amount_cents,
                        },
                        "quantity": 1,
                    }
                ],
                metadata=session_meta,
                success_url=success_url,
                cancel_url=cancel_url,
            )

            logger.info(
                "Stripe Checkout Session created: session_id={}, order_no={}",
                session.id,
                order_no,
            )

            return PaymentIntentResult(
                success=True,
                provider_tx_id=session.id,
                checkout_url=session.url or "",
                client_secret="",
                raw_data={
                    "session_id": session.id,
                    "session_url": session.url,
                    "payment_status": session.payment_status,
                },
            )
        except stripe.error.StripeError as e:
            logger.error("Stripe create_payment failed: {}", e)
            return PaymentIntentResult(
                success=False,
                error_message=str(e),
            )
        except Exception as e:
            logger.error("Unexpected error in Stripe create_payment: {}", e)
            return PaymentIntentResult(
                success=False,
                error_message=str(e),
            )

    def verify_webhook(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookEvent:
        signature = headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=signature,
                secret=self._webhook_secret,
            )
        except stripe.error.SignatureVerificationError as e:
            logger.warning("Stripe webhook signature verification failed: {}", e)
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)
        except Exception as e:
            logger.warning("Stripe webhook construction failed: {}", e)
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        # Stripe SDK v15+ returns stripe.Event (StripeObject), not a dict.
        # Use attribute access instead of .get() to avoid KeyError.
        event_type = getattr(event, "type", "") or ""
        event_data_obj = getattr(event, "data", None)
        event_data = getattr(event_data_obj, "object", None) if event_data_obj else None

        type_map = {
            "checkout.session.completed": WebhookEventType.PAYMENT_COMPLETED,
            "checkout.session.async_payment_succeeded": WebhookEventType.PAYMENT_COMPLETED,
            "checkout.session.expired": WebhookEventType.PAYMENT_EXPIRED,
            "checkout.session.async_payment_failed": WebhookEventType.PAYMENT_EXPIRED,
            "charge.refunded": WebhookEventType.PAYMENT_REFUNDED,
            "refund.created": WebhookEventType.PAYMENT_REFUNDED,
            "refund.updated": WebhookEventType.PAYMENT_REFUNDED,
        }
        resolved_type = type_map.get(event_type, WebhookEventType.UNKNOWN)

        if resolved_type == WebhookEventType.UNKNOWN:
            logger.warning("Stripe webhook: unmapped event type '{}'", event_type)
            return WebhookEvent(event_type=WebhookEventType.UNKNOWN)

        provider_tx_id = getattr(event_data, "id", "") or ""
        # metadata is also a StripeObject in v15+, use getattr for attribute access
        raw_metadata = getattr(event_data, "metadata", None)
        if raw_metadata and isinstance(raw_metadata, dict):
            order_no = raw_metadata.get("order_no", "")
        elif raw_metadata:
            # StripeObject: attribute access works via __getattr__ -> __getitem__
            order_no = getattr(raw_metadata, "order_no", "") or ""
        else:
            order_no = ""
        amount_cents = (
            getattr(event_data, "amount_total", 0)
            or getattr(event_data, "amount", 0)
            or 0
        )
        currency = getattr(event_data, "currency", "") or ""

        return WebhookEvent(
            event_type=resolved_type,
            provider_tx_id=provider_tx_id,
            order_no=order_no,
            amount_cents=amount_cents,
            currency=currency,
            raw_event=event,
        )

    async def refund(
        self,
        provider_tx_id: str,
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> RefundResult:
        try:
            # 同步 Stripe SDK 调用需包装以避免阻塞事件循环
            session = await asyncio.to_thread(
                stripe.checkout.Session.retrieve,
                provider_tx_id,
                api_key=self._api_key,
            )
            payment_intent_id = session.payment_intent

            if not payment_intent_id:
                return RefundResult(
                    success=False,
                    error_message="Cannot find payment intent for session",
                )

            refund_params = {
                "api_key": self._api_key,
                "payment_intent": payment_intent_id,
            }
            if amount_cents is not None:
                refund_params["amount"] = amount_cents
            if reason:
                refund_params["reason"] = "requested_by_customer"

            refund_obj = await asyncio.to_thread(stripe.Refund.create, **refund_params)

            logger.info(
                "Stripe refund created: refund_id={}, provider_tx_id={}",
                refund_obj.id,
                provider_tx_id,
            )

            return RefundResult(
                success=True,
                refund_id=refund_obj.id,
                amount_cents=refund_obj.amount or 0,
                status=refund_obj.status or "",
            )
        except stripe.error.StripeError as e:
            logger.error("Stripe refund failed: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )
        except Exception as e:
            logger.error("Unexpected error in Stripe refund: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )

    async def get_payment_status(
        self,
        provider_tx_id: str,
    ) -> PaymentStatusResult:
        try:
            # 同步 Stripe SDK 调用需包装以避免阻塞事件循环
            session = await asyncio.to_thread(
                stripe.checkout.Session.retrieve,
                provider_tx_id,
                api_key=self._api_key,
            )
            return PaymentStatusResult(
                success=True,
                status=session.payment_status or "",
                provider_tx_id=session.id,
                amount_cents=session.amount_total or 0,
                currency=session.currency or "",
                raw_data={
                    "session_id": session.id,
                    "payment_status": session.payment_status,
                    "payment_intent": session.payment_intent,
                },
            )
        except stripe.error.StripeError as e:
            logger.error("Stripe get_payment_status failed: {}", e)
            return PaymentStatusResult(
                success=False,
                error_message=str(e),
            )
        except Exception as e:
            logger.error("Unexpected error in Stripe get_payment_status: {}", e)
            return PaymentStatusResult(
                success=False,
                error_message=str(e),
            )

    async def cancel_payment(
        self,
        provider_tx_id: str,
    ) -> RefundResult:
        """取消 Stripe Checkout Session（expire）"""
        try:
            session = await asyncio.to_thread(
                stripe.checkout.Session.retrieve,
                provider_tx_id,
                api_key=self._api_key,
            )
            if session.status == "complete":
                return RefundResult(
                    success=False,
                    error_message="Session already completed, use refund instead",
                )
            await asyncio.to_thread(
                stripe.checkout.Session.expire,
                provider_tx_id,
                api_key=self._api_key,
            )
            logger.info(
                "Stripe session expired: session_id={}",
                provider_tx_id,
            )
            return RefundResult(
                success=True,
                status="cancelled",
            )
        except stripe.error.StripeError as e:
            logger.error("Stripe cancel failed: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )
        except Exception as e:
            logger.error("Unexpected error in Stripe cancel: {}", e)
            return RefundResult(
                success=False,
                error_message=str(e),
            )
