"""Exact, versioned price calculations. No production commercial defaults."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any

from backend.services.payment.currency_units import normalize_currency

UNITS_PER_CREDIT = 1_000_000
MAX_UNITS = 9_000_000_000_000_000_000
DECIMAL_PRECISION = 192
MAX_RATE_DIGITS = 36


class PricingPending(ValueError):
    """Usage cannot yet be turned into an exact charge."""


def exact_decimal(value: Any) -> Decimal:
    if isinstance(value, bool | float) or not isinstance(value, str | int | Decimal):
        raise ValueError("Billing values must be Decimal, integer or decimal strings")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed.copy_abs() > Decimal("1e18"):
        raise ValueError("Billing value must be finite and within supported range")
    return parsed


def credits_to_units(value: Any, *, round_up: bool = False) -> int:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        amount = exact_decimal(value)
        # Quantize the original Decimal directly; multiplying first could lose
        # a tiny positive tail and undercharge at an integer microcredit boundary.
        rounded = amount.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        if not round_up and rounded != amount:
            raise ValueError("Credits support at most six decimal places")
        scaled = rounded * UNITS_PER_CREDIT
        if scaled.copy_abs() > MAX_UNITS:
            raise ValueError("Credit amount exceeds supported range")
        return int(scaled)


def exact_rate(value: Any) -> Decimal:
    amount = exact_decimal(value)
    if amount == 0:
        return Decimal(0)
    parts = amount.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if len(digits) > MAX_RATE_DIGITS or exponent < -MAX_RATE_DIGITS:
        raise ValueError(
            "Pricing precision supports 36 significant digits and 36 decimal places"
        )
    return amount


def units_to_credits(value: int) -> str:
    return format(Decimal(value) / UNITS_PER_CREDIT, "f")


def proportional_units(total: int, part: int, whole: int, *, half_even=False) -> int:
    """Exact cumulative minor-unit allocation, independent of Decimal context."""
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (total, part, whole)
    ):
        raise ValueError("Proportional amounts require integer units")
    if total < 0 or part < 0 or whole <= 0 or part > whole:
        raise ValueError("Invalid proportional amount")
    quotient, remainder = divmod(total * part, whole)
    if half_even and (
        remainder * 2 > whole or (remainder * 2 == whole and quotient % 2)
    ):
        quotient += 1
    return quotient


def validate_price_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Price configuration must be an object")
    allowed = {
        "currency",
        "settlement_currency",
        "fx_rate",
        "markup",
        "credits_per_currency_unit",
        "unit",
        "input_price",
        "output_price",
        "cached_input_price",
        "cache_creation_price",
        "reasoning_price",
        "unit_price",
        "meter",
        "rounding",
        "cache_read_supported",
        "cache_creation_supported",
        "reasoning_supported",
    }
    if set(config) - allowed:
        raise ValueError("Unknown pricing configuration fields")
    for field in (
        "cache_read_supported",
        "cache_creation_supported",
        "reasoning_supported",
    ):
        if field in config and not isinstance(config[field], bool):
            raise ValueError(f"{field} must be an explicit model capability boolean")
    result = dict(config)
    for field in ("currency", "settlement_currency"):
        try:
            result[field] = normalize_currency(config.get(field))
        except ValueError:
            raise ValueError(f"{field} must be a supported currency code") from None
    for field in ("fx_rate", "markup", "credits_per_currency_unit"):
        amount = exact_rate(config.get(field))
        if amount <= 0:
            raise ValueError(f"{field} must be explicitly positive")
        result[field] = str(amount)
    unit = config.get("unit")
    if unit not in {"tokens", "requests", "documents", "search_units"}:
        raise ValueError("Unsupported price unit")
    required = ("input_price", "output_price") if unit == "tokens" else ("unit_price",)
    for field in (
        *required,
        "cached_input_price",
        "cache_creation_price",
        "reasoning_price",
    ):
        if field not in required and field not in config:
            continue
        amount = exact_rate(config.get(field))
        if amount < 0:
            raise ValueError(f"{field} cannot be negative")
        result[field] = str(amount)
    if unit != "tokens":
        result["meter"] = config.get("meter", unit)
        if result["meter"] not in {"requests", "documents", "search_units"}:
            raise ValueError("Unsupported reported billing meter")
    result["rounding"] = "cumulative_ceiling_microcredit"
    return result


@dataclass(frozen=True)
class PriceQuote:
    provider_cost: Decimal
    settlement_amount: Decimal
    credits: Decimal
    snapshot: dict


def calculate_price(usage: Any, config: dict) -> PriceQuote:
    try:
        config = validate_price_config(config)
    except ValueError:
        # Immutable historical profiles can outlive supported configuration.
        # Do not reinterpret their rates or silently turn them into free usage.
        raise PricingPending("Price configuration requires review") from None
    if config["unit"] == "tokens" and not usage.usage_reported:
        raise PricingPending("Provider did not report Usage")
    if getattr(usage, "usage_complete", True) is False:
        raise PricingPending("Provider Usage is incomplete")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        if config["unit"] != "tokens":
            counter = (getattr(usage, "billing_units", None) or {}).get(config["meter"])
            if counter is None:
                raise PricingPending(
                    "Provider did not report the configured billing unit"
                )
            count = exact_decimal(counter)
            if count < 0:
                raise PricingPending("Negative provider usage")
            cost = count * exact_decimal(config["unit_price"])
            dimensions = {config["meter"]: str(count)}
        else:
            cost, dimensions = _token_cost(usage, config)
        settlement = (
            cost * exact_decimal(config["fx_rate"]) * exact_decimal(config["markup"])
        )
        credits = settlement * exact_decimal(config["credits_per_currency_unit"])
        return PriceQuote(
            cost,
            settlement,
            credits,
            {
                **config,
                "dimensions": dimensions,
                "usage_semantics": getattr(usage, "usage_semantics", None) or {},
                "provider_cost": str(cost),
                "settlement_amount": str(settlement),
                "unrounded_credits": str(credits),
            },
        )


def _token_cost(usage: Any, config: dict) -> tuple[Decimal, dict]:
    semantics = getattr(usage, "usage_semantics", None) or {}
    input_count = usage.input_tokens
    output_count = usage.output_tokens
    if input_count is None:
        raise PricingPending("Missing input tokens")
    input_only = usage.call_kind in {"embedding", "rerank"}
    if output_count is None and not input_only:
        raise PricingPending("Missing output tokens")
    dimensions = {"input": input_count, "output": output_count or 0}
    cost = Decimal(0)
    for field, semantic, rate in (
        ("cached_input_tokens", "input_includes_cache_read", "cached_input_price"),
        (
            "cache_creation_tokens",
            "input_includes_cache_creation",
            "cache_creation_price",
        ),
    ):
        count = getattr(usage, field, None)
        capability = (
            "cache_read_supported"
            if rate == "cached_input_price"
            else "cache_creation_supported"
        )
        supported = config.get(capability, semantics.get(capability))
        if count is None:
            # Unknown subsets must never become zero. Equal inclusive tariffs
            # (or an explicit unsupported model capability) can prove that the
            # missing split has no monetary effect without inventing a count.
            independent = rate in config and (
                (
                    semantics.get(semantic) is True
                    and exact_decimal(config[rate])
                    == exact_decimal(config["input_price"])
                )
                or (
                    semantics.get(semantic) is False
                    and exact_decimal(config[rate]) == 0
                )
            )
            if supported is not False and not independent:
                raise PricingPending(f"Missing {field}")
            dimensions[field] = None
            continue
        if count:
            if supported is False:
                raise PricingPending(
                    "Provider cache usage conflicts with configured capability"
                )
            if semantic not in semantics:
                raise PricingPending("Unknown cache protocol semantics")
            if rate not in config:
                raise PricingPending(f"Missing {rate}")
            if semantics[semantic]:
                dimensions["input"] -= count
            cost += Decimal(count) * exact_decimal(config[rate])
        dimensions[field] = count
    # Input-only embedding/rerank interfaces do not generate a separately billed
    # text output. Their reported total/input or non-token meter is authoritative.
    reasoning = None if input_only else getattr(usage, "reasoning_tokens", None)
    reasoning_supported = (
        False
        if input_only
        else config.get("reasoning_supported", semantics.get("reasoning_supported"))
    )
    reasoning_rate = exact_decimal(
        config.get("reasoning_price", config["output_price"])
    )
    if reasoning is None and reasoning_supported is not False:
        independent = (
            semantics.get("output_includes_reasoning") is True
            and reasoning_rate == exact_decimal(config["output_price"])
        ) or (
            semantics.get("output_includes_reasoning") is False and reasoning_rate == 0
        )
        if not independent:
            raise PricingPending("Missing reasoning_tokens")
    if reasoning:
        if reasoning_supported is False:
            raise PricingPending(
                "Provider reasoning usage conflicts with configured capability"
            )
        if "output_includes_reasoning" not in semantics:
            raise PricingPending("Unknown reasoning protocol semantics")
        if "reasoning_price" in config:
            if semantics["output_includes_reasoning"]:
                dimensions["output"] -= reasoning
            cost += Decimal(reasoning) * exact_decimal(config["reasoning_price"])
        elif not semantics["output_includes_reasoning"]:
            dimensions["output"] += reasoning
    dimensions["reasoning_tokens"] = reasoning
    if min(value for value in dimensions.values() if value is not None) < 0:
        raise PricingPending("Usage subsets exceed parent counters")
    cost += Decimal(dimensions["input"]) * exact_decimal(config["input_price"])
    cost += Decimal(dimensions["output"]) * exact_decimal(config["output_price"])
    return cost / Decimal(1_000_000), dimensions
