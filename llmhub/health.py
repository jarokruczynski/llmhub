"""Periodic proof that an idle account still works.

Real traffic already answers this for the accounts it touches: a served call writes a usage row
and the status page reads the newest one per account. What traffic cannot answer is whether an
account nothing has called lately still works - a key revoked, a plan changed, a login expired -
because that only surfaces when something needs the account and fails.

So the sweep probes exactly those. Every probe is one request against a free allowance and some
allowances are small (a Gemini CLI plan counts whole requests, 1500 a day), so it buys nothing
where traffic has already answered: an account with a served call inside the window is left
alone. What remains is at most one probe per idle account per window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .runtime import Hub
from .store import parse_iso, to_iso

log = logging.getLogger(__name__)

# how long the loop may sleep in one go, so stopping stays responsive on shutdown
SWEEP_POLL_SECONDS = 300.0
# a backend that does not answer inside this is reported as a timeout and the sweep moves on.
# A CLI backend may sit on a 600 s print timeout, and one of those must not stretch the run.
PROBE_TIMEOUT_S = 60.0


def _recent_success(hub: Hub, provider_name: str, account_id: str, models: Any, since: str) -> str | None:
    """The newest served call for this account inside the window, or None."""
    newest: str | None = None
    for model in models:
        stats = hub.store.model_stats(account_id, f"{provider_name}/{model.id}")
        last_ok = stats.get("last_ok_at")
        if last_ok and str(last_ok) >= since and (newest is None or str(last_ok) > newest):
            newest = str(last_ok)
    return newest


async def sweep_once(hub: Hub, *, now: datetime | None = None, stale_after_h: float = 0.0) -> dict[str, Any]:
    """Probe one free model per eligible account and record what happened.

    `stale_after_h` is the window real traffic counts for: an account served inside it is
    skipped as already proven. Zero means probe every eligible account, which is what the
    manual button does - someone asking for a check now means all of them.
    """
    # deferred: the probe lives with the endpoints, which import this module
    from .api import probe_entry

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    disabled = hub.store.disabled_models()
    unavailable_all = hub.store.unavailable_all(moment)
    since = to_iso(moment - timedelta(hours=stale_after_h)) if stale_after_h > 0 else None

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for provider_name, provider in hub.registry.providers.items():
        for account in provider.accounts:
            if since is not None:
                served_at = _recent_success(hub, provider_name, account.id, provider.models, since)
                if served_at is not None:
                    skipped.append(
                        {
                            "provider": provider_name,
                            "account": account.id,
                            "reason": "recently_served",
                            "last_ok_at": served_at,
                        }
                    )
                    continue

            candidate_entry = None
            skip_reason = None
            if account.api_key_env and not os.environ.get(account.api_key_env):
                skip_reason = "missing_api_key"
            else:
                for model in provider.models:
                    if not model.is_free:
                        continue
                    entry = hub.registry.entry(f"{provider_name}/{model.id}", account.id)
                    if entry is None:
                        continue
                    if entry.key in disabled:
                        skip_reason = "disabled"
                        continue
                    if entry.key in unavailable_all:
                        skip_reason = "parked_unavailable"
                        continue
                    room = hub.quota.room(entry, moment)
                    if room["remaining_out"] == 0 or room["remaining_requests"] == 0:
                        skip_reason = "exhausted"
                        continue
                    candidate_entry = entry
                    break

            if candidate_entry is None:
                skipped.append(
                    {
                        "provider": provider_name,
                        "account": account.id,
                        "reason": skip_reason or "no_eligible_free_model",
                    }
                )
                continue

            # A slot held right now means real traffic is on this pair, which answers the
            # question the probe was going to ask - and waiting for the slot would queue the
            # sweep behind a job that may run for minutes.
            semaphore = hub.router.semaphore(candidate_entry)
            if semaphore is not None and semaphore.locked():
                skipped.append(
                    {
                        "provider": provider_name,
                        "account": account.id,
                        "model": candidate_entry.key,
                        "reason": "in_use",
                    }
                )
                continue

            try:
                probe_res = await asyncio.wait_for(probe_entry(hub, candidate_entry), timeout=PROBE_TIMEOUT_S)
            except TimeoutError:
                probe_res = {
                    "ok": False,
                    "status": "timeout",
                    "latency_ms": int(PROBE_TIMEOUT_S * 1000),
                    "error": f"no answer inside {int(PROBE_TIMEOUT_S)}s",
                }
            results.append(
                {
                    "provider": provider_name,
                    "account": account.id,
                    "model": candidate_entry.key,
                    "ok": probe_res.get("ok", False),
                    "status": "ok" if probe_res.get("ok") else probe_res.get("status", "error"),
                    "latency_ms": probe_res.get("latency_ms", 0),
                    "error": probe_res.get("error"),
                }
            )

    ts = to_iso(moment)
    hub.store.record_health_sweep(ts=ts, probed=len(results), skipped=len(skipped), results=results)
    hub.store.add_event(
        kind="health_sweep",
        message=f"health sweep: probed {len(results)} account(s), skipped {len(skipped)}",
    )
    return {
        "timestamp": ts,
        "probed_count": len(results),
        "skipped_count": len(skipped),
        "request_cost": len(results),
        "results": results,
        "skipped": skipped,
    }


def last_sweep(hub: Hub) -> dict[str, Any]:
    """The newest recorded sweep, in the shape the API answers with."""
    row = hub.store.last_health_sweep()
    if not row:
        return {"last_sweep_at": None, "probed_count": 0, "skipped_count": 0, "results": []}
    try:
        results = json.loads(row["results"] or "[]")
    except ValueError:
        results = []
    return {
        "last_sweep_at": row["ts"],
        "probed_count": int(row["probed"] or 0),
        "skipped_count": int(row["skipped"] or 0),
        "results": results,
    }


class HealthSweepService:
    """Runs the sweep every `health_sweep_every_h` hours; 0 turns it off.

    The clock is the last recorded sweep, not process start, so restarting the hub neither
    re-probes everything nor postpones the next one forever.
    """

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()

    @property
    def every_h(self) -> int:
        return int(self.hub.settings.health_sweep_every_h)

    def next_run_dt(self, now: datetime | None = None) -> datetime | None:
        if self.every_h <= 0:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        row = self.hub.store.last_health_sweep()
        if not row or not row.get("ts"):
            return moment
        return parse_iso(str(row["ts"])) + timedelta(hours=self.every_h)

    def next_run_at(self, now: datetime | None = None) -> str | None:
        target = self.next_run_dt(now)
        return to_iso(target) if target else None

    async def start(self) -> None:
        self._stopping.clear()
        if self.every_h > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="llmhub-health-sweep")
            log.info("health sweep every %sh (next %s)", self.every_h, self.next_run_at())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            target = self.next_run_dt()
            if target is None:
                return
            delay = max(0.0, (target - datetime.now(UTC)).total_seconds())
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=min(delay, SWEEP_POLL_SECONDS))
                    return
                except TimeoutError:
                    continue
            try:
                await sweep_once(self.hub, stale_after_h=self.every_h)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # a sweep that dies must not take the schedule with it
                log.exception("health sweep failed")
                self.hub.store.record_health_sweep(
                    ts=to_iso(datetime.now(UTC)), probed=0, skipped=0, results=[]
                )
