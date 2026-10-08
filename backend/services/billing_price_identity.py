"""Pricing categories are separate from the actual provider request kind."""

PRICING_CALL_KINDS = ("chat", "context_compression", "embedding", "rerank")


def canonical_price_call_kind(call_kind: str) -> str:
    """Stream transport uses the same model tariff; raw Usage keeps its kind."""
    return "chat" if call_kind == "chat_stream" else call_kind
