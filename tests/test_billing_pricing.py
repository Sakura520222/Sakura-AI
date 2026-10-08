"""Exact billing prices; fixtures are examples, never production defaults."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.services.billing_pricing import (
    PricingPending,
    calculate_price,
    credits_to_units,
    proportional_units,
    validate_price_config,
)


def price(**overrides):
    return dict(
        currency="USD",
        settlement_currency="CNY",
        fx_rate="7",
        markup="1.2",
        credits_per_currency_unit="100",
        unit="tokens",
        input_price="2",
        output_price="10",
        cached_input_price="0.2",
        cache_creation_price="2.5",
        **overrides,
    )


def usage(**overrides):
    values = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cached_input_tokens": 800,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 50,
        "usage_reported": True,
        "protocol_family": "openai_compatible",
        "call_kind": "chat",
        "billing_units": {},
        "usage_semantics": {
            "input_includes_cache_read": True,
            "input_includes_cache_creation": True,
            "output_includes_reasoning": True,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_openai_cached_and_reasoning_are_subsets():
    quote = calculate_price(usage(), price())
    assert quote.provider_cost == Decimal("0.00156")
    assert quote.credits == Decimal("1.3104")
    assert quote.snapshot["rounding"] == "cumulative_ceiling_microcredit"


def test_anthropic_input_excludes_caches():
    quote = calculate_price(
        usage(
            input_tokens=200,
            usage_semantics={
                "input_includes_cache_read": False,
                "input_includes_cache_creation": False,
                "output_includes_reasoning": True,
            },
        ),
        price(),
    )
    assert quote.provider_cost == Decimal("0.00156")


def test_gemini_reasoning_is_exclusive_output():
    quote = calculate_price(
        usage(
            cached_input_tokens=0,
            usage_semantics={
                "input_includes_cache_read": True,
                "input_includes_cache_creation": True,
                "output_includes_reasoning": False,
            },
        ),
        price(),
    )
    assert quote.provider_cost == Decimal("0.0035")


def test_missing_counter_and_explicit_zero_differ():
    with pytest.raises(PricingPending):
        calculate_price(usage(output_tokens=None), price())
    assert (
        calculate_price(
            usage(output_tokens=0, reasoning_tokens=0), price()
        ).provider_cost
        > 0
    )
    with pytest.raises(PricingPending):
        calculate_price(usage(usage_reported=False), price())
    with pytest.raises(PricingPending, match="reasoning"):
        calculate_price(
            usage(reasoning_tokens=None), {**price(), "reasoning_price": "20"}
        )
    assert (
        calculate_price(
            usage(reasoning_tokens=0), {**price(), "reasoning_price": "20"}
        ).provider_cost
        > 0
    )


def test_no_float_or_implicit_conversion_defaults():
    with pytest.raises(ValueError):
        validate_price_config({**price(), "fx_rate": 7.0})
    with pytest.raises(ValueError):
        credits_to_units(0.1)
    with pytest.raises(ValueError):
        validate_price_config({"unit": "tokens"})
    with pytest.raises(ValueError):
        validate_price_config({**price(), "markup": "NaN"})
    with pytest.raises(ValueError):
        validate_price_config(
            {**price(), "api_key": "fixture-secret-must-not-enter-ledger"}
        )


def test_embedding_input_and_rerank_reported_units():
    quote = calculate_price(
        usage(
            call_kind="embedding",
            output_tokens=None,
            cached_input_tokens=0,
            reasoning_tokens=None,
        ),
        price(),
    )
    assert quote.provider_cost == Decimal("0.002")
    config = {
        **price(),
        "unit": "requests",
        "meter": "search_units",
        "unit_price": "0.002",
    }
    quote = calculate_price(
        usage(call_kind="rerank", billing_units={"search_units": 2}), config
    )
    assert quote.provider_cost == Decimal("0.004")
    with pytest.raises(PricingPending):
        calculate_price(usage(call_kind="rerank", billing_units={}), config)


def test_cumulative_rounding_does_not_round_every_call_up():
    tiny = Decimal("0.00000001")
    assert credits_to_units(sum([tiny] * 100), round_up=True) == 1
    assert sum(credits_to_units(tiny, round_up=True) for _ in range(100)) == 100


def test_credit_rounding_cannot_drop_digits_before_the_ceiling_boundary():
    amount = Decimal("1." + "0" * 100 + "1")
    assert credits_to_units(amount, round_up=True) == 1_000_001
    with pytest.raises(ValueError, match="six decimal"):
        credits_to_units(amount)


def test_pricing_rejects_unsupported_scale_before_decimal_underflow():
    with pytest.raises(ValueError, match="precision"):
        validate_price_config({**price(), "fx_rate": "1e-10000000"})


def test_partial_refund_ratio_is_exact_at_large_integer_boundaries():
    total = 8_999_999_999_999_999_999
    whole = 2_000_000_003
    assert proportional_units(total, whole - 1, whole) == (total * (whole - 1)) // whole
    assert proportional_units(5, 1, 2, half_even=True) == 2
    assert proportional_units(7, 1, 2, half_even=True) == 4


def test_unknown_cache_without_tariff_is_not_explicit_zero():
    config = {**price()}
    config.pop("cached_input_price")
    with pytest.raises(PricingPending, match="cached_input"):
        calculate_price(usage(cached_input_tokens=None), config)
    assert calculate_price(usage(cached_input_tokens=0), config).provider_cost > 0


def test_missing_inclusive_cache_split_can_be_priced_only_if_fee_independent():
    quote = calculate_price(
        usage(cached_input_tokens=None), {**price(), "cached_input_price": "2"}
    )
    assert quote.provider_cost == Decimal("0.003")
    assert quote.snapshot["dimensions"]["cached_input_tokens"] is None
    quote = calculate_price(
        usage(cached_input_tokens=None), {**price(), "cache_read_supported": False}
    )
    assert quote.snapshot["dimensions"]["cached_input_tokens"] is None
    with pytest.raises(PricingPending, match="conflicts"):
        calculate_price(
            usage(cached_input_tokens=1), {**price(), "cache_read_supported": False}
        )


def test_missing_exclusive_gemini_reasoning_cannot_be_priced_as_zero():
    sample = usage(
        cached_input_tokens=0,
        reasoning_tokens=None,
        usage_semantics={
            "input_includes_cache_read": True,
            "input_includes_cache_creation": True,
            "output_includes_reasoning": False,
        },
    )
    with pytest.raises(PricingPending, match="reasoning"):
        calculate_price(sample, price())
    quote = calculate_price(sample, {**price(), "reasoning_supported": False})
    assert quote.snapshot["dimensions"]["reasoning_tokens"] is None


@pytest.mark.asyncio
async def test_checkout_fx_keeps_tiny_tail_at_half_even_minor_unit_boundary(
    monkeypatch,
):
    from backend.services import payment_service

    async def rate(key):
        return "1.000000500000000000000000000001"

    monkeypatch.setattr(payment_service, "get_dynamic_config", rate)
    assert (
        await payment_service.PaymentService(None)._convert_currency(
            1_000_000, "USD", "CNY"
        )
        == 1_000_001
    )
