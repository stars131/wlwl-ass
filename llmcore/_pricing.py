"""Approximate per-call USD cost estimation for the cost ledger.

This is intentionally a coarse, prefix-matched table — not an invoice.
The ledger's job is to flag anomalies ("why did this one turn burn $5?")
and give a rough monthly tally, not to settle accounts. Pricing for
non-Anthropic / non-OpenAI models falls back to a sensible default;
exact attribution is the user's job, not ours.

Numbers are per 1,000,000 tokens, USD. Update when providers change
their public rate cards. Cache-read is ~10% of input on Anthropic;
cache-write is ~25% premium over input.
"""
from __future__ import annotations


_FALLBACK = {
    "input_per_1m": 1.00,
    "output_per_1m": 5.00,
    "cache_read_per_1m": 0.10,
    "cache_write_per_1m": 1.25,
}

# Match by lowercased prefix. Longest-match wins.
_PRICING_TABLE: dict[str, dict[str, float]] = {
    # ---- Anthropic Claude 4.x ----
    "claude-opus-4": {
        "input_per_1m": 15.00,
        "output_per_1m": 75.00,
        "cache_read_per_1m": 1.50,
        "cache_write_per_1m": 18.75,
    },
    "claude-sonnet-4": {
        "input_per_1m": 3.00,
        "output_per_1m": 15.00,
        "cache_read_per_1m": 0.30,
        "cache_write_per_1m": 3.75,
    },
    "claude-haiku-4": {
        "input_per_1m": 1.00,
        "output_per_1m": 5.00,
        "cache_read_per_1m": 0.10,
        "cache_write_per_1m": 1.25,
    },
    # ---- Claude 3.x legacy (just in case relays still serve these) ----
    "claude-3-opus": {
        "input_per_1m": 15.00,
        "output_per_1m": 75.00,
        "cache_read_per_1m": 1.50,
        "cache_write_per_1m": 18.75,
    },
    "claude-3-5-sonnet": {
        "input_per_1m": 3.00,
        "output_per_1m": 15.00,
        "cache_read_per_1m": 0.30,
        "cache_write_per_1m": 3.75,
    },
    "claude-3-5-haiku": {
        "input_per_1m": 0.80,
        "output_per_1m": 4.00,
        "cache_read_per_1m": 0.08,
        "cache_write_per_1m": 1.00,
    },
    # ---- OpenAI ----
    "gpt-4o-mini": {
        "input_per_1m": 0.15,
        "output_per_1m": 0.60,
        "cache_read_per_1m": 0.075,
        "cache_write_per_1m": 0.15,
    },
    "gpt-4o": {
        "input_per_1m": 2.50,
        "output_per_1m": 10.00,
        "cache_read_per_1m": 1.25,
        "cache_write_per_1m": 2.50,
    },
    "gpt-4-turbo": {
        "input_per_1m": 10.00,
        "output_per_1m": 30.00,
        "cache_read_per_1m": 5.00,
        "cache_write_per_1m": 10.00,
    },
    "gpt-4": {
        "input_per_1m": 30.00,
        "output_per_1m": 60.00,
        "cache_read_per_1m": 15.00,
        "cache_write_per_1m": 30.00,
    },
    "gpt-3.5": {
        "input_per_1m": 0.50,
        "output_per_1m": 1.50,
        "cache_read_per_1m": 0.25,
        "cache_write_per_1m": 0.50,
    },
    "o1": {
        "input_per_1m": 15.00,
        "output_per_1m": 60.00,
        "cache_read_per_1m": 7.50,
        "cache_write_per_1m": 15.00,
    },
    # ---- Common Chinese providers (rough) ----
    "deepseek": {
        "input_per_1m": 0.14,
        "output_per_1m": 0.28,
        "cache_read_per_1m": 0.014,
        "cache_write_per_1m": 0.14,
    },
    "qwen": {
        "input_per_1m": 0.50,
        "output_per_1m": 1.50,
        "cache_read_per_1m": 0.05,
        "cache_write_per_1m": 0.50,
    },
    "glm": {
        "input_per_1m": 0.50,
        "output_per_1m": 1.50,
        "cache_read_per_1m": 0.05,
        "cache_write_per_1m": 0.50,
    },
    "kimi": {
        "input_per_1m": 0.60,
        "output_per_1m": 2.50,
        "cache_read_per_1m": 0.06,
        "cache_write_per_1m": 0.60,
    },
    "moonshot": {
        "input_per_1m": 0.60,
        "output_per_1m": 2.50,
        "cache_read_per_1m": 0.06,
        "cache_write_per_1m": 0.60,
    },
}


def lookup_pricing(model: str) -> dict[str, float]:
    """Longest-prefix match on the pricing table. Returns the fallback rate
    when nothing matches (e.g. free-pool relay model names we've never seen)."""
    if not model:
        return _FALLBACK
    m = model.lower().strip()
    # Strip Anthropic suffix variants like "claude-opus-4-7[1m]"
    m = m.replace("[1m]", "").strip()
    best_match: str | None = None
    for prefix in _PRICING_TABLE:
        if m.startswith(prefix):
            if best_match is None or len(prefix) > len(best_match):
                best_match = prefix
    if best_match:
        return _PRICING_TABLE[best_match]
    return _FALLBACK


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float:
    """Compute the rough USD cost of a single LLM event. Best-effort —
    returns 0.0 silently on any unexpected shape."""
    try:
        rates = lookup_pricing(model)
        cost = (
            (int(input_tokens or 0) * rates["input_per_1m"])
            + (int(output_tokens or 0) * rates["output_per_1m"])
            + (int(cache_read or 0) * rates["cache_read_per_1m"])
            + (int(cache_creation or 0) * rates["cache_write_per_1m"])
        ) / 1_000_000.0
        return round(cost, 6)
    except Exception:
        return 0.0
