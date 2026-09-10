from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Entry, QuotaWindow
from .store import Store, parse_iso, to_iso
from .vendor_errors import Classification

log = logging.getLogger(__name__)

WINDOW_ORDER = ("hourly", "daily", "monthly")
SCOPE_ORDER = ("hourly", "daily", "monthly", "allowance")
OBSERVABLE_SCOPES = ("hourly", "daily", "monthly")
# The metric the used-so-far heuristic falls back to when a token quota body names none.
OBSERVED_METRIC = "out_tokens"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
METRICS = ("in_tokens", "out_tokens", "total_tokens", "requests")
# Which number a window is reported by when it caps several. Tokens first: they are what a
# caller sizes max_tokens against; a request count is a yes/no and reads last.
METRIC_PRIORITY = ("out_tokens", "total_tokens", "in_tokens", "requests")
# Below this, a vendor's "try again in Ns" reads as pacing, not a window naming its own
# length. Google's daily-quota body says "retry in 9.026s" next to a per-day violation -
# that number describes the per-minute component underneath it, not the day. A window
# measured in hours or more cannot legitimately be re-timed by a sub-minute hint, so one
# is not trusted to shorten hourly/daily/monthly parks; groq's "27m44s" on a rolling TPD
# window clears this bar and still shortens as designed.
RETRY_HINT_MIN_S = 60.0


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("unknown timezone %s, falling back to UTC", name)
        return ZoneInfo("UTC")


def window_bounds(window: str, tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    tz = zone(tz_name)
    local = now.astimezone(tz)
    if window == "hourly":
        start_local = local.replace(minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(hours=1)
    elif window == "daily":
        start_local = datetime.combine(local.date(), time(0, 0), tzinfo=tz)
        end_local = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz)
    elif window == "monthly":
        start_local = datetime.combine(local.date().replace(day=1), time(0, 0), tzinfo=tz)
        next_month_year = local.year + (1 if local.month == 12 else 0)
        next_month = 1 if local.month == 12 else local.month + 1
        end_local = datetime.combine(
            local.date().replace(year=next_month_year, month=next_month, day=1),
            time(0, 0),
            tzinfo=tz,
        )
    else:
        raise ValueError(f"unknown window {window}")
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def primary_metric(limits: dict[str, int]) -> str | None:
    for metric in METRIC_PRIORITY:
        if metric in limits:
            return metric
    return None


@dataclass
class WindowState:
    window: str
    used: int
    limit: int | None
    metric: str | None
    resets_at: datetime | None
    details: dict[str, dict[str, int | None]]
    started_at: datetime | None = None
    limit_source: str = "declared"

    def as_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "limit": self.limit,
            "metric": self.metric,
            "resets_at": to_iso(self.resets_at) if self.resets_at else None,
            "started_at": to_iso(self.started_at) if self.started_at else None,
            "limit_source": self.limit_source,
            "details": self.details,
        }


class QuotaTracker:
    def __init__(self, store: Store) -> None:
        self.store = store

    # --- observed limits -------------------------------------------------
    # A vendor that publishes no numbers still answers 429 eventually. What it served before
    # that 429 is the only cap we know, so it is stored per (account, model, window) and used
    # as the effective limit whenever it is tighter than the declared one (or there is none).

    def observed(self, entry: Entry) -> dict[str, dict[str, Any]]:
        return self.store.observed_limits(entry.account_id, entry.key)

    def specs(
        self, entry: Entry, observed: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, QuotaWindow]:
        """Declared windows plus any window we only know about from an observed cap."""
        specs = dict(entry.model.windows)
        for window in observed if observed is not None else self.observed(entry):
            specs.setdefault(window, QuotaWindow())
        return specs

    def effective_limits(
        self,
        window: str,
        spec: QuotaWindow,
        observed: dict[str, dict[str, Any]] | None,
    ) -> tuple[dict[str, int], str]:
        """Declared limits with every observed metric merged in, tightest wins per metric."""
        limits = dict(spec.limits())
        source = "declared"
        for metric, value in (observed or {}).get(window, {}).items():
            if metric not in METRICS or not isinstance(value, int):
                continue
            declared = limits.get(metric)
            if declared is not None and declared <= value:
                continue
            limits[metric] = int(value)
            source = "observed"
        return limits, source

    def learned_cap(
        self, entry: Entry, scope: str, classification: Classification, now: datetime
    ) -> tuple[str, int] | None:
        """The (metric, cap) a quota error just revealed, or None when it revealed nothing.

        Three sources, in this order:

        1. The vendor states its own ceiling ("Limit 200000", `quotaValue: 20`). That number is
           the truth and is stored as it stands, whatever this account happened to spend.
        2. It names `requests` but no number. A request count is the one thing "used so far"
           measures correctly: the vendor counted the same calls we did, so the count at the
           refusal is the cap.
        3. It names no metric at all but talks about tokens. Then, and only then, the old
           heuristic applies to out tokens. A body that says neither teaches nothing - reading
           tokens spent as a token cap is what turned request-metered tiers into 97-token
           models.
        """
        detail = classification.quota_detail or {}
        metric = detail.get("metric")
        limit = detail.get("limit")
        if metric in METRICS and isinstance(limit, int) and limit > 0:
            return str(metric), int(limit)
        if metric in METRICS and metric != "requests":
            # a token metric with no number attached: what this account spent is not what the
            # vendor allows, and guessing it is what produced the caps this replaced
            return None
        start, _ = window_bounds(scope, entry.model.reset_tz, now)
        used = self.store.window_usage(entry.account_id, entry.key, start)
        if metric == "requests":
            count = int(used.get("requests", 0))
            return ("requests", count) if count > 0 else None
        if "token" not in (classification.message or "").lower():
            return None
        value = int(used.get(OBSERVED_METRIC, 0))
        return (OBSERVED_METRIC, value) if value > 0 else None

    def record_observed(
        self, entry: Entry, classification: Classification, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Note the cap a quota error just revealed, when it is news.

        Only the resetting windows: an `allowance` bucket is a declared total, and a hit on it
        says nothing new. Nothing is stored when the declared window is already at or below
        what the vendor allows, or when the error named no cap this window could learn from.
        """
        scope = classification.scope
        if scope not in OBSERVABLE_SCOPES:
            return None
        now = (now or datetime.now(UTC)).astimezone(UTC)
        learned = self.learned_cap(entry, scope, classification, now)
        if learned is None:
            return None
        metric, value = learned
        spec = entry.model.windows.get(scope)
        declared = spec.limits().get(metric) if spec is not None else None
        if declared is not None and declared <= value:
            return None
        current = self.observed(entry).get(scope, {}).get(metric)
        row = self.store.record_observed_limit(entry.account_id, entry.key, scope, metric, value, to_iso(now))
        if current != value:
            self.store.add_event(
                kind="observed_limit",
                message=f"observed {scope} cap {value} {metric} (declared "
                f"{declared if declared is not None else 'none'})",
                model=entry.key,
                account=entry.account_id,
            )
        return row

    def allowance_start(self, entry: Entry) -> datetime:
        activated = entry.activated_at
        if activated is not None:
            return activated
        first = self.store.first_usage_ts(entry.account_id, entry.key)
        return parse_iso(first) if first else EPOCH

    def bounds(
        self, entry: Entry, window: str, spec: QuotaWindow, now: datetime
    ) -> tuple[datetime, datetime | None]:
        if window == "allowance":
            return self.allowance_start(entry), entry.model.expires_at()
        return window_bounds(window, entry.model.reset_tz, now)

    def expired_at(self, entry: Entry, now: datetime | None = None) -> datetime | None:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        expires = entry.model.expires_at()
        if expires is not None and expires <= now:
            return expires
        return None

    def window_state(
        self,
        entry: Entry,
        window: str,
        spec: QuotaWindow,
        now: datetime | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> WindowState:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        start, end = self.bounds(entry, window, spec, now)
        used = self.store.window_usage(entry.account_id, entry.key, start)
        limits, source = self.effective_limits(
            window, spec, self.observed(entry) if observed is None else observed
        )
        metric = primary_metric(limits)
        details = {
            name: {"used": used.get(name, 0), "limit": limits.get(name)}
            for name in METRICS
            if name in limits or used.get(name)
        }
        return WindowState(
            window=window,
            used=used.get(metric, 0) if metric else used.get("total_tokens", 0),
            limit=limits.get(metric) if metric else None,
            metric=metric,
            resets_at=end,
            details=details,
            started_at=start,
            limit_source=source,
        )

    def windows(
        self,
        entry: Entry,
        now: datetime | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, WindowState]:
        observed = self.observed(entry) if observed is None else observed
        return {
            window: self.window_state(entry, window, spec, now, observed)
            for window, spec in self.specs(entry, observed).items()
        }

    def window_limit(self, entry: Entry, observed: dict[str, dict[str, Any]] | None = None) -> int | None:
        """Most out tokens a single request could ever get on this (account, model).

        Not the remaining amount: the widest a fresh window opens. The tightest window wins,
        because every declared window has to admit the request. None = nothing is known about
        this pair's limits, and an unknown limit never rejects anything.
        """
        observed = self.observed(entry) if observed is None else observed
        caps: list[int] = []
        for window, spec in self.specs(entry, observed).items():
            limits, _ = self.effective_limits(window, spec, observed)
            window_caps = [limits[metric] for metric in ("out_tokens", "total_tokens") if metric in limits]
            if window_caps:
                caps.append(min(window_caps))
        return min(caps) if caps else None

    def room(self, entry: Entry, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        windows: dict[str, dict[str, Any]] = {}
        binding: dict[str, tuple[int, str] | None] = {"out": None, "in": None, "requests": None}
        observed = self.observed(entry)
        for name, spec in self.specs(entry, observed).items():
            limits, _ = self.effective_limits(name, spec, observed)
            start, end = self.bounds(entry, name, spec, now)
            used = self.store.window_usage(entry.account_id, entry.key, start)
            metric = primary_metric(limits)
            remaining = max(0, limits[metric] - used.get(metric, 0)) if metric else None
            windows[name] = {
                "metric": metric,
                "remaining": remaining,
                "resets_at": to_iso(end) if end else None,
            }
            # a request cap is its own axis: it says how many more calls fit, never how big
            # one may be, so it never lands in remaining_out/remaining_in
            for axis, own in (("out", "out_tokens"), ("in", "in_tokens"), ("requests", "requests")):
                pair = (own,) if own == "requests" else (own, "total_tokens")
                caps = [limits[cap] - used.get(cap, 0) for cap in pair if cap in limits]
                if not caps:
                    continue
                value = max(0, min(caps))
                current = binding[axis]
                if current is None or value < current[0]:
                    binding[axis] = (value, name)
        out, inbound, requests = binding["out"], binding["in"], binding["requests"]
        # the window worth naming is the one that actually blocks: a pair with tokens to spare
        # and no calls left is stopped by its request window, not by the token one
        axes = [axis for axis in (out, inbound, requests) if axis is not None]
        blocked = next((axis for axis in axes if axis[0] <= 0), None)
        first = blocked or (axes[0] if axes else None)
        window = first[1] if first else None
        return {
            "windows": windows,
            "remaining_out": out[0] if out else None,
            "remaining_in": inbound[0] if inbound else None,
            "remaining_requests": requests[0] if requests else None,
            "binding_window": window,
            "resets_at": windows[window]["resets_at"] if window else None,
        }

    def has_room(self, entry: Entry, est_in: int = 0, est_out: int = 0, now: datetime | None = None) -> bool:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if self.expired_at(entry, now) is not None:
            return False
        observed = self.observed(entry)
        for window, spec in self.specs(entry, observed).items():
            limits, _ = self.effective_limits(window, spec, observed)
            if not limits:
                continue
            start, _ = self.bounds(entry, window, spec, now)
            used = self.store.window_usage(entry.account_id, entry.key, start)
            planned = {
                "in_tokens": used["in_tokens"] + est_in,
                "out_tokens": used["out_tokens"] + est_out,
                "total_tokens": used["total_tokens"] + est_in + est_out,
                # the call being planned is the one more request that has to fit
                "requests": used["requests"] + 1,
            }
            for metric, limit in limits.items():
                if planned[metric] > limit:
                    return False
        return True

    def earliest_reset(self, entry: Entry, now: datetime) -> tuple[datetime, str | None]:
        windows = self.specs(entry) or {"hourly": QuotaWindow()}
        resets = [
            (window_bounds(window, entry.model.reset_tz, now)[1], window)
            for window in windows
            if window in WINDOW_ORDER
        ]
        if resets:
            return min(resets, key=lambda item: item[0])
        if "allowance" in windows:
            expires = entry.model.expires_at()
            if expires is not None:
                return expires, "allowance"
            return now + timedelta(days=1), "allowance"
        return now + timedelta(hours=1), None

    def next_reset(self, entry: Entry, now: datetime | None = None) -> datetime:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        return self.earliest_reset(entry, now)[0]

    def window_at_limit(self, entry: Entry, window: str, spec: QuotaWindow, now: datetime) -> bool:
        limits, _ = self.effective_limits(window, spec, self.observed(entry))
        if not limits:
            return False
        start, _ = self.bounds(entry, window, spec, now)
        used = self.store.window_usage(entry.account_id, entry.key, start)
        return any(used.get(metric, 0) >= limit for metric, limit in limits.items())

    def saturated_scope(self, entry: Entry, now: datetime) -> str | None:
        windows = self.specs(entry)
        hit = [
            name
            for name in SCOPE_ORDER
            if name in windows and self.window_at_limit(entry, name, windows[name], now)
        ]
        return hit[-1] if hit else None

    def exhaustion_target(
        self, entry: Entry, now: datetime, scope: str | None = None
    ) -> tuple[datetime, str | None, str | None]:
        windows = self.specs(entry)
        chosen = scope if scope in windows else None
        if chosen is None:
            chosen = self.saturated_scope(entry, now)
        expires = entry.model.expires_at()
        if chosen is None:
            until, chosen = self.earliest_reset(entry, now)
        elif chosen == "allowance":
            until = expires if expires is not None else now + timedelta(days=1)
        else:
            until = window_bounds(chosen, entry.model.reset_tz, now)[1]
        note = "no reset known" if chosen == "allowance" and expires is None else None
        return until, chosen, note

    def mark_exhausted(
        self,
        entry: Entry,
        reason: str,
        now: datetime | None = None,
        scope: str | None = None,
        until: datetime | None = None,
    ) -> datetime:
        """`until` is the vendor's own "try again at" and can only shorten the park.

        A 429 that says "try again in 27m" on a daily bucket is naming a rolling window, not
        the calendar day. Believing it costs at most one more 429; ignoring it parks a pair
        that is usable again in half an hour until midnight.

        But a hint under RETRY_HINT_MIN_S is never trusted to shorten an hourly/daily/monthly
        park: it is too short to be describing a window that long, so it is read as the
        per-minute pacing underneath the window rather than the window's own reset. Without
        this, Google's "retry in 9.026s" on a per-day violation parks the pair for 9 seconds
        and the very next selection walks straight back into the same 429.
        """
        now = (now or datetime.now(UTC)).astimezone(UTC)
        override = until
        until, chosen, note = self.exhaustion_target(entry, now, scope)
        if override is not None and override < until:
            hint_is_too_short = (override - now).total_seconds() < RETRY_HINT_MIN_S
            if not (chosen in WINDOW_ORDER and hint_is_too_short):
                until = override
        self.store.set_exhausted(entry.account_id, entry.key, until, reason, chosen)
        label = ", ".join(part for part in (chosen, note) if part)
        suffix = f" ({label})" if label else ""
        self.store.add_event(
            kind="quota",
            message=f"exhausted until {to_iso(until)}{suffix}: {reason}"[:500],
            model=entry.key,
            account=entry.account_id,
        )
        return until

    def exhausted_until(self, entry: Entry, now: datetime | None = None) -> datetime | None:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        row = self.store.exhausted_until(entry.account_id, entry.key, now)
        if not row:
            return None
        from .store import parse_iso

        return parse_iso(row["until_ts"])

    def forgive(self, model_key: str) -> int:
        """Lift the exhaustion marker. Observed caps survive - they are measurements."""
        return self.store.clear_exhausted_for_model(model_key)

    def forget_observed(self, model_key: str) -> int:
        """Drop both the observed window caps and the observed request-size cap for this model."""
        return self.store.clear_observed_limits(model_key) + self.store.clear_request_caps(model_key)


def estimate_request_cost(body: dict[str, Any]) -> tuple[int, int]:
    chars = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chars += len(part["text"])
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        chars += len(prompt)
    est_in = chars // 4
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    est_out = int(max_tokens) if isinstance(max_tokens, (int, float)) and max_tokens else 4096
    return est_in, est_out
