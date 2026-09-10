from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from llmhub import jobs as jobs_module
from llmhub.config import Settings
from llmhub.gateway import CallResult
from llmhub.router import RunResult
from llmhub.runtime import Hub

from .conftest import entry_of


def job_body(app: str, priority: int = 5) -> dict[str, Any]:
    return {
        "app": app,
        "model": "alpha/m1",
        "priority": priority,
        "request": {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
    }


def fake_result(hub: Hub) -> RunResult:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    payload = {"id": "chatcmpl-fake", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    call = CallResult(
        entry=entry,
        status_code=200,
        payload=payload,
        raw=b"{}",
        latency_ms=1,
        usage_id=0,
    )
    return RunResult(entry=entry, result=call, attempts=[])


async def test_worker_pool_runs_jobs_concurrently_up_to_the_limit(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.jobs.workers = 2
    release = asyncio.Event()
    state = {"active": 0, "peak": 0}

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await release.wait()
        state["active"] -= 1
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)

    ids = [
        (await client.post("/jobs", json=job_body(app))).json()["id"]
        for app in ("batch-ocr", "my-app", "notes")
    ]

    await hub.jobs.tick()
    assert hub.jobs.inflight == 2
    assert hub.store.job(ids[2])["state"] == "queued"

    for _ in range(3):
        await asyncio.sleep(0)
    assert state["active"] == 2

    release.set()
    await hub.jobs.drain()
    assert state["peak"] == 2

    await hub.jobs.run_once()
    assert [hub.store.job(job_id)["state"] for job_id in ids] == ["done", "done", "done"]


async def test_job_in_retry_backoff_does_not_block_other_apps(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.jobs.workers = 3
    stuck = asyncio.Event()

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        if kwargs["app"] == "slowpoke":
            await stuck.wait()
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)

    slow_id = (await client.post("/jobs", json=job_body("slowpoke", priority=1))).json()["id"]
    fast_id = (await client.post("/jobs", json=job_body("my-app", priority=9))).json()["id"]

    await hub.jobs.tick()
    await hub.jobs._inflight[fast_id]

    assert hub.store.job(fast_id)["state"] == "done"
    assert hub.store.job(slow_id)["state"] == "running"

    stuck.set()
    await hub.jobs.drain()
    assert hub.store.job(slow_id)["state"] == "done"


async def test_one_job_per_app_when_jobs_per_app_is_one(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.jobs.workers = 3
    hub.jobs.jobs_per_app = 1
    release = asyncio.Event()

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        await release.wait()
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)

    first = (await client.post("/jobs", json=job_body("my-app", priority=1))).json()["id"]
    second = (await client.post("/jobs", json=job_body("my-app", priority=1))).json()["id"]

    await hub.jobs.tick()
    assert hub.jobs.inflight == 1
    assert hub.store.job(first)["state"] == "running"
    assert hub.store.job(second)["state"] == "queued"

    release.set()
    await hub.jobs.drain()
    await hub.jobs.run_once()
    assert hub.store.job(second)["state"] == "done"


async def test_three_jobs_of_one_app_run_concurrently(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.jobs.workers = 6
    hub.jobs.jobs_per_app = 3
    release = asyncio.Event()
    state = {"active": 0, "peak": 0}

    async def fake_execute(hub_arg: Hub, **kwargs: Any) -> RunResult:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await release.wait()
        state["active"] -= 1
        return fake_result(hub_arg)

    monkeypatch.setattr(jobs_module, "execute_chat", fake_execute)

    ids = [(await client.post("/jobs", json=job_body("my-app", priority=1))).json()["id"] for _ in range(4)]

    await hub.jobs.tick()
    assert hub.jobs.inflight == 3
    assert hub.jobs.app_inflight("my-app") == 3
    # FIFO inside the app: the three oldest claims run, the fourth waits for a slot
    assert [hub.store.job(job_id)["state"] for job_id in ids] == [
        "running",
        "running",
        "running",
        "queued",
    ]

    for _ in range(3):
        await asyncio.sleep(0)
    assert state["active"] == 3

    release.set()
    await hub.jobs.drain()
    assert state["peak"] == 3

    await hub.jobs.run_once()
    assert [hub.store.job(job_id)["state"] for job_id in ids] == ["done"] * 4


def test_job_workers_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMHUB_JOB_WORKERS", "7")
    assert Settings.from_env().job_workers == 7
    monkeypatch.setenv("LLMHUB_JOB_WORKERS", "nonsense")
    assert Settings.from_env().job_workers == 6
    monkeypatch.delenv("LLMHUB_JOB_WORKERS")
    assert Settings.from_env().job_workers == 6


def test_jobs_per_app_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # the hard ceiling is off by default: the fair share is what limits an app day to day
    monkeypatch.setenv("LLMHUB_JOBS_PER_APP", "5")
    assert Settings.from_env().jobs_per_app == 5
    monkeypatch.setenv("LLMHUB_JOBS_PER_APP", "nonsense")
    assert Settings.from_env().jobs_per_app == 0
    monkeypatch.delenv("LLMHUB_JOBS_PER_APP")
    assert Settings.from_env().jobs_per_app == 0
    assert Settings.from_env().jobs_per_app_min == 1
    monkeypatch.setenv("LLMHUB_JOBS_PER_APP_MIN", "2")
    assert Settings.from_env().jobs_per_app_min == 2


def test_queue_defaults_to_settings_workers(hub: Hub) -> None:
    assert hub.settings.job_workers == 6
    assert hub.settings.jobs_per_app == 0
    assert jobs_module.JobQueue(hub).workers == 6
    assert jobs_module.JobQueue(hub).jobs_per_app == 0
    assert jobs_module.JobQueue(hub, workers=1).workers == 1
    assert jobs_module.JobQueue(hub, jobs_per_app=1).jobs_per_app == 1
