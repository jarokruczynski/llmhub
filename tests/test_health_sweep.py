from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from llmhub.health import HealthSweepService, last_sweep, sweep_once
from llmhub.runtime import Hub
from llmhub.store import to_iso


def mark_every_account_served(hub: Hub, when: datetime) -> int:
    served = 0
    for provider_name, provider in hub.registry.providers.items():
        if not provider.models:
            continue
        for account in provider.accounts:
            hub.store.start_usage(
                app="test",
                provider=provider_name,
                account=account.id,
                model=f"{provider_name}/{provider.models[0].id}",
                status="ok",
                latency_ms=1,
                attempt=1,
                ts=to_iso(when),
            )
            served += 1
    return served


async def test_traffic_inside_the_window_stands_in_for_a_probe(hub: Hub) -> None:
    now = datetime.now(UTC)
    accounts = mark_every_account_served(hub, now - timedelta(minutes=5))

    result = await sweep_once(hub, now=now, stale_after_h=6)

    assert result["probed_count"] == 0
    assert result["request_cost"] == 0
    assert len(result["skipped"]) == accounts
    assert {row["reason"] for row in result["skipped"]} == {"recently_served"}


async def test_traffic_older_than_the_window_does_not_count(hub: Hub) -> None:
    now = datetime.now(UTC)
    mark_every_account_served(hub, now - timedelta(hours=30))

    result = await sweep_once(hub, now=now, stale_after_h=6)

    reasons = {row["reason"] for row in result["skipped"]}
    assert "recently_served" not in reasons


async def test_the_last_sweep_survives_a_restart(hub: Hub) -> None:
    hub.store.record_health_sweep(
        ts=to_iso(datetime.now(UTC)),
        probed=3,
        skipped=2,
        results=[{"provider": "alpha", "account": "alpha-1", "ok": True}],
    )

    restarted = Hub.create(hub.settings)
    try:
        summary = last_sweep(restarted)
    finally:
        restarted.store.close()

    assert summary["probed_count"] == 3
    assert summary["skipped_count"] == 2
    assert summary["results"][0]["account"] == "alpha-1"


async def test_the_schedule_counts_from_the_last_sweep_not_from_startup(hub: Hub) -> None:
    hub.settings = replace(hub.settings, health_sweep_every_h=6)
    stamp = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
    hub.store.record_health_sweep(ts=to_iso(stamp), probed=1, skipped=0, results=[])

    assert HealthSweepService(hub).next_run_dt() == stamp + timedelta(hours=6)


async def test_zero_hours_turns_the_schedule_off(hub: Hub) -> None:
    hub.settings = replace(hub.settings, health_sweep_every_h=0)
    service = HealthSweepService(hub)

    assert service.next_run_at() is None
    await service.start()
    try:
        assert service._task is None
    finally:
        await service.stop()


async def test_health_status_reports_the_schedule(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/health/status")

    assert res.status_code == 200
    body = res.json()
    assert body["every_h"] >= 0
    assert "next_run_at" in body
    assert "last_sweep_at" in body


async def test_a_pair_busy_with_real_traffic_is_not_probed(hub: Hub, monkeypatch) -> None:
    probed: list[tuple[str, str]] = []

    async def fake_probe(_hub: Hub, entry, prompt: str = "ping") -> dict[str, object]:
        probed.append((entry.account_id, entry.key))
        return {"ok": True, "latency_ms": 1}

    monkeypatch.setattr("llmhub.api.probe_entry", fake_probe)

    entry = hub.registry.entry("alpha/m1", "alpha-1")
    assert entry is not None
    object.__setattr__(entry.model, "concurrency", 1)
    semaphore = hub.router.semaphore(entry)
    assert semaphore is not None
    semaphore._value = 0

    result = await sweep_once(hub, stale_after_h=0)

    busy = [row for row in result["skipped"] if row["account"] == "alpha-1"]
    assert busy and busy[0]["reason"] == "in_use"
    assert ("alpha-1", "alpha/m1") not in probed
    assert ("alpha-2", "alpha/m1") in probed


async def test_a_probe_that_never_answers_ends_as_a_timeout(hub: Hub, monkeypatch) -> None:
    async def hanging_probe(_hub: Hub, _entry, prompt: str = "ping") -> dict[str, object]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr("llmhub.api.probe_entry", hanging_probe)
    monkeypatch.setattr("llmhub.health.PROBE_TIMEOUT_S", 0.05)

    result = await sweep_once(hub, stale_after_h=0)

    assert result["probed_count"] >= 1
    assert {row["status"] for row in result["results"]} == {"timeout"}
    assert not any(row["ok"] for row in result["results"])
