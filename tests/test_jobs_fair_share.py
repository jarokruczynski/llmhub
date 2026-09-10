from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from llmhub import jobs as jobs_module
from llmhub.gateway import CallResult
from llmhub.router import RunResult
from llmhub.runtime import Hub

from .conftest import entry_of


def fake_result(hub: Hub) -> RunResult:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    call = CallResult(
        entry=entry, status_code=200, payload={"id": "chatcmpl-fake"}, raw=b"{}", latency_ms=1, usage_id=0
    )
    return RunResult(entry=entry, result=call, attempts=[])


class Workers:
    """Jobs that run until the test lets them finish, one gate per job."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.ids: dict[str, str] = {}

    def gate(self, tag: str) -> asyncio.Event:
        return self.gates.setdefault(tag, asyncio.Event())

    async def execute(self, hub: Hub, **kwargs: Any) -> RunResult:
        tag = kwargs["body"]["messages"][0]["content"]
        await self.gate(tag).wait()
        return fake_result(hub)

    async def post(self, client: httpx.AsyncClient, app: str, count: int = 1) -> list[str]:
        tags = []
        for index in range(count):
            tag = f"{app}-{len(self.ids)}-{index}"
            response = await client.post(
                "/jobs",
                json={
                    "app": app,
                    "model": "alpha/m1",
                    "request": {"messages": [{"role": "user", "content": tag}], "max_tokens": 8},
                },
            )
            assert response.status_code == 200
            self.ids[tag] = response.json()["id"]
            tags.append(tag)
        return tags

    async def finish(self, hub: Hub, tags: list[str]) -> None:
        tasks = [hub.jobs._inflight[self.ids[tag]] for tag in tags]
        for tag in tags:
            self.gate(tag).set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> Workers:
    harness = Workers()
    monkeypatch.setattr(jobs_module, "execute_chat", harness.execute)
    return harness


def running(hub: Hub) -> dict[str, int]:
    return {app: len(jobs) for app, jobs in hub.jobs._inflight_apps.items()}


async def test_one_app_saturates_the_whole_pool(
    client: httpx.AsyncClient, hub: Hub, workers: Workers
) -> None:
    hub.jobs.workers = 4
    await workers.post(client, "my-app", 6)

    await hub.jobs.tick()

    assert hub.jobs.inflight == 4
    assert running(hub) == {"my-app": 4}


async def test_second_app_gets_the_freed_slots_until_balanced(
    client: httpx.AsyncClient, hub: Hub, workers: Workers
) -> None:
    hub.jobs.workers = 4
    first = await workers.post(client, "my-app", 6)
    await hub.jobs.tick()
    await workers.post(client, "batch-ocr", 4)

    # nothing is preempted: the pool is full and stays with the app that holds it
    await hub.jobs.tick()
    assert running(hub) == {"my-app": 4}

    await workers.finish(hub, first[:2])
    await hub.jobs.tick()

    assert running(hub) == {"my-app": 2, "batch-ocr": 2}


async def test_third_app_gets_the_next_slots(client: httpx.AsyncClient, hub: Hub, workers: Workers) -> None:
    hub.jobs.workers = 6
    first = await workers.post(client, "my-app", 6)
    await hub.jobs.tick()
    await workers.post(client, "batch-ocr", 2)
    await workers.post(client, "notes", 2)

    await workers.finish(hub, first[:2])
    await hub.jobs.tick()

    # cap is 2 with three apps active, so the two freed slots go to the two apps holding none
    assert running(hub) == {"my-app": 4, "batch-ocr": 1, "notes": 1}


async def test_drained_app_gives_its_share_back(
    client: httpx.AsyncClient, hub: Hub, workers: Workers
) -> None:
    hub.jobs.workers = 4
    first = await workers.post(client, "my-app", 4)
    await hub.jobs.tick()
    await workers.post(client, "batch-ocr", 3)
    assert running(hub) == {"my-app": 4}

    await workers.finish(hub, first)
    await hub.jobs.tick()

    # my-app has nothing left to run, so the whole pool is batch-ocr's share
    assert running(hub) == {"batch-ocr": 3}


async def test_paused_app_holds_no_share(client: httpx.AsyncClient, hub: Hub, workers: Workers) -> None:
    hub.jobs.workers = 2
    await workers.post(client, "my-app", 3)
    await client.post("/api/apps/my-app/pause")
    await workers.post(client, "batch-ocr", 2)

    await hub.jobs.tick()

    assert running(hub) == {"batch-ocr": 2}
    assert hub.jobs.shares()["my-app"] == {"queued": 3, "running": 0, "cap": 0, "paused": True}


async def test_hard_ceiling_is_respected(client: httpx.AsyncClient, hub: Hub, workers: Workers) -> None:
    hub.jobs.workers = 6
    hub.jobs.jobs_per_app = 2
    await workers.post(client, "my-app", 5)

    await hub.jobs.tick()

    assert running(hub) == {"my-app": 2}
    assert hub.jobs.cap(1) == 2


async def test_cap_floors_at_one_when_apps_outnumber_workers(
    client: httpx.AsyncClient, hub: Hub, workers: Workers
) -> None:
    hub.jobs.workers = 2
    for app in ("my-app", "batch-ocr", "notes", "hub-cli"):
        await workers.post(client, app, 1)

    assert hub.jobs.cap(4) == 1
    await hub.jobs.tick()

    assert hub.jobs.inflight == 2
    assert sorted(running(hub).values()) == [1, 1]


async def test_share_change_is_one_event_not_one_per_dispatch(
    client: httpx.AsyncClient, hub: Hub, workers: Workers
) -> None:
    hub.jobs.workers = 4
    await workers.post(client, "my-app", 4)
    await hub.jobs.tick()
    await hub.jobs.tick()
    assert len([row for row in hub.store.events(50) if row["kind"] == "queue"]) == 1

    await workers.post(client, "batch-ocr", 2)
    await hub.jobs.tick()
    await hub.jobs.tick()

    messages = [row["message"] for row in hub.store.events(50) if row["kind"] == "queue"]
    assert len(messages) == 2
    assert "2 app(s)" in messages[0]
    assert "cap 2 of 4 workers" in messages[0]


async def test_apps_endpoint_exposes_the_share(client: httpx.AsyncClient, hub: Hub, workers: Workers) -> None:
    hub.jobs.workers = 4
    await workers.post(client, "my-app", 3)
    await workers.post(client, "batch-ocr", 1)
    await hub.jobs.tick()

    rows = {row["app"]: row for row in (await client.get("/api/apps")).json()["apps"]}
    assert rows["my-app"]["running"] == 2
    assert rows["my-app"]["queued"] == 1
    assert rows["my-app"]["cap"] == 2
    assert rows["batch-ocr"]["running"] == 1

    queue = (await client.get("/api/status")).json()["queue"]
    assert queue["workers"] == 4
    assert queue["apps"]["batch-ocr"]["cap"] == 2
