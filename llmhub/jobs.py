from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import require_token
from .config import Entry
from .gateway import CallResult, execute_chat
from .quota import estimate_request_cost
from .router import (
    CANCEL_BY_OWNER,
    AllCandidatesFailed,
    NoCandidatesError,
    UnknownModelError,
    UpstreamError,
    quota_room,
)
from .runtime import Hub
from .store import LIVE_JOB_STATES, TERMINAL_JOB_STATES, now_iso, parse_iso, to_iso

log = logging.getLogger(__name__)

router = APIRouter()

TERMINAL_STATES = TERMINAL_JOB_STATES
WAITING_STATES = ("queued", "waiting_quota")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# A model with no window data never reports a reset that would wake the job up, so a 429 on
# it is answered with a backoff of our own: 1, 5, 15, 30 minutes, then every 30.
RETRY_BACKOFF_S: tuple[int, ...] = (60, 300, 900, 1800)
# how many lost leases a job survives before it is failed instead of requeued again
LEASE_MAX_ATTEMPTS = 3
# the deadline/lease sweep is a per-minute job, the purge a per-ten-minutes one; neither
# belongs in every two-second poll
MAINTAIN_INTERVAL_S = 60.0
PURGE_INTERVAL_S = 600.0
DEFAULT_REQUEST_TIMEOUT_S = 600.0


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobRequest(BaseModel):
    app: str
    model: str | None = None
    alias: str | None = None
    require: list[str] = Field(default_factory=list)
    priority: int = 5
    request: dict[str, Any]
    callback_url: str | None = None
    ttl_s: int | None = None


def requested_out(body: dict[str, Any]) -> int | None:
    """The output budget the caller actually asked for, or None when it said nothing.

    Only an explicit number feeds the feasibility check: rejecting a request over an assumed
    default would refuse work the vendor might well have served.
    """
    value = body.get("max_tokens") or body.get("max_completion_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def free_candidates(hub: Hub, model_request: str) -> list[Entry]:
    """Entries a job on this model could ever land on, by the same rules the router uses."""
    try:
        pool, _, alias_prefer, alias = hub.router.resolve_pool(model_request)
    except UnknownModelError:
        return []
    named = set(alias_prefer)
    disabled = hub.store.disabled_models()
    return [
        entry
        for entry in pool
        if entry.model.is_free
        and entry.key_present
        and entry.key not in disabled
        and not (alias is not None and entry.opt_in_only and entry.key not in named)
    ]


def pool_window_limit(hub: Hub, model_request: str) -> int | None:
    """Widest window any candidate for this request could ever open with, or None if unknown.

    One candidate with neither a declared nor an observed limit makes the answer unknown: it
    may serve any size, and an unknown limit must never reject a job.
    """
    best: int | None = None
    for entry in free_candidates(hub, model_request):
        limit = hub.quota.window_limit(entry)
        if limit is None:
            return None
        best = limit if best is None else max(best, limit)
    return best


def pool_is_windowless(hub: Hub, model_request: str) -> bool:
    """True when no candidate declares a window and none has ever been observed.

    Such a model never reports a reset, so a job parked on `next_window_at` would be waiting
    for an event that is not coming. It gets a backoff clock of its own instead.
    """
    candidates = free_candidates(hub, model_request)
    return bool(candidates) and all(not hub.quota.specs(entry) for entry in candidates)


def request_timeout_s(hub: Hub) -> float:
    read = getattr(getattr(hub.client, "timeout", None), "read", None)
    return float(read) if read else DEFAULT_REQUEST_TIMEOUT_S


def deadline_of(job: dict[str, Any]) -> datetime:
    """When this job gives up. Rows written before deadlines existed carry no `expires_at`."""
    stamp = job.get("expires_at")
    if stamp:
        return parse_iso(str(stamp))
    ttl = int(job.get("ttl_s") or 0) or 6 * 3600
    return parse_iso(str(job["created_at"])) + timedelta(seconds=ttl)


def backoff_at(now: datetime, attempts: int) -> datetime:
    index = min(max(attempts, 1) - 1, len(RETRY_BACKOFF_S) - 1)
    return now + timedelta(seconds=RETRY_BACKOFF_S[index])


class JobQueue:
    def __init__(
        self,
        hub: Hub,
        poll_interval: float = 2.0,
        workers: int | None = None,
        jobs_per_app: int | None = None,
        jobs_per_app_min: int | None = None,
    ) -> None:
        settings = hub.settings
        self.hub = hub
        self.poll_interval = poll_interval
        self.workers = max(1, workers if workers is not None else settings.job_workers)
        # hard ceiling, 0 = none; the everyday limit is the fair share computed per pass
        self.jobs_per_app = max(0, jobs_per_app if jobs_per_app is not None else settings.jobs_per_app)
        self.jobs_per_app_min = max(
            1, jobs_per_app_min if jobs_per_app_min is not None else settings.jobs_per_app_min
        )
        self.lease_s = request_timeout_s(hub) + settings.job_lease_margin_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._inflight: dict[str, asyncio.Task[str]] = {}
        # jobs whose worker was cancelled on request, so `_released` can tell an owner's
        # cancellation from a worker that simply died
        self._cancelling: set[str] = set()
        self._inflight_apps: dict[str, list[str]] = {}
        self._last_dispatch: dict[str, datetime] = {}
        self._share_apps: frozenset[str] = frozenset()
        self._last_maintenance: datetime | None = None
        self._last_purge: datetime | None = None

    async def start(self) -> None:
        if self._task is None:
            self.requeue_orphans()
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="llmhub-jobs")

    def requeue_orphans(self) -> None:
        """Nothing survives a restart in `running`: this process holds no lease on those rows."""
        self.hub.store.execute("UPDATE jobs SET state = 'queued', lease_until = NULL WHERE state = 'running'")

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    async def stop(self) -> None:
        self._stopping.set()
        for task in list(self._inflight.values()):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        self._inflight.clear()
        self._inflight_apps.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("job worker error: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    def app_inflight(self, app: str) -> int:
        return len(self._inflight_apps.get(app, ()))

    # --- one scheduling pass ---------------------------------------------

    async def tick(self) -> None:
        """Housekeeping first, then dispatch: both halves see the same clock.

        Housekeeping keeps the queue honest (deadlines, leases, feasibility, purge); dispatch
        shares the workers out. Same pass, so a slot freed by a lost lease is filled by the
        pass that freed it. Dispatch runs on every poll because a free worker should not wait;
        housekeeping runs on the minute, because rereading every waiting job that often costs
        more than it finds.
        """
        now = utcnow()
        if (
            self._last_maintenance is None
            or (now - self._last_maintenance).total_seconds() >= MAINTAIN_INTERVAL_S
        ):
            self._last_maintenance = now
            self.maintain(now)
        self.dispatch_due(now)

    # --- housekeeping ----------------------------------------------------

    def maintain(self, now: datetime) -> None:
        self.renew_leases(now)
        self.reclaim_lost_leases(now)
        self.expire_stale_jobs(now)
        self.purge_old_jobs(now)

    def renew_leases(self, now: datetime) -> None:
        """A job this process is still running holds its slot; the lease says so out loud."""
        until = to_iso(now + timedelta(seconds=self.lease_s))
        for job_id in list(self._inflight):
            self.hub.store.update_job(job_id, lease_until=until)

    def reclaim_lost_leases(self, now: datetime) -> None:
        """No row stays `running` without a worker behind it."""
        store = self.hub.store
        for job in store.jobs_in_states(("running",)):
            job_id = str(job["id"])
            if job_id in self._inflight:
                continue
            lease = job.get("lease_until")
            if lease and parse_iso(str(lease)) > now:
                continue
            attempts = int(job.get("lease_attempts") or 0) + 1
            if attempts >= LEASE_MAX_ATTEMPTS:
                store.update_job(
                    job_id,
                    state="failed",
                    finished_at=to_iso(now),
                    lease_until=None,
                    lease_attempts=attempts,
                    error=json.dumps(
                        {
                            "code": "lease_lost",
                            "message": f"no worker renewed the lease, {attempts} attempts",
                            "attempts": attempts,
                        }
                    ),
                )
                store.add_event(
                    kind="job",
                    message=f"job {job_id} failed: lease_lost after {attempts} attempts",
                    app=str(job["app"]),
                    model=job.get("model"),
                )
                continue
            store.update_job(
                job_id,
                state="queued",
                lease_until=None,
                started_at=None,
                lease_attempts=attempts,
                attempts=int(job.get("attempts") or 0) + 1,
            )
            store.add_event(
                kind="job",
                message=f"job {job_id} lease lost, requeued (attempt {attempts})",
                app=str(job["app"]),
                model=job.get("model"),
            )

    def expire_stale_jobs(self, now: datetime) -> None:
        """Two ways out of the queue without a worker: past the deadline, or never feasible."""
        limits: dict[str, int | None] = {}
        for job in self.hub.store.jobs_in_states(WAITING_STATES):
            try:
                body = json.loads(job["request"])
            except ValueError:
                body = {}
            model_request = str(job["model"] or "auto")
            if model_request not in limits:
                limits[model_request] = pool_window_limit(self.hub, model_request)
            limit = limits[model_request]
            want = requested_out(body)
            if limit is not None and want is not None and want > limit:
                self.expire(job, "window_too_small", now, window_limit=limit, requested=want)
                continue
            if deadline_of(job) <= now:
                self.expire(job, self.expiry_reason(job, body), now, window_limit=limit)

    def expiry_reason(self, job: dict[str, Any], body: dict[str, Any]) -> str:
        """Why this job never ran, in terms the client can act on."""
        model_request = str(job["model"] or "auto")
        est_in, est_out = estimate_request_cost(body)
        try:
            require = json.loads(job.get("require") or "[]")
        except ValueError:
            require = []
        try:
            selection = self.hub.router.select(
                model_request=model_request,
                require=require,
                est_in=est_in,
                est_out=est_out,
                # same pool the job would have run against, bans included
                app=str(job["app"]),
            )
        except UnknownModelError:
            return "provider_down"
        if selection.candidates:
            # a candidate has room right now, so the queue simply never got to this job
            return "no_worker"
        reasons = selection.rejected_reasons()
        if reasons == {"too_large"}:
            return "window_too_small"
        if reasons & {"quota", "exhausted", "expired"}:
            return "no_window"
        return "provider_down"

    def expire(
        self,
        job: dict[str, Any],
        reason: str,
        now: datetime,
        window_limit: int | None = None,
        requested: int | None = None,
    ) -> None:
        job_id = str(job["id"])
        error: dict[str, Any] = {
            "code": "expired",
            "reason": reason,
            "message": f"job left the queue without running: {reason}",
        }
        if window_limit is not None:
            error["window_limit"] = window_limit
        if requested is not None:
            error["requested_out"] = requested
        self.hub.store.update_job(
            job_id,
            state="expired",
            finished_at=to_iso(now),
            lease_until=None,
            next_attempt_at=None,
            error=json.dumps(error),
        )
        self.hub.store.add_event(
            kind="job",
            message=f"job {job_id} expired: {reason}",
            app=str(job["app"]),
            model=job.get("model"),
        )

    def purge_old_jobs(self, now: datetime) -> None:
        if self._last_purge is not None and (now - self._last_purge).total_seconds() < PURGE_INTERVAL_S:
            return
        self._last_purge = now
        before = to_iso(now - timedelta(days=self.hub.settings.job_retention_days))
        removed = self.hub.store.purge_terminal_jobs(before)
        if removed:
            self.hub.store.add_event(
                kind="queue", message=f"purged {removed} finished job row(s) older than {before}"
            )

    # --- fair share dispatch ---------------------------------------------

    def cap(self, active_apps: int) -> int:
        """Workers one app may hold: an equal share of the pool, floor 1, ceiling optional."""
        share = max(self.jobs_per_app_min, self.workers // max(1, active_apps))
        if self.jobs_per_app > 0:
            share = min(share, self.jobs_per_app)
        return max(1, share)

    def dispatch_due(self, now: datetime) -> None:
        """Give every free worker to the app that holds the fewest of them.

        Nothing running is preempted. Among apps with runnable work the one with the least
        running wins, ties go to the app that waited longest since its last dispatch, and
        inside an app the claim order (priority, then age) decides - so an app's own jobs
        still start FIFO.
        """
        queues: dict[str, list[dict[str, Any]]] = {}
        for job in self.hub.store.claimable_jobs(to_iso(now)):
            queues.setdefault(str(job["app"]), []).append(job)
        order = {app: index for index, app in enumerate(queues)}
        active = set(queues) | set(self._inflight_apps)
        self.note_share(active, now)
        cap = self.cap(len(active))
        while len(self._inflight) < self.workers:
            if self._stopping.is_set():
                return
            ready = [app for app, jobs in queues.items() if jobs and self.app_inflight(app) < cap]
            if not ready:
                return
            app = min(
                ready,
                key=lambda name: (
                    self.app_inflight(name),
                    self._last_dispatch.get(name, EPOCH),
                    order.get(name, 0),
                ),
            )
            self.dispatch(queues[app].pop(0), now)

    def note_share(self, active: set[str], now: datetime) -> None:
        """One event per change of the active set, not one per dispatch."""
        current = frozenset(active)
        if current == self._share_apps:
            return
        previous, self._share_apps = self._share_apps, current
        if not current and not previous:
            return
        names = ", ".join(sorted(current)) or "none"
        self.hub.store.add_event(
            kind="queue",
            message=f"fair share: {len(current)} app(s) with runnable work ({names}), "
            f"cap {self.cap(len(current))} of {self.workers} workers",
        )

    def dispatch(self, job: dict[str, Any], now: datetime | None = None) -> asyncio.Task[str]:
        now = now or utcnow()
        job_id = str(job["id"])
        app = str(job["app"])
        self.hub.store.update_job(
            job_id,
            state="running",
            started_at=to_iso(now),
            lease_until=to_iso(now + timedelta(seconds=self.lease_s)),
            next_attempt_at=None,
        )
        task = asyncio.create_task(self.process(job), name=f"llmhub-job-{job_id}")
        self._inflight[job_id] = task
        self._inflight_apps.setdefault(app, []).append(job_id)
        self._last_dispatch[app] = now
        task.add_done_callback(lambda _task, jid=job_id, capp=app: self._released(jid, capp))
        return task

    def _released(self, job_id: str, app: str) -> None:
        task = self._inflight.pop(job_id, None)
        running = self._inflight_apps.get(app)
        if running is not None:
            if job_id in running:
                running.remove(job_id)
            if not running:
                self._inflight_apps.pop(app, None)
        if task is None:
            return
        if task.cancelled():
            # a cancelled task is either an owner's cancellation or a worker that died; only
            # the first ends the job, the second is a lost lease and gets requeued
            asked_for = job_id in self._cancelling or job_id in self.hub.router.cancelled_jobs
            self._cancelling.discard(job_id)
            self.hub.router.cancelled_jobs.discard(job_id)
            if asked_for:
                self.close_cancelled(job_id)
            return
        exc = task.exception()
        if exc is not None:
            log.exception("job %s crashed", job_id, exc_info=exc)

    def close_cancelled(self, job_id: str) -> None:
        """Mark a cancelled job terminal, unless something already did."""
        job = self.hub.store.job(job_id)
        if job is None or job["state"] in TERMINAL_STATES:
            return
        self.hub.store.update_job(
            job_id,
            state="cancelled",
            finished_at=now_iso(),
            lease_until=None,
            next_attempt_at=None,
            error=json.dumps({"code": CANCEL_BY_OWNER, "message": "cancelled while running"}),
        )
        self.hub.store.add_event(
            kind="job", message=f"job {job_id} cancelled while running", app=str(job["app"])
        )

    def cancel_running(self, job_id: str) -> bool:
        """Cancel the worker task of a running job. The vendor call goes with it."""
        task = self._inflight.get(job_id)
        if task is None or task.done():
            return False
        self._cancelling.add(job_id)
        task.cancel()
        return True

    def shares(self) -> dict[str, dict[str, Any]]:
        """Per-app queue picture for `GET api/apps` and the Queue tab.

        `active` is counted the way the dispatcher counts it - an app whose only queued jobs
        are backing off holds no share - so the cap reported here is the cap that will apply.
        """
        counts = self.hub.store.job_app_breakdown()
        runnable = {str(job["app"]) for job in self.hub.store.claimable_jobs(now_iso())}
        apps = set(counts) | set(self._inflight_apps)
        paused = {app for app in apps if self.hub.store.app_paused(app)}
        cap = self.cap(len((runnable | set(self._inflight_apps)) - paused))
        return {
            app: {
                "queued": counts.get(app, {}).get("queued", 0),
                "running": counts.get(app, {}).get("running", 0),
                "cap": 0 if app in paused else cap,
                "paused": app in paused,
            }
            for app in sorted(apps)
        }

    async def drain(self) -> None:
        while self._inflight:
            await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)

    async def run_once(self) -> None:
        await self.tick()
        await self.drain()

    async def process(self, job: dict[str, Any]) -> str:
        store = self.hub.store
        job_id = job["id"]
        body = json.loads(job["request"])
        require = json.loads(job["require"] or "[]")
        model_request = job["model"] or "auto"
        app = job["app"]

        try:
            result = await execute_chat(
                self.hub,
                body=body,
                app=app,
                model_request=model_request,
                require=require,
                allow_paid=False,
                stream=False,
                kind="job",
                job_id=str(job_id),
            )
        except (NoCandidatesError, AllCandidatesFailed) as exc:
            if isinstance(exc, AllCandidatesFailed) and not all(
                attempt["status"] == "quota" for attempt in exc.attempts
            ):
                return self._fail(job_id, f"upstream failure: {exc.attempts}")
            next_window = self.hub.router.next_window_at(model_request)
            rejected = exc.rejected if isinstance(exc, NoCandidatesError) else []
            room = quota_room(rejected)
            attempts = int(job.get("attempts") or 0) + 1
            # no window data means no reset will ever be reported: back off on a clock of our
            # own instead of waiting for an event that never comes
            blind = not next_window or pool_is_windowless(self.hub, model_request)
            next_attempt = to_iso(backoff_at(utcnow(), attempts)) if blind else None
            store.update_job(
                job_id,
                state="waiting_quota",
                next_window_at=next_window,
                next_attempt_at=next_attempt,
                lease_until=None,
                attempts=attempts,
                error=json.dumps(
                    {
                        "state": "waiting_quota",
                        "next_window_at": next_window,
                        "next_attempt_at": next_attempt,
                        "remaining_out": room["remaining_out"],
                        "remaining_in": room["remaining_in"],
                        "remaining_requests": room["remaining_requests"],
                    }
                ),
            )
            return "waiting_quota"
        except UnknownModelError as exc:
            return self._fail(job_id, str(exc))
        except UpstreamError as exc:
            return self._fail(job_id, f"{exc.status_code}: {exc.classification.message}")
        except Exception as exc:  # noqa: BLE001
            return self._fail(job_id, repr(exc))

        call: CallResult = result.result
        payload = call.payload if call.payload is not None else {"raw": call.raw.decode("utf-8", "replace")}
        entry: Entry = result.entry
        store.update_job(
            job_id,
            state="done",
            finished_at=now_iso(),
            result=json.dumps(payload),
            served_by=f"{entry.key}#{entry.account_id}",
            attempts=int(job.get("attempts") or 0) + 1,
            lease_until=None,
            next_attempt_at=None,
            error=None,
        )
        await self._callback(job, "done", payload)
        return "done"

    def _fail(self, job_id: str, error: str) -> str:
        self.hub.store.update_job(
            job_id, state="failed", finished_at=now_iso(), lease_until=None, error=error[:1000]
        )
        self.hub.store.add_event(kind="job", message=f"job {job_id} failed: {error}"[:500])
        return "failed"

    async def _callback(self, job: dict[str, Any], state: str, payload: Any) -> None:
        url = job.get("callback_url")
        if not url:
            return
        try:
            await self.hub.client.post(
                url, json={"id": job["id"], "state": state, "result": payload}, timeout=15.0
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("callback failed for job %s: %s", job["id"], exc)
            self.hub.store.add_event(kind="job", message=f"callback failed for job {job['id']}: {exc}"[:500])


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


def queue_of(hub: Hub) -> JobQueue | None:
    queue = getattr(hub, "jobs", None)
    return queue if isinstance(queue, JobQueue) else None


@router.post("/jobs")
async def create_job(request: Request, payload: JobRequest) -> Any:
    require_token(request)
    hub = hub_of(request)
    model_request = payload.model or payload.alias or payload.request.get("model") or "auto"
    try:
        hub.router.resolve_pool(str(model_request))
    except UnknownModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = hub.settings
    live = hub.store.job_app_counts().get(payload.app, 0)
    if live >= settings.queue_max_per_app:
        hub.store.add_event(
            kind="queue",
            message=f"queue full for {payload.app}: {live} live jobs",
            app=payload.app,
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "queue_full",
                    "message": f"app '{payload.app}' already has {live} live jobs "
                    f"(max {settings.queue_max_per_app})",
                    "queued": live,
                    "max_per_app": settings.queue_max_per_app,
                }
            },
            headers={"Retry-After": "60"},
        )

    # what can never run is never queued: a request over the widest window the account will
    # ever open is a client bug, not something to wait out
    want = requested_out(payload.request)
    limit = pool_window_limit(hub, str(model_request))
    if want is not None and limit is not None and want > limit:
        hub.store.add_event(
            kind="queue",
            message=f"rejected job for {payload.app}: max_tokens {want} over window limit "
            f"{limit} on {model_request}",
            app=payload.app,
            model=str(model_request),
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "window_too_small",
                    "message": f"max_tokens {want} exceeds the largest window "
                    f"{model_request} will ever open ({limit} tokens)",
                    "window_limit": limit,
                    "requested_out": want,
                    "model": str(model_request),
                }
            },
        )

    ttl_s = min(max(1, payload.ttl_s or settings.job_ttl_s), settings.job_ttl_max_s)
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    return hub.store.create_job(
        job_id=job_id,
        app=payload.app,
        model=str(model_request),
        require=payload.require,
        priority=max(1, min(9, payload.priority)),
        request=payload.request,
        callback_url=payload.callback_url,
        ttl_s=ttl_s,
    )


@router.get("/jobs")
async def list_jobs(
    request: Request, app: str | None = None, status: str | None = None, state: str | None = None
) -> dict[str, Any]:
    hub = hub_of(request)
    return {"jobs": hub.store.jobs(state=state or status, app=app), "live_states": list(LIVE_JOB_STATES)}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    job = hub_of(request).store.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.delete("/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    store = hub.store
    job = store.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["state"] in TERMINAL_STATES:
        return {"id": job_id, "state": job["state"], "cancelled": False}
    queue = queue_of(hub)
    # a running job holds a vendor call and a concurrency slot; flipping the row alone would
    # leave both running to the end and the slot blocked for everyone behind it
    stopped = bool(queue and queue.cancel_running(job_id))
    error = json.dumps({"code": CANCEL_BY_OWNER, "message": "cancelled while running"}) if stopped else None
    store.update_job(
        job_id,
        state="cancelled",
        finished_at=now_iso(),
        lease_until=None,
        next_attempt_at=None,
        error=error,
    )
    return {"id": job_id, "state": "cancelled", "cancelled": True, "stopped_worker": stopped}
