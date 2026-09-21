from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from llmhub.jobs import park_at, parks_the_job, utcnow
from llmhub.runtime import Hub
from llmhub.store import now_iso, to_iso

from .conftest import entry_of

ALPHA_URL = "https://alpha.test/v1/chat/completions"
BETA_URL = "https://beta.test/v1/chat/completions"
GAMMA_URL = "https://gamma.test/v1/chat/completions"
CALLBACK_URL = "https://callback.test/done"

OK_PAYLOAD = {
    "id": "chatcmpl-job",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}

JOB_BODY = {
    "app": "batch-ocr",
    "model": "auto",
    "priority": 3,
    "request": {"messages": [{"role": "user", "content": "summarize"}], "max_tokens": 32},
    "callback_url": CALLBACK_URL,
}


async def test_job_waits_for_quota_then_runs(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post("/jobs", json=JOB_BODY)
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert created.json()["state"] == "queued"

    for entry in hub.registry.entries():
        if entry.model.is_free:
            hub.quota.mark_exhausted(entry, "insufficient_quota")

    await hub.jobs.run_once()
    waiting = (await client.get(f"/jobs/{job_id}")).json()
    assert waiting["state"] == "waiting_quota"
    assert waiting["next_window_at"]

    with respx.mock:
        respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
        callback = respx.post(CALLBACK_URL).mock(return_value=httpx.Response(204))
        for key in ("alpha/m1", "beta/m2"):
            await client.post(f"/api/models/{key}/forgive")
        await hub.jobs.run_once()

        done = (await client.get(f"/jobs/{job_id}")).json()
        assert done["state"] == "done"
        assert json.loads(done["result"])["id"] == "chatcmpl-job"
        assert done["served_by"] == "alpha/m1#alpha-1"
        assert callback.called


async def test_waiting_job_records_remaining_out(client: httpx.AsyncClient, hub: Hub) -> None:
    for account, out_tokens in (("alpha-1", 900), ("alpha-2", 500)):
        usage_id = hub.store.start_usage(
            app="test",
            provider="alpha",
            account=account,
            model="alpha/m1",
            status="ok",
            latency_ms=1,
            attempt=1,
        )
        hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=out_tokens)

    body = dict(
        JOB_BODY,
        model="alpha/m1",
        callback_url=None,
        request={"messages": [{"role": "user", "content": "summarize"}], "max_tokens": 900},
    )
    job_id = (await client.post("/jobs", json=body)).json()["id"]
    await hub.jobs.run_once()

    row = (await client.get(f"/jobs/{job_id}")).json()
    assert row["state"] == "waiting_quota"
    note = json.loads(row["error"])
    assert note["remaining_out"] == 500
    assert note["next_window_at"] == row["next_window_at"]


async def test_job_survives_callback_failure(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post("/jobs", json=JOB_BODY)
    job_id = created.json()["id"]
    with respx.mock:
        respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
        respx.post(CALLBACK_URL).mock(side_effect=httpx.ConnectError("refused"))
        await hub.jobs.run_once()
    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "done"


async def test_paused_app_job_is_not_claimed(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post("/jobs", json=JOB_BODY)
    job_id = created.json()["id"]
    await client.post("/api/apps/batch-ocr/pause")
    with respx.mock:
        route = respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
        await hub.jobs.run_once()
        assert not route.called
    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "queued"


async def test_job_listing_and_cancel(client: httpx.AsyncClient) -> None:
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    listing = await client.get("/jobs", params={"app": "batch-ocr"})
    assert [job["id"] for job in listing.json()["jobs"]] == [job_id]

    cancelled = await client.delete(f"/jobs/{job_id}")
    assert cancelled.json() == {
        "id": job_id,
        "state": "cancelled",
        "cancelled": True,
        # queued, so there was no worker to stop
        "stopped_worker": False,
    }
    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "cancelled"

    api_view = await client.get("/api/jobs", params={"state": "cancelled"})
    assert api_view.json()["jobs"][0]["id"] == job_id


async def test_job_with_unknown_model_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/jobs", json=dict(JOB_BODY, model="ghost/model"))
    assert response.status_code == 400


async def test_job_failure_is_terminal(client: httpx.AsyncClient, hub: Hub) -> None:
    # one provider: a rejected request is now walked across the pool, so a job pinned to a
    # single vendor is what makes the failure terminal rather than a fallback
    job_id = (await client.post("/jobs", json=dict(JOB_BODY, model="alpha/m1"))).json()["id"]
    with respx.mock:
        respx.post(ALPHA_URL).mock(
            return_value=httpx.Response(400, json={"error": {"message": "bad", "code": "invalid_request"}})
        )
        await hub.jobs.run_once()
    job = (await client.get(f"/jobs/{job_id}")).json()
    assert job["state"] == "failed"
    assert "invalid_request" in (job["error"] or "") or "bad" in (job["error"] or "")


async def test_pool_wide_transient_refusal_parks_instead_of_failing(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    # every candidate answered 503: the pool said nothing about the request, so the job is
    # parked and retried. Failing it here is what turned a minute of vendor trouble into a
    # dead job, and the contract promises the opposite.
    job_id = (await client.post("/jobs", json=dict(JOB_BODY, model="alpha/m1"))).json()["id"]
    with respx.mock:
        respx.post(ALPHA_URL).mock(
            return_value=httpx.Response(503, json={"error": {"message": "overloaded"}})
        )
        await hub.jobs.run_once()
    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "waiting_quota"


async def test_parked_job_sleeps_instead_of_spinning(client: httpx.AsyncClient, hub: Hub) -> None:
    # a park with a window but no backoff was claimable on the very next poll, so one job
    # could burn ten thousand attempts inside its six-hour life. Every park carries a clock.
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    for entry in hub.registry.entries():
        if entry.model.is_free:
            hub.quota.mark_exhausted(entry, "insufficient_quota")
    await hub.jobs.run_once()

    parked = hub.store.job(job_id)
    assert parked["state"] == "waiting_quota"
    assert parked["next_attempt_at"]
    assert parked["next_attempt_at"] > now_iso()
    assert not hub.store.claimable_jobs(now_iso())
    # a second pass changes nothing: the job is asleep, not re-attempted
    await hub.jobs.run_once()
    assert hub.store.job(job_id)["attempts"] == parked["attempts"]


async def test_forgive_wakes_a_parked_job(client: httpx.AsyncClient, hub: Hub) -> None:
    # the backoff is a guess about the pool; forgiving a model says the guess is wrong, so
    # the parked job has to be reconsidered on the next poll rather than sleeping it out
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    for entry in hub.registry.entries():
        if entry.model.is_free:
            hub.quota.mark_exhausted(entry, "insufficient_quota")
    await hub.jobs.run_once()
    assert hub.store.job(job_id)["next_attempt_at"]

    forgiven = await client.post("/api/models/alpha/m1/forgive")
    assert forgiven.json()["woken"] == 1
    assert hub.store.job(job_id)["next_attempt_at"] is None


async def test_park_at_takes_the_window_when_it_opens_first() -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    # the backoff is 60s on the first attempt; a window 10s out beats it, one an hour out does not
    assert park_at(now, 1, to_iso(now + timedelta(seconds=10))) == now + timedelta(seconds=10)
    assert park_at(now, 1, to_iso(now + timedelta(hours=1))) == now + timedelta(seconds=60)
    # a window already past does not put the clock behind us, and a missing one is just the backoff
    assert park_at(now, 1, to_iso(now - timedelta(hours=1))) == now
    assert park_at(now, 1, None) == now + timedelta(seconds=60)


async def test_pool_wide_auth_refusal_still_fails(client: httpx.AsyncClient, hub: Hub) -> None:
    # nothing but "this key is not accepted" is not a minute of trouble: there is no working
    # key for the request, and a retry in thirty minutes meets the same wall
    job_id = (await client.post("/jobs", json=dict(JOB_BODY, model="alpha/m1"))).json()["id"]
    with respx.mock:
        respx.post(ALPHA_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "invalid api key", "code": "invalid_api_key"}}
            )
        )
        await hub.jobs.run_once()
    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "failed"


def test_parks_the_job_draws_the_line_at_auth_alone() -> None:
    def attempts(*statuses: str) -> list[dict[str, str]]:
        return [{"status": status} for status in statuses]

    # one dead key among candidates that were merely pacing must not take the job down
    assert parks_the_job(attempts("retry", "auth"))
    assert parks_the_job(attempts("auth", "quota"))
    # but authentication on its own is not something waiting fixes
    assert not parks_the_job(attempts("auth"))
    assert not parks_the_job(attempts("auth", "auth"))
    # and a hard verdict anywhere in the list still fails, auth or no auth
    assert not parks_the_job(attempts("retry", "too_large"))
    assert not parks_the_job(attempts("retry", "auth", "error"))
    # the statuses that were already transient are unchanged, and an empty run still parks
    assert parks_the_job(attempts("retry"))
    assert parks_the_job(attempts("quota"))
    assert parks_the_job([])


async def test_dead_key_is_parked_and_reported_even_though_the_job_survives(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    # parking the job makes the dead key quiet, so the router has to make it loud: the pair
    # leaves the pool and says why, instead of the failure being visible only as a dead job
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    with respx.mock:
        respx.post(ALPHA_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "invalid api key", "code": "invalid_api_key"}}
            )
        )
        slow_down = httpx.Response(429, json={"error": {"message": "slow down"}})
        respx.post(BETA_URL).mock(return_value=slow_down)
        respx.post(GAMMA_URL).mock(return_value=slow_down)
        await hub.jobs.run_once()

    assert (await client.get(f"/jobs/{job_id}")).json()["state"] == "waiting_quota"
    assert hub.router.unavailable_until(entry_of(hub, "alpha/m1", "alpha-1"), utcnow())
    parked = [row for row in hub.store.events(limit=200) if row["kind"] == "unavailable"]
    assert any(row["model"] == "alpha/m1" and "auth" in row["message"] for row in parked)


async def test_orphaned_running_job_is_requeued_on_start(client: httpx.AsyncClient, hub: Hub) -> None:
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    hub.store.update_job(job_id, state="running")
    hub.jobs.requeue_orphans()
    assert hub.store.job(job_id)["state"] == "queued"
