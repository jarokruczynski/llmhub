from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .collect import Page
from .llm import BudgetExhausted, HubLLM, LLMError, Usage

log = logging.getLogger(__name__)

MAX_EXTRA_PAGES = 6
MAX_EXTRA_CHARS = 6000
MAX_REJECTIONS = 30

# The curator does not always answer with a bare, on-schema JSON object: it sometimes wraps
# the array in prose, fences it, or names the action something schema-adjacent ("add" instead
# of "new"). None of that is a reason to throw the whole answer away.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

ACTION_SYNONYMS = {
    "add": "new",
    "new": "new",
    "create": "new",
    "insert": "new",
    "update": "update",
    "patch": "update",
    "modify": "update",
    "edit": "update",
    "skip": "skip",
    "ignore": "skip",
    "duplicate": "skip",
    "dup": "skip",
    # a skip the owner has already ruled on: same outcome, counted apart in the run report
    "skip_rejected": "skip",
    "skip-rejected": "skip",
    "rejected": "skip",
}
REJECTED_ACTIONS = ("skip_rejected", "skip-rejected", "rejected")

CURATE_SYSTEM = (
    "You keep a watchlist of free LLM/VLM API access. You get offers extracted from web "
    "pages and the rows already on the list, and you decide what the list should look "
    "like. You answer with JSON only.\n"
    "Rules: dedupe by url and by provider - one row per provider offer; prefer the vendor's "
    "own docs url over a blog, forum or deal-site link; an offer whose limits carry no "
    "evidence quote is a skip; no rumours, only what the evidence states; base_url must be "
    "an OpenAI-compatible API endpoint (e.g. https://api.vendor.com/v1), never a marketing "
    "page - leave it null if the evidence does not name one; a row that only repeats an "
    "existing promo with nothing new is a skip; never touch a row whose status is used.\n"
    "Rejections: the owner throws out leads that look like promos but are useless for a "
    "free-only gateway - a card needed for a trial, credits that expire in days, IDE-only "
    "access with no API, a business email, a region the owner cannot use, an aggregator of "
    "models the hub already routes - and each rejected reason is a rule learned over time, "
    "not one-off noise. An offer from a rejected vendor, or one that fails for the same "
    "reason as a listed rejection, is `skip_rejected` with `reason` naming the rejection it "
    "matches; never propose it as new or as an update."
)

CURATE_SCHEMA = """{"decisions": [{
  "action": "new" | "update" | "skip" | "skip_rejected",
  "promo_id": <id of the existing row for update/skip, else null>,
  "row": {
    "provider": "vendor name, lowercase, matching a catalog template id when one fits",
    "url": "the best url for a human to read the offer",
    "base_url": "OpenAI-compatible endpoint or null",
    "api_key_env": "env var name the key would live in, or null",
    "expires_at": "YYYY-MM-DD or null",
    "note": "one line: what is free, the limits, the friction"
  },
  "reason": "why this action, one line"
}],
"needs_pages": ["url to fetch and re-read, only when a docs url would settle base_url"]}"""

CURATE_USER = """Catalog template ids (use one as `provider` when the offer matches):
{templates}

Promos already on the list:
{promos}
{rejections}
Offers extracted this run:
{offers}
{extra}
Return JSON matching this schema exactly:
{schema}

Every offer must end up in exactly one decision. No prose outside the JSON object."""

EXTRA_PAGES_BLOCK = """
Pages you asked for:
{pages}
"""

REJECTIONS_BLOCK = """
Rejected by the owner (do not propose these or similar offers; reasons are the owner's rules):
{rejections}
"""


class DecisionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    url: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    expires_at: str | None = None
    note: str | None = None

    @field_validator("provider")
    @classmethod
    def _provider_set(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider must not be empty")
        return value.strip()

    @field_validator("url", "base_url", "api_key_env", "expires_at", "note", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("base_url")
    @classmethod
    def _endpoint_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) endpoint")
        return value.rstrip("/")


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["new", "update", "skip"]
    promo_id: int | None = None
    row: DecisionRow | None = None
    reason: str = ""
    # set when the curator answered `skip_rejected`: the skip stands on a rejection the owner
    # already made, which the run report counts apart from an ordinary dedupe skip
    rejected_match: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Decision:
        if self.action in ("new", "update") and self.row is None:
            raise ValueError(f"action {self.action} needs a row")
        if self.action == "update" and self.promo_id is None:
            raise ValueError("action update needs promo_id")
        return self


@dataclass
class CurateResult:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    errors: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    rounds: int = 0
    needs_pages: list[str] = field(default_factory=list)
    stopped: str | None = None


def promo_view(promos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only what the curator needs to dedupe against: no notes dump, no key material."""
    return [
        {
            "id": row.get("id"),
            "provider": row.get("provider"),
            "url": row.get("url"),
            "status": row.get("status"),
            "base_url": row.get("base_url"),
        }
        for row in promos
    ]


def rejection_lines(rejections: list[dict[str, Any]]) -> list[str]:
    """One line per rejection, most recent first. The provider and the reason are the whole
    rule: a url or a date would only cost tokens."""
    lines: list[str] = []
    for row in rejections[:MAX_REJECTIONS]:
        reason = str(row.get("reason") or "").strip().replace("\n", " ")
        provider = str(row.get("provider") or "unknown").strip()
        if reason:
            lines.append(f"- {provider}: {reason}")
    return lines


def _looks_like_a_decision(value: dict[str, Any]) -> bool:
    return "action" in value


def coerce_payload(text: str, fallback: dict[str, Any] | None) -> dict[str, Any]:
    """Recover `{"decisions": [...]}` from whatever shape the model actually answered with.

    `llm.json_call` already tried to find one JSON object in the text; that naive scan grabs
    the first balanced `{...}` and nothing else, so a top-level array (fenced or not) comes
    back as just its first element - a decision-shaped dict missing the wrapper. Re-parse the
    raw text here instead: prefer a fenced code block over the whole message (models pad JSON
    with commentary), accept a bare list as the decisions list, and accept a single decision
    object on its own. Only fall back to what `json_call` already parsed when none of that
    yields anything usable.
    """
    candidates: list[str] = []
    match = _FENCE_RE.search(text)
    if match:
        candidates.append(match.group(1).strip())
    candidates.append(text.strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return {"decisions": parsed}
        if isinstance(parsed, dict):
            if "decisions" in parsed:
                return parsed
            if _looks_like_a_decision(parsed):
                return {"decisions": [parsed]}
            return parsed
    return fallback or {}


def _coerce_expires_at(value: Any) -> tuple[Any, bool]:
    """`YYYY-MM-DD`, kept as-is; anything else the curator sends (a timestamp, a stray time
    component) is dropped rather than failing the whole decision - a wrong date is worse than
    a missing one, but a missing one still loses the deadline. Returns (value, dropped)."""
    if not isinstance(value, str) or not value.strip():
        return value, False
    text = value.strip()
    date_part = text.split(" ")[0].split("T")[0]
    try:
        date.fromisoformat(date_part)
    except ValueError:
        return None, True
    return date_part, False


def _normalize_decision(item: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Tolerate the shapes a free model actually produces: case/synonyms on `action`, a
    missing `row` on a skip, and an `expires_at` that carries a time component or is
    unparseable outright. Returns (normalized, a note about anything silently fixed)."""
    normalized = dict(item)
    note: str | None = None
    action = str(normalized.get("action", "")).strip().lower()
    normalized["action"] = ACTION_SYNONYMS.get(action, action)
    if action in REJECTED_ACTIONS:
        normalized["rejected_match"] = True
    row = normalized.get("row")
    if normalized["action"] == "skip" and not row:
        normalized.pop("row", None)
    elif isinstance(row, dict):
        row = dict(row)
        if "expires_at" in row:
            original = row.get("expires_at")
            coerced, dropped = _coerce_expires_at(original)
            row["expires_at"] = coerced
            if dropped:
                note = f"expires_at {original!r} unparseable, dropped"
        normalized["row"] = row
    return normalized, note


def parse_decisions(
    payload: dict[str, Any], raw_text: str = ""
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    decisions: list[dict[str, Any]] = []
    dropped: list[str] = []
    raw = payload.get("decisions")
    if not isinstance(raw, list):
        snippet = raw_text.strip()[:300]
        detail = f": {snippet}" if snippet else f": {sorted(payload)[:5]}"
        return decisions, [f"no decisions list in the answer{detail}"], []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            dropped.append(f"decision {index}: not an object")
            continue
        normalized, note = _normalize_decision(item)
        try:
            decision = Decision.model_validate(normalized)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(part) for part in first.get("loc", ()))
            snippet = json.dumps(item, ensure_ascii=False)[:300]
            dropped.append(f"decision {index}: {where or 'shape'}: {first.get('msg', 'invalid')} ({snippet})")
            continue
        if note:
            log.info("curate decision %s: %s", index, note)
        decisions.append(decision.model_dump())
    needs = [str(url) for url in payload.get("needs_pages") or [] if isinstance(url, str)]
    return decisions, dropped, needs[:MAX_EXTRA_PAGES]


def _user_message(
    offers: list[dict[str, Any]],
    promos: list[dict[str, Any]],
    template_ids: list[str],
    extra_pages: list[Page],
    rejections: list[dict[str, Any]] | None = None,
) -> str:
    extra = ""
    if extra_pages:
        rendered = "\n\n".join(
            f"[{page.url}]\n{page.text[:MAX_EXTRA_CHARS]}" for page in extra_pages if page.text
        )
        extra = EXTRA_PAGES_BLOCK.format(pages=rendered)
    lines = rejection_lines(rejections or [])
    rejected = REJECTIONS_BLOCK.format(rejections="\n".join(lines)) if lines else ""
    return CURATE_USER.format(
        templates=", ".join(template_ids),
        promos=json.dumps(promo_view(promos), ensure_ascii=False, indent=None),
        offers=json.dumps(offers, ensure_ascii=False, indent=None),
        rejections=rejected,
        extra=extra,
        schema=CURATE_SCHEMA,
    )


async def curate(
    llm: HubLLM,
    *,
    offers: list[dict[str, Any]],
    promos: list[dict[str, Any]],
    template_ids: list[str],
    rejections: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prefer: str | None = None,
    fetch: Callable[[list[str]], Awaitable[list[Page]]] | None = None,
    max_tokens: int = 3000,
) -> CurateResult:
    """One curation call on the strong alias, plus at most one re-ask with the pages the
    curator asked for."""
    result = CurateResult()
    if not offers:
        return result

    extra_pages: list[Page] = []
    for round_no in (1, 2):
        messages = [
            {"role": "system", "content": CURATE_SYSTEM},
            {
                "role": "user",
                "content": _user_message(offers, promos, template_ids, extra_pages, rejections),
            },
        ]
        try:
            reply = await llm.json_call(messages, model=model, prefer=prefer, max_tokens=max_tokens)
        except BudgetExhausted as exc:
            result.stopped = str(exc)
            result.errors.append(f"curate stopped: {exc}")
            return result
        except LLMError as exc:
            result.errors.append(f"curate: {exc}")
            return result
        result.rounds = round_no
        result.usage.add(reply)
        payload = coerce_payload(reply.text, reply.data)
        decisions, dropped, needs = parse_decisions(payload, raw_text=reply.text)
        result.decisions = decisions
        result.dropped.extend(dropped)
        result.needs_pages = needs
        if round_no == 2 or not needs or fetch is None:
            return result
        extra_pages = await fetch(needs)
        if not extra_pages:
            return result
    return result
