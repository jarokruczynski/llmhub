from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from llmhub import jobs as jobs_module
from llmhub.gateway import CallResult
from llmhub.router import RunResult
from llmhub.runtime import Hub
from llmhub.store import to_iso

from .conftest import entry_of

JOB_BODY: dict[str, Any] = {
    "app": "batch-ocr",
    "model": "alpha/m1",
    "request": {"messages": [{"role": "user", "content": "summarize"}], "max_tokens": 32},
}


def body(**overrides: Any) -> dict[str, Any]:
    payload = dict(JOB_BODY)
    payload.update(overrides)
    return payload


def fake_result(hub: Hub) -> RunResult:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    call = CallResult(
        entry=entry,
        status_code=200,
        payload={"id": "chatcmpl-fake"},
        raw=b"{}",
        latency_ms=1,
        usage_id=0,
    )
    return RunResult(entry=entry, result=call, attempts=[])


def observe_window(hub: Hub, model: str, value: int) -> None:
    """Pin the window this model will ever open with, the way a 429 would."""
    for entry in hub.registry.entries():
        if entry.key == model:
            hub.store.record_observed_limit(entry.account_id, entry.key, "hourly", "out_tokens", value)


def exhaust_free_models(hub: Hub) -> None:
    for entry in hub.registry.entries():
        if entry.model.is_free:
            hub.quota.mark_exhausted(entry, "insufficient_quota")


async def test_job_over_the_window_limit_is_rejected_and_never_queued(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    observe_window(hub, "alpha/m1", 300)
    for _ in range(10):
        response = await client.post(
            "/jobs",
            json=body(request={"messages": [{"role": "user", "content": "x"}], "max_tokens": 6000}),
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "window_too_small"
        assert error["window_limit"] == 300
        assert error["requested_out"] == 6000
    assert hub.store.job_state_counts() == {}


async def test_request_without_max_tokens_is_still_queued(client: httpx.AsyncClient, hub: Hub) -> None:
    # nothing was asked for, so nothing is known to be too large: an assumed default must not
    # reject work the vendor might well have served
    observe_window(hub, "alpha/m1", 300)
    response = await client.post("/jobs", json=body(request={"messages": [{"role": "user", "content": "x"}]}))
    assert response.status_code == 200
    assert response.json()["state"] == "queued"


async def test_queued_job_expires_with_no_window_reason(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post("/jobs", json=body(ttl_s=60))
    job_id = created.json()["id"]
    assert created.json()["ttl_s"] == 60
    exhaust_free_models(hub)

    hub.jobs.maintain(jobs_module.utcnow() + timedelta(minutes=2))

    job = (await client.get(f"/jobs/{job_id}")).json()
    assert job["state"] == "expired"
    error = json.loads(job["error"])
    assert error["code"] == "expired"
    assert error["reason"] == "no_window"


async def test_queued_job_expires_as_window_too_small_when_the_registry_changes(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    job_id = (
        await client.post(
            "/jobs", json=body(request={"messages": [{"role": "user", "content": "x"}], "max_tokens": 900})
        )
    ).json()["id"]
    # the vendor answers a 429 that says the window is far smaller than declared
    observe_window(hub, "alpha/m1", 300)

    hub.jobs.maintain(jobs_module.utcnow())

    job = (await client.get(f"/jobs/{job_id}")).json()
    assert job["state"] == "expired"
    error = json.loads(job["error"])
    assert (error["reason"], error["window_limit"], error["requested_out"]) == ("window_too_small", 300, 900)


async def test_expired_job_says_no_worker_when_a_candidate_had_room(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    # nothing is exhausted here: the model could have served it, the queue never got to it
    job_id = (await client.post("/jobs", json=body(ttl_s=60))).json()["id"]

    hub.jobs.maintain(jobs_module.utcnow() + timedelta(minutes=2))

    job = hub.store.job(job_id)
    assert job["state"] == "expired"
    assert json.loads(job["error"])["reason"] == "no_worker"


async def test_ttl_is_clamped_to_the_cap(client: httpx.AsyncClient, hub: Hub) -> None:
    job = (await client.post("/jobs", json=body(ttl_s=10 * 24 * 3600))).json()
    assert job["ttl_s"] == hub.settings.job_ttl_max_s
    assert jobs_module.deadline_of(job) - jobs_module.parse_iso(job["created_at"]) == timedelta(hours=36)


async def test_default_ttl_is_six_hours(client: httpx.AsyncClient) -> None:
    job = (await client.post("/jobs", json=body())).json()
    assert job["ttl_s"] == 6 * 3600
    assert jobs_module.deadline_of(job) - jobs_module.parse_iso(job["created_at"]) == timedelta(hours=6)


async def test_killed_worker_returns_the_job_then_fails_it_lease_lost(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = asyncio.Event()

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        await stuck.wait()
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)
    job_id = (await client.post("/jobs", json=body())).json()["id"]

    await hub.jobs.tick()
    assert hub.store.job(job_id)["state"] == "running"
    assert hub.store.job(job_id)["lease_until"]

    # the worker dies mid-request: the task is gone, the row still says running
    task = hub.jobs._inflight[job_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert hub.jobs.inflight == 0
    assert hub.store.job(job_id)["state"] == "running"

    later = jobs_module.utcnow() + timedelta(seconds=hub.jobs.lease_s + 60)
    hub.jobs.reclaim_lost_leases(later)
    job = hub.store.job(job_id)
    assert (job["state"], job["lease_attempts"]) == ("queued", 1)

    for attempt in (2, 3):
        hub.store.update_job(job_id, state="running", lease_until=to_iso(later))
        hub.jobs.reclaim_lost_leases(later + timedelta(seconds=hub.jobs.lease_s + 60))
        job = hub.store.job(job_id)
        assert job["lease_attempts"] == attempt

    assert job["state"] == "failed"
    assert json.loads(job["error"])["code"] == "lease_lost"


async def test_running_job_keeps_its_lease_while_the_worker_lives(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = asyncio.Event()

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        await stuck.wait()
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)
    job_id = (await client.post("/jobs", json=body())).json()["id"]
    await hub.jobs.tick()

    first = hub.store.job(job_id)["lease_until"]
    later = jobs_module.utcnow() + timedelta(hours=1)
    hub.jobs.maintain(later)
    renewed = hub.store.job(job_id)

    assert renewed["state"] == "running"
    assert renewed["lease_until"] > first

    stuck.set()
    await hub.jobs.drain()


async def test_windowless_model_backs_off_instead_of_waiting_for_a_window(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    # beta/m2 declares `free: {}`: no window will ever be reported for it
    job_id = (await client.post("/jobs", json=body(model="beta/m2"))).json()["id"]
    exhaust_free_models(hub)

    await hub.jobs.run_once()

    job = hub.store.job(job_id)
    assert job["state"] == "waiting_quota"
    assert job["next_attempt_at"]
    wait = jobs_module.parse_iso(job["next_attempt_at"]) - jobs_module.utcnow()
    assert timedelta(seconds=30) < wait <= timedelta(seconds=60)
    # and it is not claimed again until then
    assert hub.store.claimable_jobs(to_iso(jobs_module.utcnow())) == []
    assert [row["id"] for row in hub.store.claimable_jobs(job["next_attempt_at"])] == [job_id]


async def test_backoff_grows_with_attempts() -> None:
    now = jobs_module.utcnow()
    steps = [(jobs_module.backoff_at(now, n) - now).total_seconds() for n in (1, 2, 3, 4, 5, 9)]
    assert steps == [60, 300, 900, 1800, 1800, 1800]


async def test_queue_cap_rejects_a_flooding_client(client: httpx.AsyncClient, hub: Hub) -> None:
    from dataclasses import replace

    hub.settings = replace(hub.settings, queue_max_per_app=2)
    for _ in range(2):
        assert (await client.post("/jobs", json=body())).status_code == 200
    response = await client.post("/jobs", json=body())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    error = response.json()["error"]
    assert (error["code"], error["max_per_app"]) == ("queue_full", 2)
    # another app is unaffected: the cap is per app, not per hub
    assert (await client.post("/jobs", json=body(app="my-app"))).status_code == 200


async def test_listing_defaults_to_live_rows_newest_first(client: httpx.AsyncClient, hub: Hub) -> None:
    first = (await client.post("/jobs", json=body())).json()["id"]
    second = (await client.post("/jobs", json=body())).json()["id"]
    await client.delete(f"/jobs/{first}")

    live = (await client.get("/jobs")).json()["jobs"]
    assert [row["id"] for row in live] == [second]

    everything = (await client.get("/jobs", params={"state": "all"})).json()["jobs"]
    assert [row["id"] for row in everything] == [second, first]

    # the dashboard counter and the default listing are the same number
    status = (await client.get("/api/status")).json()
    assert status["queue"]["live"] == len(live)
    assert status["queue"]["oldest_queued_at"] == hub.store.job(second)["created_at"]
    assert status["queue"]["expired_last_24h"] == 0


async def test_status_counts_expired_jobs_of_the_last_day(client: httpx.AsyncClient, hub: Hub) -> None:
    job_id = (await client.post("/jobs", json=body(ttl_s=60))).json()["id"]
    exhaust_free_models(hub)
    hub.jobs.maintain(jobs_module.utcnow() + timedelta(minutes=2))

    queue = (await client.get("/api/status")).json()["queue"]
    assert queue["expired_last_24h"] == 1
    assert queue["live"] == 0
    assert hub.store.job(job_id)["state"] == "expired"


async def test_terminal_rows_are_purged_after_the_retention_window(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    old = (await client.post("/jobs", json=body())).json()["id"]
    recent = (await client.post("/jobs", json=body())).json()["id"]
    now = jobs_module.utcnow()
    hub.store.update_job(old, state="done", finished_at=to_iso(now - timedelta(days=8)))
    hub.store.update_job(recent, state="done", finished_at=to_iso(now - timedelta(days=1)))

    hub.jobs.purge_old_jobs(now)

    assert hub.store.job(old) is None
    assert hub.store.job(recent) is not None


async def test_job_row_keeps_the_shape_older_clients_read(client: httpx.AsyncClient, hub: Hub) -> None:
    job = (await client.post("/jobs", json=body())).json()
    assert {"id", "app", "model", "state", "result", "error", "created_at"} <= set(job)
    assert (job["state"], job["result"], job["error"]) == ("queued", None, None)
