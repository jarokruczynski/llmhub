from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .collect import Page
from .llm import BudgetExhausted, HubLLM, LLMError, Usage

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 12000

EXTRACT_SYSTEM = (
    "You read one web page and report free LLM/VLM API access it offers: free tiers, $0 "
    "models, trial credits, time-limited promos. You answer with JSON only.\n"
    "Rules: no rumours - every offer must quote the page; a page that only advertises paid "
    "plans, or talks about free chat UIs rather than an API, has no offers; do not invent "
    "limits, urls or dates; copy numbers exactly as written."
)

EXTRACT_SCHEMA = """{"offers": [{
  "provider": "vendor name",
  "url": "page or docs url the offer is on",
  "base_url": "OpenAI-compatible API endpoint if the page names one, else null",
  "what_is_free": "one line: which models or credits are free",
  "limits": "rate/token/credit limits as written, empty string if the page gives none",
  "expires_at": "YYYY-MM-DD if the offer ends on a date, else null",
  "vision": true/false,
  "tools": true/false,
  "friction": "what it costs to get in: card, phone, waitlist, region - empty if none",
  "evidence_quote": "<= 200 chars quoted verbatim from the page"
}]}"""

EXTRACT_USER = """Page url: {url}

Return JSON matching this schema exactly:
{schema}

No offers on the page -> {{"offers": []}}. No prose outside the JSON object.

--- page text ---
{text}"""


class Offer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    url: str = ""
    base_url: str | None = None
    what_is_free: str = ""
    limits: str = ""
    expires_at: str | None = None
    vision: bool = False
    tools: bool = False
    friction: str = ""
    evidence_quote: str = ""

    @field_validator("provider", "evidence_quote")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("base_url", "expires_at", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("vision", "tools", mode="before")
    @classmethod
    def _loose_bool(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)


@dataclass
class ExtractResult:
    offers: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    errors: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    pages: int = 0
    stopped: str | None = None


def parse_offers(payload: dict[str, Any], page_url: str) -> tuple[list[dict[str, Any]], list[str]]:
    offers: list[dict[str, Any]] = []
    dropped: list[str] = []
    raw = payload.get("offers")
    if not isinstance(raw, list):
        return offers, [f"{page_url}: no offers list in the answer"]
    for item in raw:
        if not isinstance(item, dict):
            dropped.append(f"{page_url}: offer is not an object")
            continue
        try:
            offer = Offer.model_validate(item)
        except ValidationError as exc:
            dropped.append(f"{page_url}: {exc.error_count()} invalid field(s) in an offer")
            continue
        row = offer.model_dump()
        row["url"] = row["url"] or page_url
        row["source_url"] = page_url
        offers.append(row)
    return offers, dropped


async def extract_offers(
    llm: HubLLM,
    pages: list[Page],
    *,
    model: str = "fast",
    max_page_chars: int = MAX_PAGE_CHARS,
) -> ExtractResult:
    """One call per changed page on the cheap alias. A page with no offers costs one call."""
    result = ExtractResult(pages=len(pages))
    for page in pages:
        if not page.text.strip():
            continue
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": EXTRACT_USER.format(
                    url=page.url, schema=EXTRACT_SCHEMA, text=page.text[:max_page_chars]
                ),
            },
        ]
        try:
            reply = await llm.json_call(messages, model=model)
        except BudgetExhausted as exc:
            result.stopped = str(exc)
            result.errors.append(f"extract stopped: {exc}")
            break
        except LLMError as exc:
            result.errors.append(f"extract {page.url}: {exc}")
            continue
        result.usage.add(reply)
        offers, dropped = parse_offers(reply.data or {}, page.url)
        result.offers.extend(offers)
        result.dropped.extend(dropped)
    return result
