"""Keeps the registry's model list in step with what each vendor actually serves.

A vendor adds models and withdraws others without telling anyone. A withdrawn model stays in
providers.yaml and fails as not_found on every call that reaches it, and a new one is never
routed to because nobody registered it. So on a schedule, every provider with a listing route
(`GET /models` or the template's own endpoint for HTTP kinds, `<command> models` for a CLI whose
dialect has one, each login under its own HOME) is asked what it serves, and the answer is
diffed against the registry.

What the diff does, and what it never does:
- a model the vendor lists and the registry lacks is registered only where the listing itself
  proves it is free: a CLI login lists what its plan serves, an id carries a `:free` / `-free`
  marker, or the catalog template already vetted it as free. It
  gets `free: {}` (free, limits unknown) and caps guessed from the vendor's own name for it.
  Anything else - most of an aggregator's catalogue is paid, and a local server's models are
  sized against the machine's memory by hand - is recorded as offered and reported, not
  written to the yaml: the owner registers what fits.
- a registered model the vendor stopped listing gets a disabled override, not a deletion. If it
  comes back, discovery lifts only the override it wrote itself.
- a registered model's `free` windows are never touched, and an override the owner set is never
  lifted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .accounts import (
    AccountError,
    RegistryWriter,
    _discover_fields_of,
    _fetch_listing,
    _template_id_of,
    backup_registry_file,
    caps_for_model_id,
    parse_model_rows,
)
from .cli_backend import CliRequestError, discover_cli_models
from .config import CLI_KIND
from .providers_catalog import discover_spec, is_chat_model_id, normalize_model_id, template
from .runtime import Hub
from .store import parse_iso, to_iso

log = logging.getLogger(__name__)

DISCOVERY_POLL_SECONDS = 300.0
# a cold `agy models` takes a few seconds; one provider must not hold the whole pass
PROVIDER_TIMEOUT_S = 120.0
LISTING_TIMEOUT_S = 10.0
# the start-up pass waits this long so it does not compete with the boot probes
START_DELAY_S = 60.0
# how long a model discovery registered is reported as new
NEW_MODEL_DAYS = 7
# a listing that drops most of the registry at once is more likely a truncated or wrong answer
# than a vendor withdrawing everything: report it, do not act on it
SUSPECT_SHARE = 0.8
SUSPECT_MIN = 5
FREE_ID_SUFFIXES = (":free", "-free")
# offered ids shown by name in one event; the rest are counted
EVENT_NAMES_MAX = 40

REASON_VANISHED = "vanished"


@dataclass
class Listing:
    status: str
    # every id the vendor named, raw and normalized: a registered model counts as present when
    # the vendor lists it at all, whether or not the chat filter would offer it as new
    listed: set[str] = field(default_factory=set)
    # chat model rows that may be registered: [{"id": ..., "context": ...}]
    offered: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def diff_listing(registered: list[str], listing: Listing) -> dict[str, Any]:
    """New, vanished and still-present ids for one provider. Pure: no store, no yaml."""
    if listing.status != "ok":
        return {"new": [], "vanished": [], "present": [], "suspect": False}
    known = set(registered)
    new = [row for row in listing.offered if row["id"] not in known]
    vanished = [model_id for model_id in registered if model_id not in listing.listed]
    present = [model_id for model_id in registered if model_id in listing.listed]
    suspect = len(vanished) >= SUSPECT_MIN and len(vanished) > SUSPECT_SHARE * len(registered)
    return {"new": new, "vanished": vanished, "present": present, "suspect": suspect}


def catalog_model(template_id: str | None, model_id: str) -> dict[str, Any] | None:
    found = template(template_id) if template_id else None
    for row in (found or {}).get("models") or []:
        if isinstance(row, dict) and row.get("id") == model_id:
            return row
    return None


def new_model_spec(
    kind: str, template_id: str | None, row: dict[str, Any], today: str
) -> dict[str, Any] | None:
    """The yaml entry for a model discovery found, or None when nothing proves it is free.

    A catalog row the template ships wins: someone already measured its caps and free status.
    Otherwise vision is claimed only when the vendor's own id says so, and a CLI model gets text
    alone - its listing names the model and nothing else.
    """
    model_id = str(row["id"])
    vetted = catalog_model(template_id, model_id)
    if vetted is not None:
        if vetted.get("free") is None:
            return None
        spec = {
            "id": model_id,
            "caps": list(vetted.get("caps") or ["text"]),
            "free": vetted["free"],
            "notes": f"discovered {today}; {vetted.get('notes') or 'from the catalog template'}",
        }
    elif kind == CLI_KIND or model_id.lower().endswith(FREE_ID_SUFFIXES):
        spec = {
            "id": model_id,
            "caps": ["text"] if kind == CLI_KIND else caps_for_model_id(model_id),
            "free": {},
            "notes": f"discovered {today}; free tier unknown",
        }
    else:
        return None
    context = row.get("context")
    if isinstance(context, int) and context > 0:
        spec["context"] = context
    return spec


async def _list_http(hub: Hub, name: str, block: Any) -> Listing:
    accounts = [item for item in block.accounts if not item.api_key_env or _env(item.api_key_env)]
    if not accounts:
        return Listing("no_account_key")
    template_id = _template_id_of(block, name)
    spec = discover_spec(template_id)
    fields = _discover_fields_of(block, spec)
    merged = Listing("ok")
    seen: set[str] = set()
    errors: list[str] = []
    for account in accounts:
        api_key = _env(account.api_key_env) if account.api_key_env else None
        try:
            payload, id_field, response = await _fetch_listing(
                hub.client,
                base_url=block.base_url,
                api_key=api_key,
                headers=dict(block.headers),
                timeout=LISTING_TIMEOUT_S,
                spec=spec,
                fields=fields,
            )
        except (httpx.HTTPError, AccountError) as exc:
            errors.append(f"{account.id}: {exc}")
            continue
        if response.status_code >= 400 or payload is None:
            errors.append(f"{account.id}: http_{response.status_code}")
            continue
        for row in parse_model_rows(payload, id_field):
            raw = str(row["id"])
            normalized = normalize_model_id(raw, template_id)
            merged.listed.update({raw, normalized})
            if normalized and is_chat_model_id(normalized, template_id) and normalized not in seen:
                seen.add(normalized)
                merged.offered.append({"id": normalized, "context": row.get("context")})
    if not merged.listed:
        return Listing("error" if errors else "empty", error="; ".join(errors)[:500] or None)
    return merged


async def _list_cli(name: str, block: Any) -> Listing:
    try:
        ids, run = await discover_cli_models(name, block)
    except CliRequestError as exc:
        return Listing("no_listing", error=str(exc)[:300])
    if run.returncode != 0:
        return Listing("error", error=(run.stderr or run.stdout or f"exit {run.returncode}")[:500])
    if not ids:
        return Listing("empty")
    return Listing("ok", listed=set(ids), offered=[{"id": model_id, "context": None} for model_id in ids])


def _env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


async def list_provider(hub: Hub, name: str, block: Any) -> Listing:
    if not block.accounts:
        return Listing("no_account")
    try:
        if block.kind == CLI_KIND:
            return await asyncio.wait_for(_list_cli(name, block), timeout=PROVIDER_TIMEOUT_S)
        return await asyncio.wait_for(_list_http(hub, name, block), timeout=PROVIDER_TIMEOUT_S)
    except TimeoutError:
        return Listing("error", error=f"no listing inside {int(PROVIDER_TIMEOUT_S)}s")


async def plan_pass(hub: Hub, *, provider_filter: str | None = None) -> list[dict[str, Any]]:
    """Ask every provider what it serves and diff it against the registry. Writes nothing."""
    today = datetime.now(UTC).date().isoformat()
    listing_rows = hub.store.model_listing_all()
    reports: list[dict[str, Any]] = []
    for name, block in hub.registry.providers.items():
        if provider_filter and name != provider_filter:
            continue
        listing = await list_provider(hub, name, block)
        registered = [model.id for model in block.models]
        diff = diff_listing(registered, listing)
        template_id = _template_id_of(block, name)
        new: list[dict[str, Any]] = []
        offered: list[str] = []
        for row in diff["new"]:
            spec = new_model_spec(block.kind, template_id, row, today)
            if spec is None:
                offered.append(str(row["id"]))
            else:
                new.append({"id": row["id"], "spec": spec})
        returned = [
            model_id
            for model_id in diff["present"]
            if (listing_rows.get(f"{name}/{model_id}") or {}).get("vanished_at")
        ]
        reports.append(
            {
                "provider": name,
                "kind": block.kind,
                "status": listing.status,
                "error": listing.error,
                "listed": len(listing.offered),
                "registered": len(registered),
                "present": diff["present"],
                "new": new,
                "offered": offered,
                "vanished": diff["vanished"],
                "returned": returned,
                "suspect": diff["suspect"],
            }
        )
    return reports


def _names(ids: list[str]) -> str:
    shown = ", ".join(ids[:EVENT_NAMES_MAX])
    return shown + (f" (+{len(ids) - EVENT_NAMES_MAX} more)" if len(ids) > EVENT_NAMES_MAX else "")


def _record_present(hub: Hub, name: str, report: dict[str, Any], rows: dict[str, Any], moment: str) -> None:
    store = hub.store
    for model_id in report["present"]:
        key = f"{name}/{model_id}"
        row = rows.get(key) or {}
        fields: dict[str, Any] = {
            "last_seen": moment,
            "first_seen": row.get("first_seen") or moment,
            "offered": 0,
        }
        if row.get("vanished_at"):
            fields["vanished_at"] = None
            if row.get("disabled_reason") == REASON_VANISHED:
                store.set_model_disabled(key, False, None)
                fields["disabled_reason"] = None
        store.upsert_model_listing(key, name, **fields)
    if report["returned"]:
        store.add_event(
            kind="discovery",
            message=f"{name}: listed again, re-enabled where discovery had disabled it: "
            + _names(report["returned"]),
        )


def _record_offered(hub: Hub, name: str, report: dict[str, Any], rows: dict[str, Any], moment: str) -> None:
    """Offered-but-unregistered ids get a row so the next pass can tell which ones are new."""
    store = hub.store
    fresh: list[str] = []
    still = set(report["offered"])
    for model_id in report["offered"]:
        key = f"{name}/{model_id}"
        row = rows.get(key)
        if row is None:
            fresh.append(model_id)
        store.upsert_model_listing(
            key, name, first_seen=(row or {}).get("first_seen") or moment, last_seen=moment, offered=1
        )
    keep = still | set(report["present"]) | {item["id"] for item in report["new"]}
    for key, row in rows.items():
        if row["provider"] == name and row.get("offered") and key.split("/", 1)[1] not in keep:
            store.delete_model_listing(key)
    if fresh:
        store.add_event(
            kind="discovery",
            message=f"{name} lists {len(fresh)} model(s) with no proven free tier, not registered: "
            + _names(fresh),
        )


def _record_vanished(hub: Hub, name: str, report: dict[str, Any], rows: dict[str, Any], moment: str) -> None:
    store = hub.store
    if report["suspect"]:
        store.add_event(
            kind="discovery",
            message=f"{name}: listing drops {len(report['vanished'])} of {report['registered']} models, "
            "left as they are",
        )
        return
    disabled = store.disabled_models()
    gone: list[str] = []
    for model_id in report["vanished"]:
        key = f"{name}/{model_id}"
        if (rows.get(key) or {}).get("vanished_at"):
            continue
        fields: dict[str, Any] = {"vanished_at": moment}
        if key not in disabled:
            store.set_model_disabled(key, True, "gone from the vendor's model listing")
            fields["disabled_reason"] = REASON_VANISHED
        store.upsert_model_listing(key, name, **fields)
        gone.append(model_id)
    if gone:
        store.add_event(
            kind="discovery", message=f"{name}: no longer listed, marked unavailable: " + _names(gone)
        )


def apply_pass(hub: Hub, reports: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Write what `plan_pass` found: yaml for new free models, store rows and overrides for the rest."""
    moment = to_iso((now or datetime.now(UTC)).astimezone(UTC))
    store = hub.store
    rows = store.model_listing_all()
    backup = None
    added_total = 0
    for report in reports:
        name = report["provider"]
        if report["status"] != "ok":
            continue
        _record_present(hub, name, report, rows, moment)
        _record_offered(hub, name, report, rows, moment)
        if report["new"]:
            if backup is None:
                backup = backup_registry_file(hub.settings.registry_path, hub.settings.home / "backups")
            account_id = hub.registry.providers[name].accounts[0].id
            RegistryWriter(hub.settings).add_models(
                provider=name, account_id=account_id, models=[item["spec"] for item in report["new"]]
            )
            added_total += len(report["new"])
            for item in report["new"]:
                store.upsert_model_listing(
                    f"{name}/{item['id']}",
                    name,
                    first_seen=moment,
                    last_seen=moment,
                    added_at=moment,
                    vanished_at=None,
                    disabled_reason=None,
                    offered=0,
                )
            store.add_event(
                kind="discovery",
                message=f"new model available on {name}: " + _names([item["id"] for item in report["new"]]),
            )
        if report["vanished"]:
            _record_vanished(hub, name, report, rows, moment)
    store.record_discovery_run(ts=moment, report=public_report(reports))
    if added_total:
        hub.reload()
        service = getattr(hub, "health", None)
        if service is not None and hasattr(service, "request_verify"):
            service.request_verify()
    return {"ts": moment, "added": added_total, "backup": str(backup) if backup else None}


def public_report(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The report without the yaml specs and the long present list, for storage and the API."""
    return [
        {
            **{name: value for name, value in report.items() if name not in ("present", "new")},
            "present": len(report["present"]),
            "new": [item["id"] for item in report["new"]],
        }
        for report in reports
    ]


async def run_pass(hub: Hub, *, dry_run: bool = False, provider_filter: str | None = None) -> dict[str, Any]:
    reports = await plan_pass(hub, provider_filter=provider_filter)
    if dry_run:
        return {"dry_run": True, "reports": public_report(reports)}
    applied = apply_pass(hub, reports)
    return {"dry_run": False, **applied, "reports": public_report(reports)}


def discovery_summary(hub: Hub, now: datetime | None = None) -> dict[str, Any]:
    """What the status page shows: models new this week, models gone, offered ones, the schedule."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    since = to_iso(moment - timedelta(days=NEW_MODEL_DAYS))
    registered = set(hub.registry.keys())
    disabled = hub.store.disabled_models()
    new_models: list[dict[str, Any]] = []
    vanished: list[dict[str, Any]] = []
    offered: list[dict[str, Any]] = []
    for key, row in sorted(hub.store.model_listing_all().items()):
        if key not in registered:
            if row.get("offered") and str(row.get("first_seen") or "") >= since:
                offered.append({"key": key, "first_seen": row["first_seen"]})
            continue
        if row.get("vanished_at"):
            vanished.append({"key": key, "vanished_at": row["vanished_at"]})
        elif row.get("added_at") and str(row["added_at"]) >= since:
            new_models.append({"key": key, "added_at": row["added_at"], "enabled": key not in disabled})
    last = hub.store.last_discovery_run()
    service = getattr(hub, "discovery", None)
    return {
        "last_run_at": last["ts"] if last else None,
        "next_run_at": service.next_run_at(moment) if service is not None else None,
        "new_models": new_models,
        "vanished": vanished,
        "offered_count": len(offered),
        "offered": offered[:200],
    }


class DiscoveryService:
    """Runs a discovery pass every `discovery_every_h` hours and once shortly after start."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def every_h(self) -> int:
        return int(self.hub.settings.discovery_every_h)

    def next_run_dt(self, now: datetime | None = None) -> datetime | None:
        if self.every_h <= 0:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        last = self.hub.store.last_discovery_run()
        if not last:
            return moment
        return parse_iso(str(last["ts"])) + timedelta(hours=self.every_h)

    def next_run_at(self, now: datetime | None = None) -> str | None:
        target = self.next_run_dt(now)
        return to_iso(target) if target else None

    async def run(self, *, dry_run: bool = False, provider_filter: str | None = None) -> dict[str, Any]:
        async with self._lock:
            return await run_pass(self.hub, dry_run=dry_run, provider_filter=provider_filter)

    async def start(self) -> None:
        self._stopping.clear()
        if self.every_h > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="llmhub-model-discovery")
            log.info("model discovery every %sh", self.every_h)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("model discovery ended with an error")
        self._task = None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _once(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # a pass that dies must not take the schedule with it
            log.exception("model discovery pass failed")
            self.hub.store.record_discovery_run(ts=to_iso(datetime.now(UTC)), report=[])

    async def _loop(self) -> None:
        await self._sleep(START_DELAY_S)
        if not self._stopping.is_set():
            await self._once()
        while not self._stopping.is_set():
            target = self.next_run_dt()
            if target is None:
                return
            delay = (target - datetime.now(UTC)).total_seconds()
            if delay > 0:
                await self._sleep(min(delay, DISCOVERY_POLL_SECONDS))
                continue
            await self._once()
