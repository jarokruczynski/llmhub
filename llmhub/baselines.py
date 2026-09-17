"""What the traffic would have cost on a paid vendor, and where that number comes from.

The hub routes free models, so "saved" only means something against a named comparison. These
are published list prices, per million tokens, in USD, read off each vendor's own pricing page
on `AS_OF`. They are a reference point, not a quote: prices change, promotional rates expire,
and nothing here is fetched at runtime.

Keeping them in one place is the point. They used to sit inside the dashboard script with no
date and no source, which is how a number goes quietly stale while still looking precise. When
they are refreshed, change `AS_OF` in the same commit and say which page was read.

Cached input is billed separately by all three vendors and is much cheaper than fresh input, so
it is priced on its own rather than at the input rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AS_OF = "2026-09-17"
CURRENCY = "USD"
UNIT = "per 1M tokens"

OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
ANTHROPIC_SOURCE = "https://claude.com/pricing"
GOOGLE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"


@dataclass(frozen=True)
class Baseline:
    id: str
    label: str
    vendor: str
    input_per_m: float
    cached_per_m: float
    output_per_m: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "vendor": self.vendor,
            "input": self.input_per_m,
            "cached": self.cached_per_m,
            "output": self.output_per_m,
            "source": self.source,
        }


BASELINES: tuple[Baseline, ...] = (
    Baseline(
        id="gpt-5.6-terra",
        label="OpenAI GPT-5.6 Terra",
        vendor="OpenAI",
        input_per_m=2.00,
        cached_per_m=0.20,
        output_per_m=12.00,
        source=OPENAI_SOURCE,
    ),
    Baseline(
        id="claude-sonnet-5",
        label="Claude Sonnet 5",
        vendor="Anthropic",
        input_per_m=2.00,
        cached_per_m=0.20,
        output_per_m=10.00,
        source=ANTHROPIC_SOURCE,
    ),
    Baseline(
        id="gemini-3.8-flash",
        label="Gemini 3.8 Flash",
        vendor="Google",
        input_per_m=0.75,
        cached_per_m=0.075,
        output_per_m=3.75,
        source=GOOGLE_SOURCE,
    ),
    Baseline(
        id="gpt-5.6-luna",
        label="OpenAI GPT-5.6 Luna",
        vendor="OpenAI",
        input_per_m=0.20,
        cached_per_m=0.02,
        output_per_m=1.20,
        source=OPENAI_SOURCE,
    ),
    Baseline(
        id="gpt-6-astra",
        label="OpenAI GPT-6 Astra",
        vendor="OpenAI",
        input_per_m=10.00,
        cached_per_m=1.00,
        output_per_m=50.00,
        source=OPENAI_SOURCE,
    ),
)

DEFAULT_BASELINE = "gpt-5.6-terra"


@dataclass(frozen=True)
class TierMatched:
    """Small models priced against a small model, the rest against a mid-tier one.

    A single baseline flatters or punishes the whole table: pricing a 4B model as a flagship
    invents savings, pricing a flagship as a 4B hides them. The markers are the names vendors
    give their own small tiers.
    """

    id: str = "tier-matched"
    label: str = "Tier matched"
    small: str = "gpt-5.6-luna"
    large: str = DEFAULT_BASELINE
    markers: tuple[str, ...] = field(
        default=("flash", "mini", "lite", "nano", "small", "haiku", "8b", "7b", "4b", "3b")
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "small": self.small,
            "large": self.large,
            "markers": list(self.markers),
        }


TIER_MATCHED = TierMatched()


def catalog() -> dict[str, Any]:
    """The whole table as the API serves it, provenance included."""
    return {
        "as_of": AS_OF,
        "currency": CURRENCY,
        "unit": UNIT,
        "default": DEFAULT_BASELINE,
        "baselines": [item.as_dict() for item in BASELINES],
        "tier_matched": TIER_MATCHED.as_dict(),
    }
