from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Registry
from ..promo_identity import identify
from ..store import Store, now_iso

log = logging.getLogger(__name__)

SOURCE = "scout"
# a row the owner already turned into an account is history, not a lead: never rewrite it
FROZEN_STATUS = "used"
REPORT_FINDS = 3


@dataclass
class ApplyResult:
    new: int = 0
    updated: int = 0
    skipped: int = 0
    skipped_rejected: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_decisions(
    store: Store,
    decisions: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    registry: Registry | None = None,
) -> ApplyResult:
    """Post/patch promo rows straight through the store - same process, same table the API
    serves. Nothing is written on a dry run.

    Rows are matched by provider identity, the same way a hand-written post is: the curator
    naming a fresh url for a vendor already on the watchlist is an update to that row, not a
    second entry. A row the owner has already turned into an account still collects the note,
    but keeps its status and its endpoint.
    """
    result = ApplyResult()
    by_id = {int(row["id"]): row for row in store.promos()}

    for decision in decisions:
        entry = dict(decision)
        action = entry.get("action")
        row = entry.get("row") or {}
        target: dict[str, Any] | None = None

        if action == "skip":
            result.skipped += 1
            entry["outcome"] = "skipped"
            if entry.get("rejected_match"):
                result.skipped_rejected += 1
                entry["outcome"] = "skipped_rejected"
            result.decisions.append(entry)
            continue

        promo_id = entry.get("promo_id")
        if promo_id is not None:
            target = by_id.get(int(promo_id))
            if target is None:
                result.skipped += 1
                entry["outcome"] = "skipped"
                entry["outcome_reason"] = f"unknown promo {promo_id}"
                result.errors.append(f"decision names unknown promo {promo_id}")
                result.decisions.append(entry)
                continue

        named = target or {}
        provider = str(row.get("provider") or named.get("provider") or "unknown")
        identity = identify(
            provider,
            row.get("url") or named.get("url"),
            row.get("base_url") or named.get("base_url"),
            store,
            registry,
            learn=not dry_run,
        )
        key = str(named.get("identity") or "") or identity.key
        if target is not None and not named.get("identity") and store.promo_by_identity(key) is None:
            # the decision named a row from before identities existed: adopt it as the owner
            if not dry_run:
                store.set_promo_identity(int(target["id"]), key)
        else:
            target = store.promo_by_identity(key) or target
        entry["identity"] = key

        frozen = target is not None and str(target.get("status")) == FROZEN_STATUS
        if frozen:
            entry["outcome_reason"] = f"promo {target['id']} is {FROZEN_STATUS}"
        if target is None:
            result.new += 1
            entry["outcome"] = "new"
        else:
            result.updated += 1
            entry["outcome"] = "updated"
            entry["promo_id"] = int(target["id"])

        if not dry_run:
            written, _ = store.upsert_promo(
                identity=key,
                provider=provider,
                url=row.get("url"),
                note=row.get("note"),
                # a frozen row keeps what the owner acted on: only the note grows
                status=None if frozen else "new",
                source=SOURCE,
                expires_at=None if frozen else row.get("expires_at"),
                base_url=None if frozen else row.get("base_url"),
                api_key_env=None if frozen else row.get("api_key_env"),
            )
            entry["promo_id"] = int(written["id"])
            by_id[int(written["id"])] = written
        result.decisions.append(entry)
    return result


def _find_lines(decisions: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for entry in decisions:
        if entry.get("outcome") not in ("new", "updated"):
            continue
        row = entry.get("row") or {}
        note = (row.get("note") or entry.get("reason") or "").strip().replace("\n", " ")
        lines.append(f"  {len(lines) + 1}. {row.get('provider')} - {note[:110]} ({row.get('url') or '-'})")
        if len(lines) == REPORT_FINDS:
            break
    return lines or ["  none"]


def build_report(
    *,
    run_id: int,
    started_at: str,
    counts: dict[str, int],
    models_used: dict[str, list[str]],
    tokens: dict[str, dict[str, int]],
    errors: list[str],
    decisions: list[dict[str, Any]],
    invalid_reasons: list[str] | None = None,
    dry_run: bool = False,
) -> str:
    """10-15 lines: what was read, what came out, what it cost, what broke, top finds."""
    extract_tokens = tokens.get("extract", {})
    curate_tokens = tokens.get("curate", {})
    head = f"# scout run {run_id} - started {started_at}" + (" (dry run)" if dry_run else "")
    lines = [
        head,
        f"- sources: {counts.get('sources', 0)}",
        f"- pages: {counts.get('pages_fetched', 0)} fetched, {counts.get('pages_changed', 0)} changed, "
        f"{counts.get('pages_fetched', 0) - counts.get('pages_changed', 0)} unchanged",
        f"- offers: {counts.get('offers', 0)} extracted, {counts.get('offers_dropped', 0)} dropped",
        f"- decisions: {counts.get('new', 0)} new, {counts.get('updated', 0)} updated, "
        f"{counts.get('skipped', 0)} skipped, {counts.get('decisions_dropped', 0)} invalid",
        f"- skipped as rejected: {counts.get('skipped_rejected', 0)}",
        f"- extract models: {', '.join(models_used.get('extract') or ['-'])}",
        f"- curate models: {', '.join(models_used.get('curate') or ['-'])}",
        f"- extract tokens: {extract_tokens.get('in', 0)} in / {extract_tokens.get('out', 0)} out",
        f"- curate tokens: {curate_tokens.get('in', 0)} in / {curate_tokens.get('out', 0)} out",
        f"- errors: {len(errors)}",
    ]
    for message in errors[:2]:
        lines.append(f"  - {message[:160]}")
    if invalid_reasons:
        joined = "; ".join(reason[:80] for reason in invalid_reasons[:2])
        lines.append(f"  - invalid decisions: {joined}"[:200])
    lines.append("- top finds:")
    lines.extend(_find_lines(decisions))
    return "\n".join(lines)


def write_run(
    store: Store,
    run_id: int,
    *,
    counts: dict[str, int],
    models_used: dict[str, list[str]],
    tokens: dict[str, dict[str, int]],
    errors: list[str],
    decisions: list[dict[str, Any]],
    report_md: str,
    invalid_reasons: list[str] | None = None,
    status: str = "done",
) -> None:
    store.update_scout_run(
        run_id,
        finished_at=now_iso(),
        status=status,
        sources=counts.get("sources", 0),
        pages_fetched=counts.get("pages_fetched", 0),
        pages_changed=counts.get("pages_changed", 0),
        offers=counts.get("offers", 0),
        new_count=counts.get("new", 0),
        updated_count=counts.get("updated", 0),
        skipped_count=counts.get("skipped", 0),
        models_used=json.dumps(models_used),
        tokens=json.dumps(tokens),
        errors=json.dumps(errors),
        decisions=json.dumps(decisions),
        invalid_reasons=json.dumps(invalid_reasons or []),
        report_md=report_md,
    )
