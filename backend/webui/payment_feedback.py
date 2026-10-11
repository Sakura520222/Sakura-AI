"""Translate a known refund refusal, including its persisted review message."""

from backend.services.payment_service import REFUND_AMOUNT_UNREPRESENTABLE_MESSAGE
from backend.webui.i18n import i18n


def refund_error_message(error, *, lang):
    message = str(error)
    if (
        getattr(error, "code", None) == "refund_amount_unrepresentable"
        or message == REFUND_AMOUNT_UNREPRESENTABLE_MESSAGE
    ):
        return i18n.t("billing.refund_amount_unrepresentable", lang=lang)
    return message
