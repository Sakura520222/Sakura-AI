"""Paddle protocol currency bounds, separate from pricing/settlement assets."""

from paddle_billing.Entities.Shared import CurrencyCode

from backend.services.payment.currency_units import (
    normalize_currency,
    supported_currencies,
)

_DECLARED_CODES = frozenset(
    str(value)
    for value in vars(CurrencyCode).values()
    if isinstance(value, CurrencyCode)
)


def supported_paddle_currencies() -> tuple[str, ...]:
    # The documented GET /currencies enum matches these explicit SDK members:
    # https://developer.paddle.com/api-reference/currencies/list-currencies/
    # CurrencyCode accepts arbitrary strings, so constructing it is not validation.
    return tuple(code for code in supported_currencies() if code in _DECLARED_CODES)


def validate_paddle_currency(value: object) -> str:
    currency = normalize_currency(value)
    if currency not in supported_paddle_currencies():
        raise ValueError("Paddle does not support the selected payment currency")
    return currency
