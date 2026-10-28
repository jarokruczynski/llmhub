"""Per-model proof that a pair the registry lists can actually answer.

The account sweep in `health.py` asks one question per account: does this key work. That leaves
most of the registry unproven. A wallet that is empty or a plan that lacks a model shows up only
when a caller picks that model and gets a 402 or a 404 back, so the pool lists hundreds of pairs
as `ok` that nobody has ever heard from.

This prober walks every free (account, model) pair, one tiny request each, and keeps a due date
per pair. What it learns goes through the same parking the router uses for real traffic, so a
refused route leaves the pool until it is worth asking again, and the due date is that moment:

- served call: proven for `model_probe_every_h` hours, real traffic inside the window counts too
- route refused (`not_found`, `unavailable`): due when the park expires
- quota spent: due when the window resets
- anything else (timeout, transport): due again in half an hour

A key or wallet refusal is a fact about the account, not the model. Probing the other models of
the account one by one would be a pile of certain failures, so the first suspicious refusal is
confirmed on two more models and, when they say the same, the rest of the account is parked in
one step and carries one due date.

The same walk also learns the `json` capability for pairs the catalog does not mark with it
(discovered models get only `text`). One json_schema request with a tiny schema; an answer that
parses and matches is the proof, and the router then counts the pair as `json`. A verdict holds
for `model_probe_json_every_h`; a call that never got a verdict (quota, timeout, transport) is
asked again soon. A failed probe only withholds the learned cap, catalog caps stay as written.
cli backends and opt-in-only accounts are never asked: they ignore response_format or are kept
out of automatic traffic on purpose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Entry
from .runtime import Hub
from .store import parse_iso, to_iso

log = logging.getLogger(__name__)

# how often the loop looks at the due list; a run only does the pairs that are due
TICK_SECONDS = 300.0
# pause after startup so the process is serving before it starts asking vendors anything
STARTUP_DELAY_SECONDS = 60.0
# an unexplained failure (timeout, transport) is asked again soon, not parked for hours
RETRY_AFTER = timedelta(minutes=30)
# models asked before one account is judged as a whole, the first failing one included
ACCOUNT_CONFIRMATIONS = 3
# the wallet, said out loud, in any wording the vendors use
BALANCE_TEXTS = (
    "not enough balance",
    "insufficient balance",
    "balance is insufficient",
    "insufficient funds",
    "insufficient_balance",
    "insufficient_quota",
    "payment required",
)
PROBE_TIMEOUT_S = 60.0

JSON_CAP = "json"
# the question names no format on purpose: a model that ignores response_format answers in prose
JSON_PROBE_PROMPT = "Is the sky blue on a clear day? Answer in one word."
JSON_PROBE_MAX_TOKENS = 64
JSON_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}
# outcomes that are a verdict on the capability; anything else is asked again after RETRY_AFTER
JSON_VERDICT_STATUSES = ("truncated", "unsupported_param")


def json_probe_body(model_id: str) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": JSON_PROBE_PROMPT}],
        "max_tokens": JSON_PROBE_MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "probe", "strict": True, "schema": JSON_PROBE_SCHEMA},
        },
    }


def json_answer_ok(content: str) -> bool:
    """The answer is a JSON object the schema accepts. No fence stripping: a vendor that honours
    response_format sends bare JSON, and a fenced one is exactly the prose case."""
    try:
        value = json.loads(content.strip())
    except ValueError:
        return False
    if not isinstance(value, dict) or set(value) != {"answer"}:
        return False
    answer = value.get("answer")
    return isinstance(answer, str) and answer.strip().lower() in ("yes", "no")


def json_verdict(result: dict[str, Any]) -> tuple[bool | None, str]:
    """(verdict, status) of one json probe; verdict None means the call proved nothing."""
    if result.get("ok"):
        if json_answer_ok(str(result.get("content") or result.get("sample") or "")):
            return True, "ok"
        return False, "not_json"
    status = str(result.get("status") or "error")
    if status in JSON_VERDICT_STATUSES:
        return False, status
    if status == "error" and result.get("http_status") == 400:
        # a plain bad request to a ping that worked: the vendor refused the response_format
        return False, "rejected"
    return None, status


def account_level(result: dict[str, Any]) -> bool:
    """True when the refusal names the key or the wallet rather than the model.

    A 403 that only says "no permission" is not enough: a plan may lack one model and serve
    seventy others, so permission stays a per-model fact. 401 and a named empty wallet are the
    account's own.
    """
    if result.get("ok"):
        return False
    status = str(result.get("status") or "")
    http = result.get("http_status")
    if status == "auth" or http in (401, 402):
        return True
    text = str(result.get("error") or "").lower()
    return any(needle in text for needle in BALANCE_TEXTS)


@dataclass
class ProbeRun:
    """What one run did, for the status endpoint and the event feed."""

    started_at: str
    finished_at: str | None = None
    due: int = 0
    probed: int = 0
    ok: int = 0
    failed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    accounts_parked: list[str] = field(default_factory=list)
    json_probed: int = 0
    json_ok: int = 0
    running: bool = True

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self.running,
            "due": self.due,
            "probed": self.probed,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": dict(self.skipped),
            "accounts_parked": list(self.accounts_parked),
            "json_probed": self.json_probed,
            "json_ok": self.json_ok,
        }


class ModelProber:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        self.current: ProbeRun | None = None
        self.last: ProbeRun | None = None
        # pairs the last due_entries() listed only for a json probe: their liveness is proven
        self.json_only: set[tuple[str, str]] = set()

    @property
    def every_h(self) -> int:
        return int(self.hub.settings.model_probe_every_h)

    @property
    def json_every_h(self) -> int:
        return int(self.hub.settings.model_probe_json_every_h)

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    # --- planning ---------------------------------------------------------------------------

    def eligible(self, entry: Entry, now: datetime, disabled: set[str]) -> str | None:
        """Why this pair is not probed at all, or None when it may be."""
        if not entry.model.is_free:
            return "not_free"
        if entry.account.api_key_env and not os.environ.get(entry.account.api_key_env):
            return "missing_api_key"
        if entry.key in disabled:
            return "disabled"
        return None

    def json_due(self, entry: Entry, rows: dict[tuple[str, str], dict[str, Any]], now: datetime) -> bool:
        """The pair should be asked about `json`: the catalog does not say so and no verdict holds."""
        if self.json_every_h <= 0 or entry.is_cli or entry.opt_in_only:
            return False
        if JSON_CAP in entry.model.caps:
            return False
        row = rows.get((entry.account_id, entry.key))
        return row is None or parse_iso(str(row["next_due_ts"])) <= now

    def due_entries(self, now: datetime) -> tuple[list[Entry], dict[str, int]]:
        """Pairs whose proof has run out, never-probed first and then oldest first."""
        hub = self.hub
        disabled = hub.store.disabled_models()
        parked = hub.store.unavailable_all(now)
        probes = hub.store.model_probes_all()
        json_rows = hub.store.learned_caps_rows(JSON_CAP)
        json_only: list[tuple[str, Entry]] = []
        self.json_only = set()
        window = timedelta(hours=max(1, self.every_h))

        def json_alone(entry: Entry) -> bool:
            """Queue a json-only probe for a pair whose liveness is proven; False when not due."""
            if not self.json_due(entry, json_rows, now):
                return False
            json_row = json_rows.get((entry.account_id, entry.key))
            json_only.append(("" if json_row is None else str(json_row["ts"]), entry))
            return True

        since = to_iso(now - window)
        skipped: dict[str, int] = {}
        due: list[tuple[str, Entry]] = []

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for entry in hub.registry.entries():
            reason = self.eligible(entry, now, disabled)
            if reason is not None:
                skip(reason)
                continue
            pair = (entry.account_id, entry.key)
            if pair in parked:
                # parked pairs are asked again by the router on their own schedule; the due
                # date follows the park, so the probe lands right after it expires
                hub.store.defer_model_probe(
                    entry.account_id,
                    entry.key,
                    ts=to_iso(now),
                    status="deferred",
                    next_due=str(parked[pair]["until_ts"]),
                )
                skip("parked")
                continue
            exhausted = hub.quota.exhausted_until(entry, now)
            if exhausted is not None:
                hub.store.defer_model_probe(
                    entry.account_id, entry.key, ts=to_iso(now), status="deferred", next_due=to_iso(exhausted)
                )
                skip("exhausted")
                continue
            row = probes.get(pair)
            if row is not None and parse_iso(row["next_due_ts"]) > now:
                if not json_alone(entry):
                    skip("not_due")
                continue
            stats = hub.store.model_stats(entry.account_id, entry.key)
            last_ok = stats.get("last_ok_at")
            if last_ok and str(last_ok) >= since:
                # served for real inside the window: that is the proof, no request needed
                hub.store.defer_model_probe(
                    entry.account_id,
                    entry.key,
                    ts=to_iso(now),
                    status="deferred",
                    next_due=to_iso(parse_iso(str(last_ok)) + window),
                )
                if not json_alone(entry):
                    skip("recently_served")
                continue
            order = "" if row is None else str(row["ts"])
            due.append((order, entry))

        due.sort(key=lambda item: item[0])
        # liveness first: a pair nobody has heard from matters more than one more capability
        json_only.sort(key=lambda item: item[0])
        self.json_only = {(entry.account_id, entry.key) for _, entry in json_only}
        return [entry for _, entry in due] + [entry for _, entry in json_only], skipped

    # --- one probe --------------------------------------------------------------------------

    async def probe_one(self, entry: Entry, now: datetime) -> dict[str, Any]:
        from .api import probe_entry

        semaphore = self.hub.router.semaphore(entry)
        if semaphore is not None and semaphore.locked():
            return {"ok": False, "status": "in_use", "error": None}
        try:
            return await asyncio.wait_for(probe_entry(self.hub, entry), timeout=PROBE_TIMEOUT_S)
        except TimeoutError:
            return {"ok": False, "status": "timeout", "error": f"no answer inside {int(PROBE_TIMEOUT_S)}s"}

    async def probe_json(self, entry: Entry, now: datetime, run: ProbeRun) -> bool | None:
        """Ask the pair for a json_schema answer and store the verdict. None: skipped or no verdict."""
        from .api import probe_entry

        hub = self.hub
        if not hub.quota.has_room(entry, 0, JSON_PROBE_MAX_TOKENS, now):
            run.skip("json_no_room")
            return None
        semaphore = hub.router.semaphore(entry)
        if semaphore is not None and semaphore.locked():
            run.skip("in_use")
            return None
        try:
            result = await asyncio.wait_for(
                probe_entry(hub, entry, body=json_probe_body(entry.model.id)), timeout=PROBE_TIMEOUT_S
            )
        except TimeoutError:
            result = {"ok": False, "status": "timeout", "error": f"no answer inside {int(PROBE_TIMEOUT_S)}s"}
        verdict, status = json_verdict(result)
        later = datetime.now(UTC)
        if verdict is None:
            due = later + RETRY_AFTER
        else:
            due = later + timedelta(hours=max(1, self.json_every_h))
        detail = str(result.get("content") or result.get("sample") or result.get("error") or "")[:200]
        hub.store.record_learned_cap(
            entry.account_id,
            entry.key,
            JSON_CAP,
            ok=verdict,
            status=status,
            ts=to_iso(later),
            next_due=to_iso(due),
            detail=detail or None,
        )
        run.json_probed += 1
        run.json_ok += 1 if verdict else 0
        return verdict

    def next_due(self, entry: Entry, result: dict[str, Any], now: datetime) -> datetime:
        if result.get("ok"):
            return now + timedelta(hours=max(1, self.every_h))
        parked = self.hub.store.unavailable_until(entry.account_id, entry.key, now)
        if parked is not None:
            return parse_iso(str(parked["until_ts"]))
        exhausted = self.hub.quota.exhausted_until(entry, now)
        if exhausted is not None:
            return exhausted
        return now + RETRY_AFTER

    def record(self, entry: Entry, result: dict[str, Any], now: datetime) -> None:
        due = self.next_due(entry, result, now)
        status = "ok" if result.get("ok") else str(result.get("status") or "error")
        self.hub.store.record_model_probe(
            entry.account_id,
            entry.key,
            ts=to_iso(now),
            ok=bool(result.get("ok")),
            status=status,
            code=result.get("error_code"),
            next_due=to_iso(due),
        )

    def park_account(self, entries: list[Entry], result: dict[str, Any], now: datetime) -> int:
        """Park every given pair of one account for the account TTL, one due date for all."""
        until = now + timedelta(seconds=int(self.hub.settings.model_probe_account_ttl_s))
        kind = "auth" if str(result.get("status")) == "auth" else "not_found"
        reason = f"account refused: {str(result.get('error') or result.get('status') or 'refused')}"
        for entry in entries:
            self.hub.store.mark_unavailable(
                entry.account_id, entry.key, kind, str(result.get("error_code") or ""), reason, until
            )
            self.hub.router._unavailable.add((entry.account_id, entry.key))
            self.hub.store.record_model_probe(
                entry.account_id,
                entry.key,
                ts=to_iso(now),
                ok=False,
                status="account_parked",
                code=str(result.get("error_code") or "") or None,
                next_due=to_iso(until),
            )
        return len(entries)

    async def probe_account(self, entries: list[Entry], run: ProbeRun, budget: list[int]) -> None:
        """Walk one account's due models in order, stopping early on an account-level refusal."""
        hub = self.hub
        gap = float(hub.settings.model_probe_gap_s)
        cli_cap = int(hub.settings.model_probe_cli_daily)
        refused: list[dict[str, Any]] = []
        json_rows = hub.store.learned_caps_rows(JSON_CAP)
        index = 0
        while index < len(entries):
            if self._stopping.is_set() or budget[0] <= 0:
                run.skip("run_budget")
                return
            entry = entries[index]
            index += 1
            if entry.is_cli and cli_cap >= 0:
                day = to_iso(datetime.now(UTC) - timedelta(days=1))
                if hub.store.model_probes_since(entry.account_id, day) >= cli_cap:
                    run.skip("cli_daily_cap")
                    continue
            budget[0] -= 1
            now = datetime.now(UTC)
            if (entry.account_id, entry.key) in self.json_only:
                await self.probe_json(entry, now, run)
                if gap > 0:
                    await asyncio.sleep(gap)
                continue
            result = await self.probe_one(entry, now)
            if result.get("status") == "in_use":
                run.skip("in_use")
                continue
            run.probed += 1
            run.ok += 1 if result.get("ok") else 0
            run.failed += 0 if result.get("ok") else 1
            self.record(entry, result, now)
            if account_level(result):
                refused.append(result)
            else:
                refused.clear()
            if len(refused) >= ACCOUNT_CONFIRMATIONS:
                rest = entries[index:]
                parked = self.park_account(rest, refused[-1], datetime.now(UTC))
                run.accounts_parked.append(entry.account_id)
                hub.store.add_event(
                    kind="unavailable",
                    message=(
                        f"{entry.account_id} refused {ACCOUNT_CONFIRMATIONS} models in a row "
                        f"({refused[-1].get('http_status') or refused[-1].get('status')}); "
                        f"{parked} more pairs parked"
                    )[:500],
                    model=None,
                    account=entry.account_id,
                )
                return
            if result.get("ok") and self.json_due(entry, json_rows, now) and budget[0] > 0:
                if gap > 0:
                    await asyncio.sleep(gap)
                budget[0] -= 1
                await self.probe_json(entry, datetime.now(UTC), run)
            if gap > 0:
                await asyncio.sleep(gap)

    # --- a run ------------------------------------------------------------------------------

    async def run_once(self, *, force: bool = False) -> ProbeRun:
        """Probe every due pair. `force` ignores due dates and probes the whole free registry."""
        now = datetime.now(UTC)
        run = ProbeRun(started_at=to_iso(now))
        self.current = run
        try:
            if force:
                disabled = self.hub.store.disabled_models()
                entries = [e for e in self.hub.registry.entries() if self.eligible(e, now, disabled) is None]
                self.json_only = set()
                skipped: dict[str, int] = {}
            else:
                entries, skipped = self.due_entries(now)
            run.skipped.update(skipped)
            run.due = len(entries)

            by_account: dict[str, list[Entry]] = {}
            for entry in entries:
                by_account.setdefault(entry.account_id, []).append(entry)

            budget = [int(self.hub.settings.model_probe_max_per_run)]
            gate = asyncio.Semaphore(max(1, int(self.hub.settings.model_probe_parallel)))

            async def one_account(items: list[Entry]) -> None:
                async with gate:
                    await self.probe_account(items, run, budget)

            await asyncio.gather(*(one_account(items) for items in by_account.values()))
        finally:
            run.running = False
            run.finished_at = to_iso(datetime.now(UTC))
            self.last = run
            self.current = None
            if run.probed or run.accounts_parked or run.json_probed:
                self.hub.store.add_event(
                    kind="model_probe",
                    message=(
                        f"model probe: {run.probed} probed, {run.ok} ok, {run.failed} failed, "
                        f"{sum(run.skipped.values())} skipped, {len(run.accounts_parked)} account(s) parked, "
                        f"json {run.json_ok}/{run.json_probed}"
                    ),
                )
        return run

    def start_run(self, *, force: bool = False) -> bool:
        """Start a run in the background; False when one is already going."""
        if self.running:
            return False
        self._run_task = asyncio.create_task(self._guarded_run(force), name="llmhub-model-probe-run")
        return True

    async def _guarded_run(self, force: bool) -> None:
        try:
            await self.run_once(force=force)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("model probe run failed")

    def status(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        probes = self.hub.store.model_probes_all()
        next_due = [parse_iso(str(row["next_due_ts"])) for row in probes.values()]
        waiting = [moment for moment in next_due if moment > now]
        return {
            "every_h": self.every_h,
            "running": self.running,
            "current": self.current.as_dict() if self.current else None,
            "last": self.last.as_dict() if self.last else None,
            "tracked_pairs": len(probes),
            "next_due_at": to_iso(min(waiting)) if waiting else None,
        }

    # --- schedule ---------------------------------------------------------------------------

    async def start(self) -> None:
        self._stopping.clear()
        if self.every_h > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="llmhub-model-probe")
            log.info("model prober every %sh, tick %ss", self.every_h, int(TICK_SECONDS))

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._task, self._run_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("model probe ended with an error")
        self._task = None
        self._run_task = None

    async def _loop(self) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=STARTUP_DELAY_SECONDS)
            return
        except TimeoutError:
            pass
        while not self._stopping.is_set():
            if not self.running:
                self.start_run()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
                return
            except TimeoutError:
                continue
