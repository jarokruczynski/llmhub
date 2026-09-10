from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..providers_catalog import known_providers
from ..runtime import Hub
from ..status import entry_status
from ..store import now_iso
from .apply import apply_decisions, build_report, write_run
from .collect import Page, collect
from .curate import curate
from .extract import extract_offers
from .llm import BudgetExhausted, HubLLM
from .sources import Source, load_sources, resolve_sources

log = logging.getLogger(__name__)

EXTRACT_ALIAS = "fast"
CURATE_ALIAS = "auto"
STRONG_ALIAS = "strong"
# used only when the registry has no `strong` alias to read a prefer order from
CURATE_PREFERENCE = (
    "explabs/gpt-6-astra",
    "explabs/claude-fable-5.1",
    "gemini/gemini-3.8-flash",
)
SCHEDULE_POLL_SECONDS = 300.0


class ScoutBusy(RuntimeError):
    """A run is already in flight; concurrency is 1."""


def local_zone() -> Any:
    return datetime.now().astimezone().tzinfo


def parse_schedule(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        hour, _, minute = value.strip().partition(":")
        parsed = (int(hour), int(minute or 0))
    except ValueError:
        log.warning("bad LLMHUB_SCOUT_AT value %r, scheduler off", value)
        return None
    if not (0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59):
        log.warning("bad LLMHUB_SCOUT_AT value %r, scheduler off", value)
        return None
    return parsed


def next_run_dt(schedule: str | None, now: datetime | None = None) -> datetime | None:
    """Next occurrence of HH:MM in the local timezone; tomorrow once today's has passed."""
    parsed = parse_schedule(schedule)
    if parsed is None:
        return None
    hour, minute = parsed
    moment = now or datetime.now(local_zone())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_zone())
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return target


def next_run_at(schedule: str | None, now: datetime | None = None) -> str | None:
    target = next_run_dt(schedule, now)
    return target.isoformat() if target else None


def curate_prefer(hub: Hub, now: datetime | None = None) -> str | None:
    """The best model, per api/status, that is not exhausted right now.

    Walks the `strong` alias's own prefer order when the registry defines one - that list is
    the source of truth for which models are capable enough to curate, and it moves with the
    registry instead of drifting out of sync with a hardcoded copy. Falls back to
    CURATE_PREFERENCE only when no `strong` alias exists.
    """
    moment = now or datetime.now(UTC)
    disabled = hub.store.disabled_models()
    alias = hub.registry.aliases.get(STRONG_ALIAS)
    keys = tuple(alias.prefer) if alias is not None and alias.prefer else CURATE_PREFERENCE
    for key in keys:
        for entry in hub.registry.entries():
            if entry.key != key:
                continue
            status, _ = entry_status(hub, entry, moment, disabled)
            if status == "ok":
                return key
    return None


def run_row(row: dict[str, Any]) -> dict[str, Any]:
    """The db row as the API and the CLI report it."""
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "sources": int(row["sources"] or 0),
        "pages_fetched": int(row["pages_fetched"] or 0),
        "pages_changed": int(row["pages_changed"] or 0),
        "offers": int(row["offers"] or 0),
        "new": int(row["new_count"] or 0),
        "updated": int(row["updated_count"] or 0),
        "skipped": int(row["skipped_count"] or 0),
        "models_used": _loads(row["models_used"], {"extract": [], "curate": []}),
        "tokens": _loads(row["tokens"], {}),
        "errors": len(_loads(row["errors"], [])),
        "dry_run": bool(row["dry_run"]),
        "invalid_reasons": _loads(row.get("invalid_reasons"), []),
    }


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class ScoutService:
    """Owns the run lock, the schedule and the pipeline."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.active_run_id: int | None = None
        self._task: asyncio.Task[Any] | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def active(self) -> bool:
        return self.active_run_id is not None

    @property
    def schedule(self) -> str | None:
        return self.hub.settings.scout_at

    def next_run_at(self, now: datetime | None = None) -> str | None:
        return next_run_at(self.schedule, now)

    def reset_orphans(self) -> None:
        """A run that died with the process is not still running."""
        self.hub.store.execute(
            "UPDATE scout_runs SET status = 'failed', finished_at = ? WHERE status = 'running'",
            (now_iso(),),
        )

    async def start(self) -> None:
        self.reset_orphans()
        self._stopping.clear()
        if self.schedule and self._scheduler is None:
            self._scheduler = asyncio.create_task(self._schedule_loop(), name="llmhub-scout-schedule")
            log.info("scout scheduled daily at %s (next %s)", self.schedule, self.next_run_at())

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._scheduler, self._task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._scheduler = None
        self._task = None

    async def _schedule_loop(self) -> None:
        while not self._stopping.is_set():
            target = next_run_dt(self.schedule)
            if target is None:
                return
            delay = max(1.0, (target - datetime.now(local_zone())).total_seconds())
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=min(delay, SCHEDULE_POLL_SECONDS))
                return
            except TimeoutError:
                pass
            if datetime.now(local_zone()) >= target and not self.active:
                try:
                    await self.run()
                except Exception as exc:  # noqa: BLE001 - a bad run must not kill the schedule
                    log.exception("scheduled scout run failed: %s", exc)

    def start_run(self, dry_run: bool = False) -> int:
        """Create the run row and hand it to a background task. Raises ScoutBusy on the second."""
        if self.active:
            raise ScoutBusy(f"scout run {self.active_run_id} is already active")
        run_id = self.hub.store.create_scout_run(dry_run=dry_run)
        self.active_run_id = run_id
        self._task = asyncio.create_task(self._background(run_id, dry_run), name=f"llmhub-scout-{run_id}")
        return run_id

    async def _background(self, run_id: int, dry_run: bool) -> None:
        try:
            await self.run(dry_run=dry_run, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("scout run %s failed: %s", run_id, exc)

    async def run(self, dry_run: bool = False, run_id: int | None = None) -> dict[str, Any]:
        if run_id is None:
            if self.active:
                raise ScoutBusy(f"scout run {self.active_run_id} is already active")
            run_id = self.hub.store.create_scout_run(dry_run=dry_run)
        self.active_run_id = run_id
        try:
            return await self._pipeline(run_id, dry_run)
        finally:
            self.active_run_id = None

    async def _pipeline(self, run_id: int, dry_run: bool) -> dict[str, Any]:
        hub = self.hub
        store = hub.store
        started_at = (store.scout_run(run_id) or {}).get("started_at") or now_iso()
        errors: list[str] = []
        counts: dict[str, int] = {}
        models_used: dict[str, list[str]] = {"extract": [], "curate": []}
        tokens: dict[str, dict[str, int]] = {"extract": {"in": 0, "out": 0}, "curate": {"in": 0, "out": 0}}
        decisions: list[dict[str, Any]] = []
        invalid_reasons: list[str] = []

        try:
            config = load_sources(hub.settings.scout_sources)
            sources = resolve_sources(config)
        except Exception as exc:  # noqa: BLE001
            report = f"# scout run {run_id} - started {started_at}\n- source list unreadable: {exc}"
            write_run(
                store,
                run_id,
                counts={},
                models_used=models_used,
                tokens=tokens,
                errors=[str(exc)],
                decisions=[],
                report_md=report,
                status="failed",
            )
            return {**run_row(store.scout_run(run_id) or {}), "report_md": report, "decisions": []}

        collected = await collect(store, hub.client, sources, results=config.search.results)
        errors.extend(collected.errors)
        counts.update(
            sources=collected.sources,
            pages_fetched=collected.fetched,
            pages_changed=len(collected.changed),
        )

        llm = HubLLM(
            hub.client,
            hub.settings.hub_base_url,
            app="scout",
            token=hub.settings.token,
        )
        extracted = await extract_offers(llm, collected.changed, model=EXTRACT_ALIAS)
        errors.extend(extracted.errors)
        models_used["extract"] = extracted.usage.models
        tokens["extract"] = extracted.usage.as_tokens()
        counts.update(offers=len(extracted.offers), offers_dropped=len(extracted.dropped))

        async def fetch_extra(urls: list[str]) -> list[Page]:
            extra = [Source(id=f"needs-{index}", kind="page", url=url) for index, url in enumerate(urls)]
            outcome = await collect(store, hub.client, extra)
            errors.extend(outcome.errors)
            return [page for page in outcome.pages if page.text]

        curated = None
        if extracted.offers and extracted.stopped is None:
            try:
                curated = await curate(
                    llm,
                    offers=extracted.offers,
                    promos=store.promos(),
                    rejections=store.promo_rejections(),
                    template_ids=[item["id"] for item in known_providers()],
                    model=CURATE_ALIAS,
                    prefer=curate_prefer(hub),
                    fetch=fetch_extra,
                )
            except BudgetExhausted as exc:
                errors.append(f"curate stopped: {exc}")
        if curated is not None:
            errors.extend(curated.errors)
            models_used["curate"] = curated.usage.models
            tokens["curate"] = curated.usage.as_tokens()
            counts["decisions_dropped"] = len(curated.dropped)
            decisions = curated.decisions
            invalid_reasons = curated.dropped

        applied = apply_decisions(store, decisions, dry_run=dry_run, registry=hub.registry)
        errors.extend(applied.errors)
        counts.update(
            new=applied.new,
            updated=applied.updated,
            skipped=applied.skipped,
            skipped_rejected=applied.skipped_rejected,
        )

        report = build_report(
            run_id=run_id,
            started_at=started_at,
            counts=counts,
            models_used=models_used,
            tokens=tokens,
            errors=errors,
            decisions=applied.decisions,
            invalid_reasons=invalid_reasons,
            dry_run=dry_run,
        )
        write_run(
            store,
            run_id,
            counts=counts,
            models_used=models_used,
            tokens=tokens,
            errors=errors,
            decisions=applied.decisions,
            invalid_reasons=invalid_reasons,
            report_md=report,
        )
        row = store.scout_run(run_id) or {}
        return {**run_row(row), "report_md": report, "decisions": applied.decisions, "error_details": errors}


async def run(hub: Hub, dry_run: bool = False) -> dict[str, Any]:
    """One full run on a hub that has no service attached (the CLI path)."""
    service = getattr(hub, "scout", None) or ScoutService(hub)
    return await service.run(dry_run=dry_run)
