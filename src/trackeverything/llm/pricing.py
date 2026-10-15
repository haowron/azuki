"""Code-owned token prices and deterministic LLM cost calculation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trackeverything.core.abc.llm import LLMUsage


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD price per one million tokens."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


# Standard API prices verified 2026-08-13 from the providers' official pricing
# pages (z.ai's GLM-5.3 family on 2026-10-14). Keep this table current when
# changing a configured model. The z.ai runtime bills the Coding Plan
# subscription, so its entries only power the equivalent-cost estimate in the
# execution log.
MODEL_PRICES: dict[tuple[str, str], ModelPrice] = {
    ("zai", "glm-4.6"): ModelPrice(
        input=Decimal("0.60"),
        cached_input=Decimal("0.11"),
        output=Decimal("2.20"),
    ),
    ("zai", "glm-5-turbo"): ModelPrice(
        input=Decimal("1.20"),
        cached_input=Decimal("0.24"),
        output=Decimal("4.00"),
    ),
    ("zai", "glm-5.3"): ModelPrice(
        input=Decimal("1.40"),
        cached_input=Decimal("0.26"),
        output=Decimal("4.40"),
    ),
    ("zai", "glm-5.3-flash"): ModelPrice(
        input=Decimal("0.15"),
        cached_input=Decimal("0.03"),
        output=Decimal("0.50"),
    ),
    ("deepseek", "deepseek-v4-flash"): ModelPrice(
        input=Decimal("0.14"),
        cached_input=Decimal("0.0028"),
        output=Decimal("0.28"),
    ),
    ("openai", "gpt-4o"): ModelPrice(
        input=Decimal("2.50"),
        cached_input=Decimal("1.25"),
        output=Decimal("10.00"),
    ),
}


def calculate_cost_usd(provider: str, model: str, usage: LLMUsage) -> str | None:
    """Calculate one call's USD cost, or ``None`` for an unknown price."""
    price = MODEL_PRICES.get((provider, model))
    if price is None:
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached = usage.input_tokens - cached
    cost = (Decimal(uncached) * price.input + Decimal(cached) * price.cached_input + Decimal(usage.output_tokens) * price.output) / Decimal(1_000_000)
    return format(cost, "f")
