from __future__ import annotations

import json

import httpx
import respx

from llmhub.runtime import Hub

ALPHA_URL = "https://alpha.test/v1/chat/completions"
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
    assert cancelled.json() == {"id": job_id, "state": "cancelled", "cancelled": True}
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


async def test_orphaned_running_job_is_requeued_on_start(client: httpx.AsyncClient, hub: Hub) -> None:
    job_id = (await client.post("/jobs", json=JOB_BODY)).json()["id"]
    hub.store.update_job(job_id, state="running")
    hub.jobs.requeue_orphans()
    assert hub.store.job(job_id)["state"] == "queued"
