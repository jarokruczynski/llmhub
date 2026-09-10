from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Entry, Registry
from .quota import QuotaTracker
from .store import Store, parse_iso, to_iso
from .vendor_errors import Classification

log = logging.getLogger(__name__)

# One retry per candidate, and a short one. The pool is wide: a second vendor answers sooner
# than a third wait on the first, and a long ladder only spends the caller's deadline.
RETRY_DELAYS: tuple[float, ...] = (2.0,)
COOLDOWN_SECONDS = 60.0
# Longest wait the hub sits through when the vendor names one. Past this the pair goes on
# cooldown for exactly that long instead and the run moves to the next candidate.
RETRY_WAIT_MAX_S = 8.0
NOT_FOUND_TTL_S = 7 * 24 * 3600.0
UNAVAILABLE_TTL_S = 600.0
# `error` is the vendor's opinion of the request. One vendor can be wrong about it; three
# providers that all say the request is bad are describing the request, not themselves.
ERROR_PROVIDERS_STOP = 3
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# One process-wide sequence, so two routers sharing the in-flight registry - a reload swaps the
# router, the probe endpoint builds a throwaway one - never hand out the same call id.
_CALL_IDS: Iterator[int] = itertools.count(1)

# A stream's entry is removed by the generator that consumes it. A generator dropped without
# being closed leaves the entry with no other way out, so a read prunes anything older than a
# call could plausibly be; the client read timeout is ten minutes.
STALE_IN_FLIGHT_S = 3600.0

# How long a run may sit in a semaphore queue: as long as its budget has left, no more. Past
# that the pair is noted `busy` and the run moves on, because a slot that frees up later frees
# up for a caller who stopped waiting.
SLOT_WAIT_STATUS = "busy"
# A bounded queue per (account, model). Waiting behind more than this many runs cannot end
# before the budget does, so the candidate is skipped without ever joining the queue.
SLOT_QUEUE_FACTOR = 2
# How much of what is left of the budget one candidate may spend queueing. Never all of it:
# the rest of the pool is worth more than a longer wait on a pair that is already busy.
SLOT_WAIT_SHARE = 0.5
# How far past the remaining budget one attempt may run before it is cancelled. The grace
# covers the vendor answering just as the budget ends; past it nobody is reading the answer.
ATTEMPT_GRACE_S = 15.0
# Consecutive timeouts on one pair that mean the backend is dead rather than slow.
TIMEOUT_STRIKES = 3
# what `cli_backend` calls its own timeout; named here to keep the router import-free of it
CLI_TIMEOUT_CODE = "cli_timeout"
# Why a run was cancelled. `client_gone` is the disconnect watcher, the other one the owner.
CANCEL_CLIENT_GONE = "client_gone"
CANCEL_BY_OWNER = "cancelled_by_owner"
# The usage status of an attempt nobody is waiting for any more. Not an error: the vendor was
# never given the chance to fail, so it must not count against the model or the app.
ABANDONED_STATUS = "abandoned"


def vendor_bound_s(entry: Entry) -> float | None:
    """The backend's own ceiling on a single call, when it declares one."""
    return float(entry.cli_timeout_s) if entry.is_cli else None


def quota_room(rejected: Iterable[dict[str, Any]]) -> dict[str, int | None]:
    rows = [row for row in rejected if row.get("reason") == "quota"]
    result: dict[str, int | None] = {}
    for key in ("remaining_out", "remaining_in", "remaining_requests"):
        values = [row[key] for row in rows if row.get(key) is not None]
        result[key] = max(values) if values else None
    return result


class UnknownModelError(Exception):
    pass


class NoCandidatesError(Exception):
    def __init__(
        self,
        message: str,
        rejected: list[dict[str, Any]] | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.rejected = rejected or []
        # what the selection was asked for, so the client can see its own constraints next to
        # the rows they rejected instead of guessing which one emptied the pool
        self.constraints = constraints or {}


class UpstreamError(Exception):
    def __init__(
        self,
        classification: Classification,
        status_code: int | None = None,
        body: Any = None,
        latency_ms: int = 0,
    ) -> None:
        super().__init__(classification.message or classification.kind)
        self.classification = classification
        self.status_code = status_code
        self.body = body
        self.latency_ms = latency_ms


class AllCandidatesFailed(Exception):
    def __init__(
        self,
        attempts: list[dict[str, Any]],
        last: UpstreamError | None,
        budget_exhausted: bool = False,
    ) -> None:
        super().__init__("all candidates failed")
        self.attempts = attempts
        self.last = last
        # the pool was not walked to the end: the run ran out of its wall-clock budget first
        self.budget_exhausted = budget_exhausted


@dataclass
class Selection:
    candidates: list[Entry]
    rejected: list[dict[str, Any]] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    alias: str | None = None
    spread: int = 1
    constraints: dict[str, Any] = field(default_factory=dict)

    def rejected_reasons(self) -> set[str]:
        return {row["reason"] for row in self.rejected}


@dataclass
class InFlight:
    """One call the hub is making right now, from the moment the run starts.

    `started_at` is set once per call and survives a retry or a fallback, so the elapsed time
    a dashboard shows is the age of the caller's request, not of the current attempt. `state`
    says which half of the call this is: `waiting` for a concurrency slot, or `running`
    against the vendor.
    """

    call_id: int
    app: str
    model_key: str
    account_id: str
    requested: str
    kind: str
    job_id: str | None
    attempt: int
    started_at: float
    started_iso: str
    tokens_in_estimate: int
    state: str = "waiting"

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "app": self.app,
            "model": self.model_key,
            "account": self.account_id,
            "requested": self.requested,
            "kind": self.kind,
            "job_id": self.job_id,
            "attempt": self.attempt,
            "state": self.state,
            "elapsed_s": round(max(0.0, now - self.started_at), 1),
            "started_at": self.started_iso,
            "tokens_in_estimate": self.tokens_in_estimate,
        }


@dataclass
class RunResult:
    entry: Entry
    result: Any
    attempts: list[dict[str, Any]]
    call_id: int = 0


class Router:
    def __init__(
        self,
        registry: Registry,
        store: Store,
        quota: QuotaTracker,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_wait_max_s: float = RETRY_WAIT_MAX_S,
        not_found_ttl_s: float = NOT_FOUND_TTL_S,
        unavailable_ttl_s: float = UNAVAILABLE_TTL_S,
        attempt_grace_s: float = ATTEMPT_GRACE_S,
    ) -> None:
        self.registry = registry
        self.store = store
        self.quota = quota
        self.retry_delays = tuple(retry_delays)
        self.cooldown_seconds = cooldown_seconds
        self.sleep = sleep
        self.retry_wait_max_s = retry_wait_max_s
        self.not_found_ttl_s = not_found_ttl_s
        self.unavailable_ttl_s = unavailable_ttl_s
        self.attempt_grace_s = attempt_grace_s
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._last_used: dict[tuple[str, str], datetime] = {}
        self._last_used_loaded = False
        self._in_flight: dict[int, InFlight] = {}
        # the task behind each live call, so the owner can end one from the dashboard: a
        # handler's run task for sync and stream, the job worker's task for a job
        self._tasks: dict[int, asyncio.Task[Any]] = {}
        self._cancel_reason: dict[int, str] = {}
        # runs queued on a pair's semaphore right now, so the queue can be bounded and a
        # waiting call is not counted as one the vendor is working on
        self._waiters: dict[tuple[str, str], int] = {}
        # consecutive timeouts per pair: a backend that answers nothing three times running is
        # dead, not slow, and parking it is cheaper than three more caller deadlines
        self._timeouts: dict[tuple[str, str], int] = {}
        # jobs whose worker task was cancelled on purpose. A cancelled worker otherwise looks
        # exactly like a worker that died, and those are requeued rather than ended.
        self.cancelled_jobs: set[str] = set()
        # pairs this process parked as unavailable. A live row keeps the pair out of `select`,
        # so an ok on a pair in this set means the row has expired: the DELETE is worth one
        # call, and every other ok skips the database entirely.
        self._unavailable: set[tuple[str, str]] = set()

    # --- in-flight registry ----------------------------------------------
    # The usage table answers "which model did this app use lately"; nothing answered "right
    # now". The semaphores know a pair is busy but not who is waiting on it, so the calls in
    # progress are kept here: in memory, one entry per `run`, no new table.

    def _register(
        self,
        call_id: int,
        entry: Entry | None,
        *,
        app: str,
        model_request: str,
        kind: str,
        job_id: str | None,
        est_in: int,
    ) -> InFlight:
        """The entry exists from the first line of the run, before any slot is asked for.

        A call queued behind a busy pair is a call the owner should be able to see: waiting is
        where the time goes when a backend stalls, and an entry created only at the vendor
        call would show that time under whichever model the run happened to try last.
        """
        record = InFlight(
            call_id=call_id,
            app=app,
            model_key=entry.key if entry is not None else model_request,
            account_id=entry.account_id if entry is not None else "",
            requested=model_request or (entry.key if entry is not None else ""),
            kind=kind,
            job_id=job_id,
            attempt=1,
            started_at=time.monotonic(),
            started_iso=to_iso(datetime.now(UTC)),
            tokens_in_estimate=est_in,
        )
        self._in_flight[call_id] = record
        return record

    def _retarget(self, record: InFlight, entry: Entry, attempt: int, state: str) -> None:
        record.model_key = entry.key
        record.account_id = entry.account_id
        record.attempt = attempt
        record.state = state

    def release(self, call_id: int) -> None:
        self._in_flight.pop(call_id, None)
        self._tasks.pop(call_id, None)
        self._cancel_reason.pop(call_id, None)

    def in_flight(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        rows: list[dict[str, Any]] = []
        for record in sorted(self._in_flight.values(), key=lambda item: item.started_at):
            if now - record.started_at > STALE_IN_FLIGHT_S:
                # bounded waits and the disconnect watcher end every call long before this, so
                # a pruned entry means one of them has a hole in it
                log.warning(
                    "pruning stale in-flight entry: %s on %s (%s), %.0fs old",
                    record.app,
                    record.model_key,
                    record.state,
                    now - record.started_at,
                )
                self.release(record.call_id)
                continue
            rows.append(record.as_dict(now))
        return rows

    def in_flight_count(self, model_key: str) -> int:
        """Calls this model is actually serving; a run queued for a slot is not one of them."""
        return sum(
            1
            for record in self._in_flight.values()
            if record.model_key == model_key and record.state == "running"
        )

    def waiting_for(self, entry: Entry) -> int:
        return self._waiters.get((entry.account_id, entry.key), 0)

    # --- cancellation ----------------------------------------------------
    # The owner's kill switch and the disconnect watcher end a call the same way: cancel the
    # task that owns the run. Everything else - slot, registry entry, CLI child, usage row -
    # falls out of the cancellation handling inside `run`.

    def cancel_call(self, call_id: int, reason: str = CANCEL_BY_OWNER) -> bool:
        task = self._tasks.get(call_id)
        if task is None or task.done():
            return False
        self._cancel_reason[call_id] = reason
        record = self._in_flight.get(call_id)
        if record is not None and record.job_id:
            self.cancelled_jobs.add(record.job_id)
        task.cancel()
        return True

    def cancel_where(self, predicate: Callable[[InFlight], bool], reason: str = CANCEL_BY_OWNER) -> list[int]:
        targets = [record.call_id for record in list(self._in_flight.values()) if predicate(record)]
        return [call_id for call_id in targets if self.cancel_call(call_id, reason)]

    # --- least recently used ---------------------------------------------
    # The spread strategy needs to know which (account, model) has been idle longest. The
    # in-memory clock is authoritative once the process is running; the usage table seeds it
    # so a restart does not send the whole spread back to the first entry.

    def _load_last_used(self) -> None:
        if self._last_used_loaded:
            return
        self._last_used_loaded = True
        for key, ts in self.store.last_used_by_model().items():
            if key in self._last_used:
                continue
            try:
                self._last_used[key] = parse_iso(ts)
            except ValueError:
                continue

    def last_used(self, entry: Entry) -> datetime:
        self._load_last_used()
        return self._last_used.get((entry.account_id, entry.key), EPOCH)

    def touch(self, entry: Entry, now: datetime | None = None) -> None:
        self._load_last_used()
        self._last_used[(entry.account_id, entry.key)] = now or datetime.now(UTC)

    def semaphore(self, entry: Entry) -> asyncio.Semaphore | None:
        limit = entry.concurrency
        if not limit or limit <= 0:
            return None
        return self._semaphores.setdefault((entry.account_id, entry.key), asyncio.Semaphore(limit))

    def in_cooldown(self, entry: Entry, now: datetime) -> datetime | None:
        until = self._cooldowns.get((entry.account_id, entry.key))
        if until is None:
            return None
        if until <= now:
            self._cooldowns.pop((entry.account_id, entry.key), None)
            return None
        return until

    def set_cooldown(self, entry: Entry, now: datetime | None = None, seconds: float | None = None) -> None:
        now = now or datetime.now(UTC)
        span = self.cooldown_seconds if seconds is None else seconds
        self._cooldowns[(entry.account_id, entry.key)] = now + timedelta(seconds=span)

    def clear_cooldown(self, model_key: str) -> None:
        for key in [k for k in self._cooldowns if k[1] == model_key]:
            self._cooldowns.pop(key, None)

    def mark_unavailable(
        self, entry: Entry, classification: Classification, now: datetime | None = None
    ) -> datetime:
        """Park a pair the vendor refuses to serve, for as long as the refusal is likely to hold."""
        now = now or datetime.now(UTC)
        ttl = self.not_found_ttl_s if classification.kind == "not_found" else self.unavailable_ttl_s
        until = now + timedelta(seconds=ttl)
        self.store.mark_unavailable(
            entry.account_id,
            entry.key,
            classification.kind,
            classification.code,
            classification.message or classification.kind,
            until,
        )
        self._unavailable.add((entry.account_id, entry.key))
        return until

    def note_timeout(self, entry: Entry) -> bool:
        """Count a timeout on this pair and park it once they stop looking like bad luck.

        A backend that never answers costs a full caller deadline per attempt, and the pool
        keeps handing it more callers because nothing about it looks failed. Three in a row is
        the signal; any answer at all resets the count.
        """
        pair = (entry.account_id, entry.key)
        strikes = self._timeouts.get(pair, 0) + 1
        self._timeouts[pair] = strikes
        if strikes < TIMEOUT_STRIKES:
            return False
        self._timeouts.pop(pair, None)
        classification = Classification(
            "unavailable", "timeouts", f"{strikes} consecutive timeouts, no answer from the backend"
        )
        until = self.mark_unavailable(entry, classification)
        self.store.add_event(
            kind="unavailable",
            message=f"{entry.key} ({entry.account_id}) parked: {strikes} consecutive timeouts",
            model=entry.key,
            account=entry.account_id,
        )
        log.info("%s parked after %d timeouts until %s", entry.key, strikes, to_iso(until))
        return True

    def clear_timeouts(self, entry: Entry) -> None:
        self._timeouts.pop((entry.account_id, entry.key), None)

    def unavailable_until(self, entry: Entry, now: datetime) -> datetime | None:
        row = self.store.unavailable_until(entry.account_id, entry.key, now)
        return parse_iso(row["until_ts"]) if row else None

    def clear_unavailable(self, model_key: str, account: str | None = None) -> int:
        cleared = self.store.clear_unavailable(model_key, account)
        for key in [
            k for k in self._unavailable if k[1] == model_key and (account is None or k[0] == account)
        ]:
            self._unavailable.discard(key)
        return cleared

    def resolve_pool(self, model_request: str) -> tuple[list[Entry], list[str], list[str], str | None]:
        entries = list(self.registry.entries())
        alias = self.registry.aliases.get(model_request)
        if alias is not None:
            return entries, list(alias.require), list(alias.prefer), model_request
        exact = [entry for entry in entries if entry.key == model_request]
        if exact:
            return exact, [], [model_request], None
        by_id = [entry for entry in entries if entry.model.id == model_request]
        if by_id:
            return by_id, [], [], None
        raise UnknownModelError(f"unknown model or alias: {model_request}")

    def select(
        self,
        *,
        model_request: str,
        require: Iterable[str] = (),
        prefer: Iterable[str] = (),
        allow_paid: bool = False,
        est_in: int = 0,
        est_out: int = 0,
        now: datetime | None = None,
        check_quota: bool = True,
        app: str | None = None,
        min_context: int | None = None,
        avoid: Iterable[str] = (),
        max_latency_ms: int | None = None,
    ) -> Selection:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        pool, alias_require, alias_prefer, alias = self.resolve_pool(model_request)
        required_caps = list(dict.fromkeys([*alias_require, *require]))
        header_prefer = [item for item in prefer if item]
        disabled = self.store.disabled_models()
        constraints = self.constraints_for(
            model_request, min_context=min_context, avoid=avoid, max_latency_ms=max_latency_ms
        )
        min_context = constraints["min_context"]
        avoided = set(constraints["avoid"])
        max_latency_ms = constraints["max_latency_ms"]
        banned = {row["model"]: row["reason"] for row in self.store.bans_for(app)} if app else {}

        request_caps = self.store.request_caps_all()
        candidates: list[Entry] = []
        rejected: list[dict[str, Any]] = []

        def reject(entry: Entry, reason: str, detail: str | None = None, **extra: Any) -> None:
            rejected.append(
                {
                    "model": entry.key,
                    "account": entry.account_id,
                    "reason": reason,
                    "detail": detail,
                    **extra,
                }
            )

        alias_named = set(alias_prefer)

        for entry in pool:
            if entry.key in disabled:
                reject(entry, "disabled")
                continue
            if entry.key in banned:
                reject(entry, "app_banned", banned[entry.key])
                continue
            # An alias reaches an opt-in-only entry only by naming it in its own prefer list;
            # an explicit provider/model_id request comes through with alias None and is fine.
            if alias is not None and entry.opt_in_only and entry.key not in alias_named:
                reject(entry, "opt_in_only", alias)
                continue
            if required_caps and not set(required_caps).issubset(set(entry.model.caps)):
                reject(entry, "capability", ",".join(sorted(set(required_caps) - set(entry.model.caps))))
                continue
            if min_context is not None:
                # an undeclared context is rejected, not assumed wide enough: the caller asked
                # for a guarantee, and the count of context_unknown rows is what tells the
                # owner how much of the registry still has no context written down
                if entry.model.context is None:
                    reject(entry, "context_unknown")
                    continue
                if entry.model.context < min_context:
                    reject(entry, "context_small", entry.model.context)
                    continue
            if not entry.model.is_free and not allow_paid:
                reject(entry, "paid")
                continue
            if not entry.key_present:
                reject(entry, "no_key", entry.account.api_key_env)
                continue
            expired = self.quota.expired_at(entry, now)
            if expired is not None:
                reject(entry, "expired", to_iso(expired))
                continue
            exhausted = self.quota.exhausted_until(entry, now)
            if exhausted is not None:
                reject(entry, "exhausted", to_iso(exhausted))
                continue
            unavailable = self.store.unavailable_until(entry.account_id, entry.key, now)
            if unavailable is not None:
                reject(
                    entry,
                    "unavailable",
                    f"{unavailable['kind']}: {unavailable['code']}",
                    until=unavailable["until_ts"],
                )
                continue
            cooldown = self.in_cooldown(entry, now)
            if cooldown is not None:
                reject(entry, "cooldown", to_iso(cooldown))
                continue
            if check_quota and not self.quota.has_room(entry, est_in, est_out, now):
                room = self.quota.room(entry, now)
                reject(
                    entry,
                    "quota",
                    room["binding_window"],
                    remaining_out=room["remaining_out"],
                    remaining_in=room["remaining_in"],
                    remaining_requests=room["remaining_requests"],
                    resets_at=room["resets_at"],
                    requested_out=est_out,
                    requested_in=est_in,
                )
                continue
            caps = request_caps.get((entry.account_id, entry.key), {})
            observed_cap = caps.get("max_request_tokens")
            ceiling = entry.input_ceiling(est_out, observed_cap)
            if ceiling is not None and ceiling < est_in:
                reject(entry, "too_large", ceiling, requested_in=est_in)
                continue
            out_cap = caps.get("max_out_tokens")
            if out_cap is not None and est_out > out_cap:
                reject(entry, "too_large", {"max_out_tokens": out_cap}, requested_out=est_out)
                continue
            candidates.append(entry)

        order = {entry.key: index for index, entry in enumerate(pool)}
        # one query for the whole pool, and only when the ordering actually asks about latency
        latency = self.store.avg_latency_by_model() if max_latency_ms is not None and candidates else {}

        def busy(entry: Entry) -> int:
            semaphore = self.semaphore(entry)
            return 1 if semaphore is not None and semaphore.locked() else 0

        def penalty(entry: Entry) -> int:
            """How far a soft preference pushes this entry down. Never removes it from the pool."""
            score = 1 if avoided.intersection(entry.model.caps) else 0
            measured = latency.get((entry.account_id, entry.key))
            if max_latency_ms is not None and measured is not None and measured > max_latency_ms:
                score += 1
            return score

        def sort_key(entry: Entry) -> tuple[int, int, int, int, int]:
            header_rank = header_prefer.index(entry.key) if entry.key in header_prefer else len(header_prefer)
            prefer_rank = alias_prefer.index(entry.key) if entry.key in alias_prefer else len(alias_prefer)
            # header prefer sits above the penalty: an explicit pick is explicit. Alias prefer
            # sits below it, so an avoided model on the alias list still sorts behind every
            # candidate that avoids nothing.
            return (busy(entry), header_rank, penalty(entry), prefer_rank, order.get(entry.key, 0))

        candidates.sort(key=sort_key)
        spread = self.spread_for(model_request)
        return Selection(
            candidates=self.apply_spread(candidates, spread, header_prefer, busy),
            rejected=rejected,
            require=required_caps,
            alias=alias,
            spread=spread,
            constraints=constraints,
        )

    def constraints_for(
        self,
        model_request: str,
        *,
        min_context: int | None = None,
        avoid: Iterable[str] = (),
        max_latency_ms: int | None = None,
    ) -> dict[str, Any]:
        """Alias and request constraints combined, always to the stricter of the two.

        The alias is the owner's standing description of the job, the headers are one caller's;
        neither may loosen the other, so the floor is the higher one, the ceiling the lower one
        and the avoid lists are unioned.
        """
        alias = self.registry.aliases.get(model_request)
        contexts = [value for value in (min_context, alias.min_context if alias else None) if value]
        latencies = [value for value in (max_latency_ms, alias.max_latency_ms if alias else None) if value]
        avoid_caps = list(dict.fromkeys([*(alias.avoid if alias else []), *(item for item in avoid if item)]))
        return {
            "min_context": max(contexts) if contexts else None,
            "avoid": avoid_caps,
            "max_latency_ms": min(latencies) if latencies else None,
        }

    def spread_for(self, model_request: str) -> int:
        alias = self.registry.aliases.get(model_request)
        return alias.spread_for(model_request) if alias is not None else 1

    def apply_spread(
        self,
        candidates: list[Entry],
        spread: int,
        header_prefer: Sequence[str],
        busy: Callable[[Entry], int],
    ) -> list[Entry]:
        """Rotate over the first `spread` candidates instead of hammering the first one.

        Free tiers are per model, so a strict prefer list burns one model's day while the rest
        of the pool sits idle. The active set keeps its prefer-order membership - only the
        order inside it changes, least recently used first, free semaphores before busy ones.
        Everything past the active set stays where it was, as fallback.
        """
        if spread <= 1 or len(candidates) < 2:
            return candidates
        pinned = [entry for entry in candidates if entry.key in header_prefer]
        rest = [entry for entry in candidates if entry.key not in header_prefer]
        active = sorted(rest[:spread], key=lambda entry: (busy(entry), self.last_used(entry)))
        return [*pinned, *active, *rest[spread:]]

    def next_window_at(self, model_request: str, allow_paid: bool = False) -> str | None:
        now = datetime.now(UTC)
        try:
            pool, _, _, _ = self.resolve_pool(model_request)
        except UnknownModelError:
            return None
        resets: list[datetime] = []
        for entry in pool:
            if not entry.model.is_free and not allow_paid:
                continue
            if self.quota.expired_at(entry, now) is not None:
                continue
            resets.append(self.quota.next_reset(entry, now))
        return to_iso(min(resets)) if resets else None

    async def run(
        self,
        candidates: Sequence[Entry],
        call: Callable[[Entry, int], Awaitable[Any]],
        *,
        app: str = "unknown",
        model_request: str = "",
        kind: str = "sync",
        job_id: str | None = None,
        est_in: int = 0,
        on_attempt: Callable[[dict[str, Any]], None] | None = None,
        budget_s: float | None = None,
    ) -> RunResult:
        """Walk the candidates until one answers. No vendor failure escapes this loop.

        Every kind of failure is a fact about one candidate, so it parks that candidate in the
        way its kind deserves and the run moves on. The caller sees a result or
        AllCandidatesFailed, never a vendor's status code.
        """
        attempts: list[dict[str, Any]] = []
        last_error: UpstreamError | None = None
        call_id = next(_CALL_IDS)
        # a stream is still being served after this returns, so its entry is handed to the
        # generator that consumes the body and released there instead
        hold = False
        started = time.monotonic()
        budget_exhausted = False
        error_providers: set[str] = set()

        def over_budget(planned_sleep: float = 0.0) -> bool:
            if budget_s is None:
                return False
            return (time.monotonic() - started) + planned_sleep > budget_s

        def note(entry: Entry, status: str, error_code: str | None, latency_ms: int) -> None:
            record = {
                "model": entry.key,
                "account": entry.account_id,
                "status": status,
                "error_code": error_code,
                "latency_ms": latency_ms,
                "attempt": len(attempts) + 1,
            }
            attempts.append(record)
            if on_attempt:
                on_attempt(record)

        def give_up(entry: Entry, failure: str, code: str | None) -> None:
            self.set_cooldown(entry)
            self.store.add_event(
                kind="fallback",
                message=f"{entry.key} ({entry.account_id}) failed: {code or failure}",
                model=entry.key,
                account=entry.account_id,
            )

        def remaining_budget() -> float | None:
            if budget_s is None:
                return None
            return budget_s - (time.monotonic() - started)

        def attempt_cap(entry: Entry) -> float | None:
            """How long one attempt may run: the shorter of the caller's deadline and the
            backend's own ceiling, plus a grace.

            A CLI with a 240 s print timeout must not outlive a 90 s request budget. The grace
            is what keeps the two from racing: a backend that stops itself gets to say why it
            stopped, which is worth more than the second it costs.
            """
            remaining = remaining_budget()
            if remaining is None:
                return None
            bound = vendor_bound_s(entry)
            limit = max(0.0, remaining)
            if bound is not None:
                limit = min(limit, bound)
            return limit + self.attempt_grace_s

        record = self._register(
            call_id,
            candidates[0] if candidates else None,
            app=app,
            model_request=model_request,
            kind=kind,
            job_id=job_id,
            est_in=est_in,
        )
        current: Entry | None = candidates[0] if candidates else None
        self._tasks[call_id] = asyncio.current_task()  # type: ignore[assignment]

        try:
            for entry in candidates:
                # the first candidate always runs: a budget shorter than one call would turn
                # every request into a 502 without ever asking a vendor
                if attempts and over_budget():
                    budget_exhausted = True
                    break
                current = entry
                self._retarget(record, entry, len(attempts) + 1, "waiting")
                semaphore = self.semaphore(entry)
                queued = False
                if semaphore is not None:
                    queue_max = max(1, entry.concurrency or 1) * SLOT_QUEUE_FACTOR
                    if self.waiting_for(entry) >= queue_max:
                        # a queue this deep cannot clear inside anyone's deadline; joining it
                        # would only spend the run's budget standing still
                        note(entry, SLOT_WAIT_STATUS, "slot_queue_full", 0)
                        continue
                    queued = semaphore.locked()
                    waited = time.monotonic()
                    remaining = remaining_budget()
                    if not await self._acquire_slot(
                        semaphore, entry, None if remaining is None else remaining * SLOT_WAIT_SHARE
                    ):
                        note(entry, SLOT_WAIT_STATUS, "slot_wait", int((time.monotonic() - waited) * 1000))
                        continue
                try:
                    # a run that actually queued for this slot may have lost its deadline while
                    # standing in line: hand the slot on rather than call for nobody. A slot
                    # that was free is not re-checked - the first candidate always runs.
                    if queued and over_budget():
                        budget_exhausted = True
                        break
                    for index in range(len(self.retry_delays) + 1):
                        self.touch(entry)
                        self._retarget(record, entry, len(attempts) + 1, "running")
                        attempt_started = time.monotonic()
                        try:
                            cap = attempt_cap(entry)
                            if cap is None:
                                result = await call(entry, len(attempts) + 1)
                            else:
                                result = await asyncio.wait_for(call(entry, len(attempts) + 1), timeout=cap)
                        except TimeoutError:
                            # the vendor call is cancelled by now: an http request is closed, a
                            # CLI child killed. Nothing is left to wait for and nothing is left
                            # of the budget either, so the run stops here.
                            note(
                                entry,
                                "retry",
                                "attempt_timeout",
                                int((time.monotonic() - attempt_started) * 1000),
                            )
                            self.set_cooldown(entry)
                            self.note_timeout(entry)
                            budget_exhausted = True
                            break
                        except UpstreamError as exc:
                            last_error = exc
                            failure = exc.classification.kind
                            note(entry, failure, exc.classification.code, exc.latency_ms)
                            if failure == "quota":
                                # before the marker: an observed window can change which window
                                # the exhaustion is pinned to
                                self.quota.record_observed(entry, exc.classification)
                                wait = exc.classification.retry_after_s
                                self.quota.mark_exhausted(
                                    entry,
                                    exc.classification.message or exc.classification.rule or "quota",
                                    scope=exc.classification.scope,
                                    # a vendor naming its own "try again at" knows better than
                                    # the calendar: a rolling day is not the day the hub tracks
                                    until=(
                                        datetime.now(UTC) + timedelta(seconds=wait)
                                        if wait is not None
                                        else None
                                    ),
                                )
                                break
                            if failure == "too_large":
                                # not a quota hit and not retryable on this candidate: record
                                # what the vendor actually allows so the next selection skips it
                                # outright for a request this size, then move straight on
                                detail = exc.classification.detail
                                cap = detail.get("max_request_tokens")
                                out_cap = detail.get("max_out_tokens")
                                if cap is not None or out_cap is not None:
                                    self.store.record_request_cap(
                                        entry.account_id,
                                        entry.key,
                                        int(cap) if cap is not None else None,
                                        source=entry.provider_name,
                                        max_out_tokens=int(out_cap) if out_cap is not None else None,
                                    )
                                break
                            if failure == "truncated":
                                # a normal outcome for prose, a broken one for the json the
                                # caller asked for; the caller already logged the event
                                break
                            if failure in ("not_found", "unavailable"):
                                # the route itself, not the request: no retry can change it, and
                                # the pair stays out of the pool until its parking expires
                                until = self.mark_unavailable(entry, exc.classification)
                                self.store.add_event(
                                    kind="unavailable",
                                    message=f"{entry.key} ({entry.account_id}) {failure} "
                                    f"{exc.classification.code}: {exc.classification.message}"[:500],
                                    model=entry.key,
                                    account=entry.account_id,
                                )
                                log.info("%s parked as %s until %s", entry.key, failure, to_iso(until))
                                break
                            if failure == "retry":
                                wait = exc.classification.retry_after_s
                                if wait is not None and wait > self.retry_wait_max_s:
                                    # the vendor named a wait longer than this call can afford:
                                    # hold the pair for exactly that long and try someone else
                                    self.set_cooldown(entry, seconds=wait)
                                    break
                                if index < len(self.retry_delays):
                                    delay = wait if wait is not None else self.retry_delays[index]
                                    if over_budget(delay):
                                        budget_exhausted = True
                                        break
                                    await self.sleep(delay)
                                    continue
                                give_up(entry, failure, exc.classification.code)
                                break
                            if failure == "error":
                                if exc.classification.code == CLI_TIMEOUT_CODE:
                                    # the backend's own timeout rather than ours: the same fact
                                    # about the pair, so it counts towards the same strikes
                                    self.note_timeout(entry)
                                error_providers.add(entry.provider_name)
                            give_up(entry, failure, exc.classification.code)
                            break
                        else:
                            note(entry, "ok", None, int(getattr(result, "latency_ms", 0) or 0))
                            self.clear_timeouts(entry)
                            if (entry.account_id, entry.key) in self._unavailable:
                                self.clear_unavailable(entry.key, entry.account_id)
                            hold = kind == "stream"
                            return RunResult(entry=entry, result=result, attempts=attempts, call_id=call_id)
                finally:
                    if semaphore is not None:
                        semaphore.release()
                if budget_exhausted:
                    break
                if len(error_providers) >= ERROR_PROVIDERS_STOP:
                    log.info("stopping run: %d providers rejected the request itself", len(error_providers))
                    break
            raise AllCandidatesFailed(attempts, last_error, budget_exhausted=budget_exhausted)
        except asyncio.CancelledError:
            self._note_abandoned(
                call_id,
                current,
                app=app,
                kind=kind,
                attempt=len(attempts) + 1,
                elapsed_s=time.monotonic() - started,
                on_attempt=on_attempt,
                attempts=attempts,
            )
            raise
        finally:
            self._tasks.pop(call_id, None)
            self._cancel_reason.pop(call_id, None)
            if not hold:
                self._in_flight.pop(call_id, None)

    def _note_abandoned(
        self,
        call_id: int,
        entry: Entry | None,
        *,
        app: str,
        kind: str,
        attempt: int,
        elapsed_s: float,
        on_attempt: Callable[[dict[str, Any]], None] | None,
        attempts: list[dict[str, Any]],
    ) -> None:
        """Book the attempt nobody is waiting for any more.

        No cooldown and no fallback event: the candidate did nothing wrong, and parking it
        would punish the pool for a client that walked away. The usage row records where the
        run was when it was cut off - the vendor may well have counted the call - under a
        status every error sum skips.
        """
        reason = self._cancel_reason.get(call_id, CANCEL_CLIENT_GONE)
        if entry is None:
            return
        record = {
            "model": entry.key,
            "account": entry.account_id,
            "status": ABANDONED_STATUS,
            "error_code": reason,
            "latency_ms": int(elapsed_s * 1000),
            "attempt": attempt,
        }
        attempts.append(record)
        if on_attempt:
            on_attempt(record)
        self.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status=ABANDONED_STATUS,
            latency_ms=int(elapsed_s * 1000),
            attempt=attempt,
            error_code=reason,
            stream=kind == "stream",
        )
        self.store.add_event(
            kind="cancel" if reason == CANCEL_BY_OWNER else CANCEL_CLIENT_GONE,
            message=f"{app} {reason} on {entry.key} ({entry.account_id}) after {elapsed_s:.1f}s",
            app=app,
            model=entry.key,
            account=entry.account_id,
        )

    async def _acquire_slot(self, semaphore: asyncio.Semaphore, entry: Entry, timeout: float | None) -> bool:
        """Wait for one of the pair's slots, but never past the caller's own deadline."""
        pair = (entry.account_id, entry.key)
        self._waiters[pair] = self._waiters.get(pair, 0) + 1
        try:
            if timeout is None:
                await semaphore.acquire()
                return True
            if timeout <= 0:
                return False
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            left = self._waiters.get(pair, 1) - 1
            if left > 0:
                self._waiters[pair] = left
            else:
                self._waiters.pop(pair, None)
