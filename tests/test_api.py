from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from llmhub import gateway
from llmhub.runtime import Hub
from llmhub.store import to_iso

from .conftest import entry_of, token_quota


async def test_status_shape(client: httpx.AsyncClient, hub: Hub) -> None:
    response = await client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"]
    assert set(payload["queue"]) == {
        "depth_by_state",
        "by_app",
        "live",
        "oldest_queued_at",
        "expired_last_24h",
        "workers",
        "apps",
    }

    row = next(item for item in payload["models"] if item["key"] == "alpha/m1")
    assert row["provider"] == "alpha"
    assert row["account"] in ("alpha-1", "alpha-2")
    assert row["caps"] == ["text", "tools"]
    assert row["status"] == "ok"
    assert set(row["windows"]) == {"hourly", "daily"}
    assert row["windows"]["hourly"]["limit"] == 1000
    assert row["windows"]["hourly"]["resets_at"]
    assert row["last_error"] is None
    assert row["avg_latency_ms"] is None


async def test_status_reports_exhausted(client: httpx.AsyncClient, hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(entry, "insufficient_quota")
    payload = (await client.get("/api/status")).json()
    row = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-1"
    )
    assert row["status"] == "exhausted"
    assert row["reason"] == to_iso(until)


async def test_registry_endpoint_exposes_env_name_not_value(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/api/registry")).json()
    account = payload["providers"]["alpha"]["accounts"][0]
    assert account["api_key_env"] == "ALPHA_KEY_1"
    assert account["key_present"] is True
    assert "test-key-1" not in str(payload)


async def test_forgive_clears_exhausted(client: httpx.AsyncClient, hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hub.quota.mark_exhausted(entry, "insufficient_quota")
    response = await client.post("/api/models/alpha/m1/forgive")
    assert response.status_code == 200
    assert response.json()["cleared"] == 1
    assert hub.store.query("SELECT * FROM exhausted") == []


async def test_forgive_unknown_model_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/models/ghost/model/forgive")).status_code == 404


async def test_disable_and_enable_model(client: httpx.AsyncClient, hub: Hub) -> None:
    assert (await client.post("/api/models/alpha/m1/disable")).json()["disabled"] is True
    assert hub.store.disabled_models() == {"alpha/m1"}
    assert (await client.post("/api/models/alpha/m1/enable")).json()["disabled"] is False
    assert hub.store.disabled_models() == set()


async def test_apps_pause_resume(client: httpx.AsyncClient, hub: Hub) -> None:
    assert (await client.post("/api/apps/my-app/pause")).json()["paused"] is True
    apps = (await client.get("/api/apps")).json()["apps"]
    assert next(item for item in apps if item["app"] == "my-app")["paused"] is True
    assert (await client.post("/api/apps/my-app/resume")).json()["paused"] is False


async def test_usage_grouping_and_timeseries(client: httpx.AsyncClient, hub: Hub) -> None:
    usage_id = hub.store.start_usage(
        app="my-app",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=120,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=10, out_tokens=20)

    by_app = (await client.get("/api/usage", params={"group_by": "app"})).json()
    assert by_app["rows"][0]["bucket"] == "my-app"
    assert by_app["rows"][0]["total_tokens"] == 30
    assert by_app["rows"][0]["errors"] == 0

    by_model = (await client.get("/api/usage", params={"group_by": "model"})).json()
    assert by_model["rows"][0]["bucket"] == "alpha/m1"

    series = (await client.get("/api/usage/timeseries", params={"bucket": "hour"})).json()
    assert series["bucket"] == "hour"
    assert series["rows"][0]["requests"] == 1


async def test_events_and_promos(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.store.add_event(kind="quota", message="alpha exhausted", model="alpha/m1", app="my-app")
    events = (await client.get("/api/events", params={"limit": 10})).json()["events"]
    assert events[0]["kind"] == "quota"

    created = await client.post(
        "/api/promos", json={"provider": "zai", "url": "https://z.ai/promo", "note": "free tier"}
    )
    assert created.status_code == 201
    promos = (await client.get("/api/promos")).json()["promos"]
    assert promos[0]["provider"] == "zai"
    assert promos[0]["status"] == "new"


async def test_lan_read_open_mutation_needs_token(client: httpx.AsyncClient, hub: Hub) -> None:
    lan = {"X-Forwarded-For": "192.168.1.44"}
    assert (await client.get("/api/status", headers=lan)).status_code == 200
    assert (await client.post("/api/apps/my-app/pause", headers=lan)).status_code == 401

    object.__setattr__(hub.settings, "token", "secret")
    ok = await client.post("/api/apps/my-app/pause", headers={**lan, "Authorization": "Bearer secret"})
    assert ok.status_code == 200


async def test_healthz(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json()["status"] == "ok"


async def test_promo_accepts_and_returns_source_and_expiry(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/promos",
        json={
            "provider": "explabs",
            "url": "https://example.test/promo",
            "note": "free daily tier",
            "source": "pepper.pl",
            "expires_at": "2026-12-31",
        },
    )
    assert created.status_code == 201
    row = created.json()
    assert row["source"] == "pepper.pl"
    assert row["expires_at"] == "2026-12-31"
    assert row["status"] == "new"

    listed = (await client.get("/api/promos")).json()["promos"][0]
    assert listed["source"] == "pepper.pl"
    assert listed["expires_at"] == "2026-12-31"


async def test_promo_source_and_expiry_are_optional(client: httpx.AsyncClient) -> None:
    row = (await client.post("/api/promos", json={"provider": "zai"})).json()
    assert row["source"] is None
    assert row["expires_at"] is None


async def test_promo_patch_status_and_note(client: httpx.AsyncClient) -> None:
    promo_id = (await client.post("/api/promos", json={"provider": "zai"})).json()["id"]

    patched = await client.patch(f"/api/promos/{promo_id}", json={"status": "used"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "used"

    noted = await client.patch(f"/api/promos/{promo_id}", json={"note": "burned through it"})
    assert noted.json()["note"] == "burned through it"
    assert noted.json()["status"] == "used"


async def test_promo_patch_rejects_unknown_status_and_id(client: httpx.AsyncClient) -> None:
    promo_id = (await client.post("/api/promos", json={"provider": "zai"})).json()["id"]
    assert (await client.patch(f"/api/promos/{promo_id}", json={"status": "burnt"})).status_code == 422
    assert (await client.patch("/api/promos/9999", json={"status": "used"})).status_code == 404
    assert (await client.post("/api/promos", json={"provider": "zai", "status": "nope"})).status_code == 422


async def test_promo_patch_needs_token_on_lan(client: httpx.AsyncClient, hub: Hub) -> None:
    promo_id = (await client.post("/api/promos", json={"provider": "zai"})).json()["id"]
    lan = {"X-Forwarded-For": "192.168.1.44"}
    assert (
        await client.patch(f"/api/promos/{promo_id}", json={"status": "used"}, headers=lan)
    ).status_code == 401

    object.__setattr__(hub.settings, "token", "secret")
    ok = await client.patch(
        f"/api/promos/{promo_id}",
        json={"status": "used"},
        headers={**lan, "Authorization": "Bearer secret"},
    )
    assert ok.status_code == 200


async def test_status_carries_usage_today_per_model(client: httpx.AsyncClient, hub: Hub) -> None:
    usage_id = hub.store.start_usage(
        app="my-app",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=12,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=30, out_tokens=70)
    hub.store.start_usage(
        app="my-app",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="error",
        latency_ms=3,
        attempt=2,
    )

    payload = (await client.get("/api/status")).json()
    row = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-1"
    )
    usage = row["usage_today"]
    assert set(usage) == {"requests", "in_tokens", "out_tokens", "errors", "last_used_at"}
    assert (usage["requests"], usage["in_tokens"], usage["out_tokens"], usage["errors"]) == (2, 30, 70, 1)
    assert usage["last_used_at"]

    idle = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-2"
    )
    assert idle["usage_today"] == {
        "requests": 0,
        "in_tokens": 0,
        "out_tokens": 0,
        "errors": 0,
        "last_used_at": None,
    }


async def test_status_carries_observed_limits_and_limit_source(client: httpx.AsyncClient, hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    usage_id = hub.store.start_usage(
        app="my-app",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=12,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=10, out_tokens=250)
    hub.quota.record_observed(entry, token_quota("daily"))

    payload = (await client.get("/api/status")).json()
    row = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-1"
    )
    assert row["observed"]["daily"]["out_tokens"] == 250
    assert row["observed"]["daily"]["observed_at"]
    assert row["windows"]["daily"]["limit"] == 250
    assert row["windows"]["daily"]["limit_source"] == "observed"
    assert row["windows"]["hourly"]["limit_source"] == "declared"

    other = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-2"
    )
    assert other["observed"] == {}
    assert other["windows"]["daily"]["limit"] == 5000


async def test_status_reports_alias_spread(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/api/status")).json()
    assert {row["alias"]: row["spread"] for row in payload["aliases"]} == {"auto": 4, "vision": 4}


async def test_drop_observed_limits_endpoint(client: httpx.AsyncClient, hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    usage_id = hub.store.start_usage(
        app="my-app",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=250)
    hub.quota.record_observed(entry, token_quota("daily"))
    hub.quota.mark_exhausted(entry, "free_limit_reached", scope="daily")

    # forgive lifts the marker, the measurement stays
    await client.post("/api/models/alpha/m1/forgive")
    assert hub.store.observed_limits("alpha-1", "alpha/m1")["daily"]["out_tokens"] == 250

    response = await client.request("DELETE", "/api/models/alpha/m1/observed")
    assert response.status_code == 200
    assert response.json()["cleared"] == 1
    assert hub.store.observed_limits("alpha-1", "alpha/m1") == {}
    assert (await client.request("DELETE", "/api/models/ghost/model/observed")).status_code == 404


async def test_status_carries_declared_and_observed_request_caps(client: httpx.AsyncClient, hub: Hub) -> None:
    for model in hub.registry.providers["alpha"].models:
        if model.id == "m1":
            model.max_request_tokens = 8000
    hub.store.record_request_cap("alpha-1", "alpha/m1", 5000, source="groq")

    payload = (await client.get("/api/status")).json()
    row = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-1"
    )
    assert row["max_request_tokens"] == 8000
    assert row["observed_request_cap"] == 5000

    other = next(
        item for item in payload["models"] if item["key"] == "alpha/m1" and item["account"] == "alpha-2"
    )
    assert other["max_request_tokens"] == 8000
    assert other["observed_request_cap"] is None

    beta = next(item for item in payload["models"] if item["key"] == "beta/m2")
    assert beta["max_request_tokens"] is None
    assert beta["observed_request_cap"] is None


async def test_drop_observed_limits_endpoint_also_clears_the_request_cap(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    hub.store.record_request_cap("alpha-1", "alpha/m1", 5000, source="groq")
    response = await client.request("DELETE", "/api/models/alpha/m1/observed")
    assert response.status_code == 200
    assert response.json()["cleared"] == 1
    assert hub.store.request_cap("alpha-1", "alpha/m1") is None


def _usage_row(hub: Hub, app: str, model: str, account: str, out_tokens: int, ts: str | None = None) -> None:
    usage_id = hub.store.start_usage(
        app=app,
        provider=model.split("/")[0],
        account=account,
        model=model,
        status="ok",
        latency_ms=10,
        attempt=1,
        ts=ts,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=10, out_tokens=out_tokens)


async def test_live_reports_in_flight_and_recent_per_app(client: httpx.AsyncClient, hub: Hub) -> None:
    _usage_row(hub, "my-app", "alpha/m1", "alpha-1", 40)
    _usage_row(hub, "my-app", "alpha/m1", "alpha-2", 60)
    _usage_row(hub, "batch-ocr", "beta/m2", "beta-1", 5)
    entry = entry_of(hub, "beta/m2", "beta-1")
    record = hub.router._register(
        1, entry, app="batch-ocr", model_request="vision", kind="stream", job_id=None, est_in=900
    )
    hub.router._retarget(record, entry, 2, "running")

    payload = (await client.get("/api/live")).json()
    assert payload["generated_at"]
    assert payload["window_min"] == 15
    assert payload["in_flight_total"] == 1

    apps = {row["app"]: row for row in payload["apps"]}
    assert set(apps) == {"my-app", "batch-ocr"}
    # the app with a call in flight leads the strip
    assert payload["apps"][0]["app"] == "batch-ocr"
    call = apps["batch-ocr"]["in_flight"][0]
    assert set(call) == {"call_id", "state", "model", "account", "kind", "elapsed_s", "attempt", "job_id"}
    assert (call["call_id"], call["state"]) == (1, "running")
    assert (call["model"], call["account"], call["kind"], call["attempt"]) == (
        "beta/m2",
        "beta-1",
        "stream",
        2,
    )
    assert call["job_id"] is None
    # both accounts of one model land on one chip
    assert apps["my-app"]["in_flight"] == []
    assert apps["my-app"]["recent"] == [{"model": "alpha/m1", "calls": 2, "out_tokens": 100}]

    hub.router.release(1)


async def test_live_window_ignores_older_rows(client: httpx.AsyncClient, hub: Hub) -> None:
    now = datetime.now(UTC)
    _usage_row(hub, "my-app", "alpha/m1", "alpha-1", 40, ts=to_iso(now - timedelta(minutes=2)))
    _usage_row(hub, "my-app", "alpha/m1", "alpha-1", 40, ts=to_iso(now - timedelta(minutes=40)))

    payload = (await client.get("/api/live?window_min=15")).json()
    assert payload["apps"][0]["recent"] == [{"model": "alpha/m1", "calls": 1, "out_tokens": 40}]

    wide = (await client.get("/api/live?window_min=60")).json()
    assert wide["window_min"] == 60
    assert wide["apps"][0]["recent"] == [{"model": "alpha/m1", "calls": 2, "out_tokens": 80}]


async def test_live_is_empty_when_nothing_ran(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/api/live")).json()
    assert payload["apps"] == []
    assert payload["in_flight_total"] == 0


async def test_status_models_carry_recent_apps_and_in_flight(client: httpx.AsyncClient, hub: Hub) -> None:
    now = datetime.now(UTC)
    _usage_row(hub, "my-app", "alpha/m1", "alpha-1", 40)
    _usage_row(hub, "batch-ocr", "alpha/m1", "alpha-1", 10)
    _usage_row(hub, "scout", "alpha/m1", "alpha-1", 10, ts=to_iso(now - timedelta(hours=2)))
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    record = hub.router._register(
        2, entry, app="my-app", model_request="auto", kind="sync", job_id=None, est_in=0
    )
    hub.router._retarget(record, entry, 1, "running")

    models = (await client.get("/api/status")).json()["models"]
    row = next(item for item in models if item["key"] == "alpha/m1" and item["account"] == "alpha-1")
    assert row["apps_15m"] == ["batch-ocr", "my-app"]
    assert row["in_flight"] == 1

    idle = next(item for item in models if item["key"] == "alpha/m1" and item["account"] == "alpha-2")
    assert idle["apps_15m"] == []
    assert idle["in_flight"] == 0

    hub.router.release(2)


# --- the owner's kill switch ---------------------------------------------------------------


async def slow_call_model(hub: Hub, entry: Any, body: Any, app: str, attempt_no: int, **kw: Any) -> Any:
    await asyncio.sleep(5)
    raise AssertionError("the call should have been cancelled")


async def start_call(client: httpx.AsyncClient, hub: Hub, app: str) -> asyncio.Task[httpx.Response]:
    """One chat request left hanging on a vendor that never answers."""
    body = {"model": "alpha/m1", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    task = asyncio.create_task(client.post("/v1/chat/completions", json=body, headers={"X-Hub-App": app}))
    seen = len(hub.router.in_flight())
    for _ in range(500):
        await asyncio.sleep(0.002)
        if len(hub.router.in_flight()) > seen:
            return task
    raise AssertionError(f"the call never reached the router: {task}")


async def test_cancelling_one_live_call(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "call_model", slow_call_model)
    call = await start_call(client, hub, "my-app")
    call_id = hub.router.in_flight()[0]["call_id"]

    payload = (await client.post(f"/api/live/{call_id}/cancel")).json()
    assert payload["cancelled"] == [call_id]
    assert payload["count"] == 1
    assert (await call).status_code == 503
    assert hub.router.in_flight() == []
    assert "cancel" in [event["kind"] for event in hub.store.events(10)]


async def test_cancelling_a_call_that_already_ended_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/live/999999/cancel")).status_code == 404


async def test_cancelling_one_app_leaves_the_others_alone(
    client: httpx.AsyncClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "call_model", slow_call_model)
    mine = await start_call(client, hub, "my-app")
    theirs = await start_call(client, hub, "batch-ocr")

    payload = (await client.post("/api/live/cancel", json={"app": "my-app"})).json()
    assert payload["count"] == 1
    assert (await mine).status_code == 503
    assert [row["app"] for row in hub.router.in_flight()] == ["batch-ocr"]

    assert (await client.post("/api/live/cancel", json={"all": True})).json()["count"] == 1
    assert (await theirs).status_code == 503
    assert hub.router.in_flight() == []


async def test_a_cancel_needs_a_target(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/live/cancel", json={})).status_code == 422
    assert (await client.post("/api/live/cancel", json={"app": "my-app", "all": True})).status_code == 422


async def test_live_rows_carry_the_call_id_and_state(client: httpx.AsyncClient, hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hub.router._register(77, entry, app="my-app", model_request="auto", kind="sync", job_id=None, est_in=0)
    call = (await client.get("/api/live")).json()["apps"][0]["in_flight"][0]
    assert (call["call_id"], call["state"]) == (77, "waiting")
    hub.router.release(77)


async def test_status_and_usage_do_not_count_an_abandoned_call_as_an_error(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    for status in ("ok", "error", "abandoned"):
        hub.store.start_usage(
            app="my-app",
            provider="alpha",
            account="alpha-1",
            model="alpha/m1",
            status=status,
            latency_ms=5,
            attempt=1,
        )

    models = (await client.get("/api/status")).json()["models"]
    row = next(item for item in models if item["key"] == "alpha/m1" and item["account"] == "alpha-1")
    assert (row["usage_today"]["requests"], row["usage_today"]["errors"]) == (3, 1)

    usage = (await client.get("/api/usage?group_by=app")).json()["rows"]
    assert [(item["bucket"], item["requests"], item["errors"]) for item in usage] == [("my-app", 3, 1)]
