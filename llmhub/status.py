from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import CLI_KIND, Entry
from .runtime import Hub
from .store import LIVE_JOB_STATES, to_iso


def entry_status(
    hub: Hub,
    entry: Entry,
    now: datetime,
    disabled: set[str],
    room: dict[str, Any] | None = None,
    unavailable: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    """`room` and `unavailable`, when given, are numbers the caller already has.

    A page with one row per (account, model) would otherwise redo the same window arithmetic
    per entry and run one query per entry for the parked pairs.
    """
    if entry.key in disabled:
        return "down", "disabled"
    if not entry.key_present:
        return "down", "missing_key"
    expired = hub.quota.expired_at(entry, now)
    if expired is not None:
        return "expired", to_iso(expired)
    exhausted = hub.quota.exhausted_until(entry, now)
    if exhausted is not None:
        return "exhausted", to_iso(exhausted)
    pair = (entry.account_id, entry.key)
    parked = (
        unavailable.get(pair)
        if unavailable is not None
        else hub.store.unavailable_until(entry.account_id, entry.key, now)
    )
    if parked is not None:
        return "down", f"{parked['kind']}: {parked['code']} until {parked['until_ts']}"
    cooldown = hub.router.in_cooldown(entry, now)
    if cooldown is not None:
        return "cooldown", to_iso(cooldown)
    if not entry_has_room(hub, entry, now, room):
        return "exhausted", "window_limit"
    return "ok", None


def entry_has_room(hub: Hub, entry: Entry, now: datetime, room: dict[str, Any] | None) -> bool:
    """Room for one more call, read off an already-computed `room` when there is one.

    Both axes have to admit it: a window metered in requests can be full while its token
    counters say nothing, and a pair with no request left is exhausted just the same.
    """
    if room is None:
        return hub.quota.has_room(entry, 0, 1, now)
    for key in ("remaining_out", "remaining_requests"):
        remaining = room[key]
        if remaining is not None and remaining < 1:
            return False
    return True


EMPTY_USAGE: dict[str, Any] = {
    "requests": 0,
    "in_tokens": 0,
    "out_tokens": 0,
    "errors": 0,
    "last_used_at": None,
}

# "lately" for the live view. The field name on a model row stays `apps_15m` whatever window
# a caller asks for: it is what the column header promises, and the window is echoed by the
# endpoint next to the rows.
LIVE_WINDOW_MIN = 15


def window_since(now: datetime, window_min: int) -> str:
    return to_iso(now - timedelta(minutes=max(1, window_min)))


def in_flight_by_pair(hub: Hub) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for call in hub.router.in_flight():
        pair = (str(call["account"]), str(call["model"]))
        counts[pair] = counts.get(pair, 0) + 1
    return counts


def model_rows(
    hub: Hub, now: datetime | None = None, window_min: int = LIVE_WINDOW_MIN
) -> list[dict[str, Any]]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    disabled = hub.store.disabled_models()
    day_start = to_iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    today = hub.store.usage_since_by_model(day_start)
    recent_apps: dict[tuple[str, str], list[str]] = {}
    for row in hub.store.usage_by_app_and_model(window_since(now, window_min)):
        apps = recent_apps.setdefault((row["account"], row["model"]), [])
        if row["app"] not in apps:
            apps.append(row["app"])
    live = in_flight_by_pair(hub)
    observed_all = hub.store.observed_limits_all()
    request_caps_all = hub.store.request_caps_all()
    unsupported_params_all = hub.store.unsupported_params_all()
    unavailable_all = hub.store.unavailable_all(now)
    rows: list[dict[str, Any]] = []
    for entry in hub.registry.entries():
        pair = (entry.account_id, entry.key)
        room = hub.quota.room(entry, now)
        status, reason = entry_status(hub, entry, now, disabled, room=room, unavailable=unavailable_all)
        stats = hub.store.model_stats(entry.account_id, entry.key)
        observed = observed_all.get(pair, {})
        windows = {name: state.as_dict() for name, state in hub.quota.windows(entry, now, observed).items()}
        rows.append(
            {
                "key": entry.key,
                "provider": entry.provider_name,
                "account": entry.account_id,
                "model": entry.model.id,
                "kind": entry.kind,
                "caps": list(entry.model.caps),
                "context": entry.model.context,
                "max_request_tokens": entry.model.max_request_tokens,
                "observed_request_cap": request_caps_all.get(pair, {}).get("max_request_tokens"),
                "observed_out_cap": request_caps_all.get(pair, {}).get("max_out_tokens"),
                # what a caller needs to size max_tokens before sending: what is left in the
                # binding window now, and the widest a fresh window ever opens
                "remaining_out": room["remaining_out"],
                "remaining_in": room["remaining_in"],
                # calls left in the binding window when the vendor meters requests, not tokens
                "remaining_requests": room["remaining_requests"],
                "window_limit": hub.quota.window_limit(entry, observed),
                "resets_at": room["resets_at"],
                "unsupported_params": sorted(unsupported_params_all.get(pair, set())),
                "free": entry.model.is_free,
                "concurrency": entry.concurrency,
                "expires_at": to_iso(entry.model.expires_at()) if entry.model.expires_at() else None,
                "status": status,
                "reason": reason,
                "disabled": entry.key in disabled,
                "key_present": entry.key_present,
                "api_key_env": entry.account.api_key_env,
                "windows": windows,
                "observed": observed,
                "usage_today": dict(today.get(pair, EMPTY_USAGE)),
                "apps_15m": sorted(recent_apps.get(pair, [])),
                "in_flight": live.get(pair, 0),
                "notes": entry.model.notes,
                "last_error": stats["last_error"],
                "last_error_at": stats["last_error_at"],
                "last_ok_at": stats["last_ok_at"],
                "avg_latency_ms": stats["avg_latency_ms"],
            }
        )
    return rows


def live_rows(
    hub: Hub, window_min: int = LIVE_WINDOW_MIN, now: datetime | None = None
) -> list[dict[str, Any]]:
    """What each app is calling right now, and what it called over the window.

    In-flight comes from the router's registry (memory, cheap enough for a two-second poll);
    `recent` is one grouped query over the usage table. An account is a detail of the call,
    not of the chip, so `recent` is per model with the accounts summed.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    rows: dict[str, dict[str, Any]] = {}

    def slot(app: str) -> dict[str, Any]:
        return rows.setdefault(app, {"app": app, "in_flight": [], "recent": []})

    for call in hub.router.in_flight():
        slot(str(call["app"]))["in_flight"].append(
            {
                "model": call["model"],
                "account": call["account"],
                "kind": call["kind"],
                "elapsed_s": call["elapsed_s"],
                "attempt": call["attempt"],
                "job_id": call["job_id"],
            }
        )

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in hub.store.usage_by_app_and_model(window_since(now, window_min)):
        key = (row["app"], row["model"])
        entry = totals.setdefault(key, {"model": row["model"], "calls": 0, "out_tokens": 0})
        entry["calls"] += row["calls"]
        entry["out_tokens"] += row["out_tokens"]
    for (app, _model), entry in totals.items():
        slot(app)["recent"].append(entry)

    for row in rows.values():
        row["recent"].sort(key=lambda item: (-item["calls"], item["model"]))
    # busy apps first, then the loudest of the window, so the strip reads top down
    return sorted(
        rows.values(),
        key=lambda row: (
            -len(row["in_flight"]),
            -sum(item["calls"] for item in row["recent"]),
            row["app"],
        ),
    )


def app_shares(hub: Hub) -> dict[str, dict[str, Any]]:
    """Per-app queued/running/cap, straight from the running queue when there is one."""
    queue = getattr(hub, "jobs", None)
    shares = getattr(queue, "shares", None)
    return shares() if callable(shares) else {}


def queue_summary(hub: Hub, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    depth = hub.store.job_state_counts()
    queue = getattr(hub, "jobs", None)
    return {
        "depth_by_state": depth,
        "by_app": hub.store.job_app_counts(),
        # what is actually still moving: history is the rest of depth_by_state
        "live": sum(depth.get(state, 0) for state in LIVE_JOB_STATES),
        "oldest_queued_at": hub.store.oldest_queued_at(),
        "expired_last_24h": hub.store.jobs_expired_since(to_iso(now - timedelta(hours=24))),
        "workers": getattr(queue, "workers", hub.settings.job_workers),
        "apps": app_shares(hub),
    }


def alias_rows(hub: Hub) -> list[dict[str, Any]]:
    return [
        {
            "alias": name,
            "require": list(alias.require),
            "prefer": list(alias.prefer),
            "spread": alias.spread_for(name),
        }
        for name, alias in hub.registry.aliases.items()
    ]


def account_rows(hub: Hub) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_name, provider in hub.registry.providers.items():
        for account in provider.accounts:
            rows.append(
                {
                    "provider": provider_name,
                    "kind": provider.kind,
                    "base_url": provider.base_url,
                    "command": provider.command,
                    "account": account.id,
                    "api_key_env": account.api_key_env,
                    "key_present": (not account.api_key_env) or bool(os.environ.get(account.api_key_env)),
                    "models": [model.id for model in provider.models],
                }
            )
    return rows


def check_rows(hub: Hub, now: datetime | None = None) -> list[dict[str, Any]]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    rows: list[dict[str, Any]] = []
    for row in model_rows(hub, now):
        windows = []
        for name, state in row["windows"].items():
            limit = state["limit"]
            windows.append(f"{name} {state['used']}/{limit if limit is not None else '-'}")
        exhausted = row["reason"] if row["status"] in ("exhausted", "cooldown", "expired") else ""
        rows.append(
            {
                "key": row["key"],
                "account": row["account"],
                # a cli provider has no key at all: the login lives in the CLI's own config
                "key_present": "cli" if row["kind"] == CLI_KIND else row["key_present"],
                "status": row["status"],
                "windows": " ".join(windows) or "-",
                "exhausted_until": exhausted or "-",
            }
        )
    return rows


def check_table(hub: Hub, now: datetime | None = None) -> str:
    rows = check_rows(hub, now)
    headers = {
        "key": "MODEL",
        "account": "ACCOUNT",
        "key_present": "KEY",
        "status": "STATUS",
        "windows": "WINDOWS used/limit",
        "exhausted_until": "EXHAUSTED_UNTIL",
    }
    cells = [
        {
            name: ("yes" if value is True else "no" if value is False else str(value))
            for name, value in row.items()
        }
        for row in rows
    ]
    widths = {
        name: max(len(title), *(len(cell[name]) for cell in cells)) if cells else len(title)
        for name, title in headers.items()
    }
    lines = [
        "  ".join(title.ljust(widths[name]) for name, title in headers.items()).rstrip(),
        "  ".join("-" * widths[name] for name in headers),
    ]
    for cell in cells:
        lines.append("  ".join(cell[name].ljust(widths[name]) for name in headers).rstrip())
    if not cells:
        lines.append("(registry has no (account, model) pairs)")
    lines.append(f"{len(cells)} (account, model) pairs; registry {hub.settings.registry_path}")
    return "\n".join(lines)
