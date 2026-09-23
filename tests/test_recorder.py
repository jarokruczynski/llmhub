from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from llmhub.recorder import MAX_CHARS, MAX_ENTRIES, Recorder

LAN = {"X-Forwarded-For": "192.168.1.44"}
BODY = {"model": "auto", "messages": [{"role": "user", "content": "say ok"}]}
ANSWER = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def note(rec: Recorder, content: str = "say ok", answer: str = "ok", now: datetime | None = None) -> None:
    rec.note(
        app="app-a",
        model_request="auto",
        entry_key="alpha/m1",
        account="alpha-1",
        kind="sync",
        body={"model": "auto", "messages": [{"role": "user", "content": content}]},
        payload={"choices": [{"message": {"role": "assistant", "content": answer}}]},
        status="ok",
        latency_ms=12,
        now=now,
    )


def test_nothing_is_kept_until_someone_turns_it_on() -> None:
    rec = Recorder()

    note(rec)

    assert rec.status()["recording"] is False
    assert rec.dump()["entries"] == []


def test_starting_again_clears_the_previous_run() -> None:
    rec = Recorder()
    rec.start(20)
    note(rec, content="first")
    assert rec.status()["count"] == 1

    rec.start(20)

    assert rec.status()["count"] == 0
    assert rec.dump()["entries"] == []


def test_the_window_closes_on_its_own() -> None:
    rec = Recorder()
    now = datetime.now(UTC)
    rec.start(20, now=now)

    later = now + timedelta(minutes=21)
    note(rec, content="too late", now=later)

    assert rec.recording(later) is False
    assert rec.status(later)["count"] == 0
    assert rec.status(later)["seconds_left"] == 0


def test_stopping_early_keeps_what_was_caught() -> None:
    rec = Recorder()
    rec.start(20)
    note(rec, content="kept")

    rec.stop()

    assert rec.status()["recording"] is False
    assert rec.status()["count"] == 1
    note(rec, content="after stop")
    assert rec.status()["count"] == 1


def test_the_ring_keeps_the_newest_and_counts_what_it_dropped() -> None:
    rec = Recorder()
    rec.start(20)

    for i in range(MAX_ENTRIES + 5):
        note(rec, content=f"call {i}")

    status = rec.status()
    assert status["count"] == MAX_ENTRIES
    assert status["dropped"] == 5
    assert "call 204" in rec.dump()["entries"][0]["prompt"]


def test_one_huge_transcript_cannot_own_the_buffer() -> None:
    rec = Recorder()
    rec.start(20)

    note(rec, content="x" * (MAX_CHARS * 4))

    entry = rec.dump()["entries"][0]
    assert entry["truncated"] is True
    assert len(entry["prompt"]) < MAX_CHARS * 2


def test_the_newest_call_is_first() -> None:
    rec = Recorder()
    rec.start(20)
    note(rec, content="older")
    note(rec, content="newer")

    prompts = [row["prompt"] for row in rec.dump()["entries"]]

    assert "newer" in prompts[0]
    assert "older" in prompts[1]


async def test_reading_the_transcript_needs_a_token_from_the_lan(client: httpx.AsyncClient) -> None:
    # every other read is deliberately open on the LAN; this one returns the text itself
    assert (await client.get("/api/status", headers=LAN)).status_code == 200

    assert (await client.get("/api/recorder", headers=LAN)).status_code == 401
    assert (await client.post("/api/recorder/start", headers=LAN, json={})).status_code == 401


async def test_the_console_can_arm_and_read_it_from_the_machine(client: httpx.AsyncClient) -> None:
    started = await client.post("/api/recorder/start", json={"minutes": 5})
    assert started.status_code == 200
    body = started.json()
    assert body["recording"] is True
    assert 0 < body["seconds_left"] <= 5 * 60

    dump = await client.get("/api/recorder")
    assert dump.status_code == 200
    assert dump.json()["entries"] == []

    stopped = await client.post("/api/recorder/stop")
    assert stopped.status_code == 200
    assert stopped.json()["recording"] is False


class FakeClassification:
    kind = "quota"
    code = "429"
    message = "insufficient_quota"


class FakeUpstream(Exception):
    classification = FakeClassification()
    status_code = 429
    latency_ms = 31


def test_a_refused_attempt_is_recorded_not_dropped() -> None:
    from llmhub.recorder import note_failure

    class FakeHub:
        pass

    class FakeEntry:
        key = "alpha/m1"
        account_id = "alpha-1"

    hub = FakeHub()
    hub.recorder = Recorder()
    hub.recorder.start(20)

    note_failure(hub, FakeEntry(), BODY, "app-a", "sync", FakeUpstream())

    entry = hub.recorder.dump()["entries"][0]
    assert entry["status"] == "quota 429"
    assert "insufficient_quota" in entry["answer"]
    assert "say ok" in entry["prompt"]
    assert entry["latency_ms"] == 31


def test_a_refusal_is_ignored_while_the_recorder_is_off() -> None:
    from llmhub.recorder import note_failure

    class FakeHub:
        pass

    class FakeEntry:
        key = "alpha/m1"
        account_id = "alpha-1"

    hub = FakeHub()
    hub.recorder = Recorder()

    note_failure(hub, FakeEntry(), BODY, "app-a", "sync", FakeUpstream())

    assert hub.recorder.dump()["entries"] == []


def test_an_attempt_is_visible_while_the_model_is_still_thinking() -> None:
    rec = Recorder()
    rec.start(20)

    opened = rec.begin(
        app="app-a", model_request="auto", entry_key="alpha/m1", account="alpha-1", kind="sync", body=BODY
    )

    assert opened is not None
    pending = rec.dump()["entries"][0]
    assert pending["status"] == "pending"
    assert pending["ts"] is None
    assert pending["messages"] == [{"role": "user", "text": "say ok"}]

    rec.finish(opened, status="ok", answer="ok", latency_ms=40, tokens={"in": 3, "out": 1, "total": 4})

    done = rec.dump()["entries"][0]
    assert done["status"] == "ok"
    assert done["ts"] is not None
    assert done["latency_ms"] == 40
    assert done["tokens"]["out"] == 1


def test_a_poll_with_since_returns_only_what_changed() -> None:
    rec = Recorder()
    rec.start(20)
    note(rec, content="first")
    seen = rec.status()["rev"]

    assert rec.dump(since=seen)["entries"] == []

    note(rec, content="second")

    changed = rec.dump(since=seen)["entries"]
    assert [row["messages"][0]["text"] for row in changed] == ["second"]


def test_usage_comes_from_the_vendor_and_is_estimated_without_it() -> None:
    rec = Recorder()
    rec.start(20)
    rec.note(
        app="app-a",
        model_request="auto",
        entry_key="alpha/m1",
        account="alpha-1",
        kind="sync",
        body=BODY,
        payload={**ANSWER, "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}},
        status="ok",
        latency_ms=12,
    )
    note(rec)

    estimated, billed = rec.dump()["entries"]
    assert billed["tokens"] == {"in": 11, "out": 5, "total": 16, "cached": 0, "reasoning": 0}
    assert estimated["tokens"]["estimated"] is True


def test_a_long_message_keeps_its_head_and_its_tail() -> None:
    rec = Recorder()
    rec.start(20)

    note(rec, content="HEAD" + "x" * (MAX_CHARS * 2) + "TAIL")

    text = rec.dump()["entries"][0]["messages"][0]["text"]
    assert text.startswith("HEAD")
    assert text.endswith("TAIL")


ALPHA_URL = "https://alpha.test/v1/chat/completions"
BETA_URL = "https://beta.test/v1/chat/completions"
CHAT = {**BODY, "max_tokens": 64}
OK_PAYLOAD = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}


@respx.mock
async def test_a_fallback_reads_as_one_request_with_several_answers(client: httpx.AsyncClient, hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"code": "insufficient_quota", "message": "no quota"}}
        )
    )
    respx.post(BETA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    hub.recorder.start(5)

    response = await client.post("/v1/chat/completions", json=CHAT, headers={"X-Hub-App": "app-a"})

    assert response.status_code == 200
    rows = list(reversed(hub.recorder.dump()["entries"]))
    assert len({row["request_id"] for row in rows}) == 1
    assert [row["attempt"] for row in rows] == [1, 2, 3]
    assert [row["status"].split()[0] for row in rows] == ["quota", "quota", "ok"]
    assert rows[-1]["answer"] == "hi"
    assert rows[-1]["tokens"]["in"] == 11


@respx.mock
async def test_a_streamed_answer_is_recorded_once_the_stream_ends(client: httpx.AsyncClient, hub) -> None:
    sse = (
        b'data: {"id":"1","choices":[{"delta":{"content":"he"},"index":0}]}\n\n'
        b'data: {"id":"1","choices":[{"delta":{"content":"llo"},"index":0}]}\n\n'
        b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
    )
    hub.recorder.start(5)

    async with client.stream(
        "POST", "/v1/chat/completions", json=dict(CHAT, stream=True), headers={"X-Hub-App": "app-a"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    row = hub.recorder.dump()["entries"][0]
    assert row["status"] == "ok"
    assert row["answer"] == "hello"
    assert row["tokens"]["out"] == 3
    assert row["first_ms"] is not None


def test_a_request_shows_while_it_waits_for_a_slot_and_splits_the_wait() -> None:
    from llmhub.recorder import begin_call, begin_request

    class FakeHub:
        pass

    class FakeEntry:
        key = "alpha/m1"
        account_id = "alpha-1"

    hub = FakeHub()
    hub.recorder = Recorder()
    hub.recorder.start(20)

    queued = begin_request(hub, BODY, "app-a", "sync", request_id=7)
    row = hub.recorder.dump()["entries"][0]
    assert row["status"] == "queued"
    assert row["model"] == ""

    opened = begin_call(hub, FakeEntry(), BODY, "app-a", "sync", request_id=7, attempt=1, queued=queued)

    assert opened is queued
    assert hub.recorder.status()["count"] == 1
    row = hub.recorder.dump()["entries"][0]
    assert row["status"] == "pending"
    assert row["model"] == "alpha/m1"
    assert row["started_at"] >= row["sent_at"]


def test_a_request_that_never_reached_a_vendor_says_why() -> None:
    from llmhub.recorder import begin_request, close_request

    class FakeHub:
        pass

    class NoCandidates(Exception):
        attempts = [{"model": "alpha/m1", "status": "slot_wait", "error_code": "slot_queue_full"}]

    hub = FakeHub()
    hub.recorder = Recorder()
    hub.recorder.start(20)
    queued = begin_request(hub, BODY, "app-a", "sync", request_id=8)

    close_request(hub, queued, NoCandidates("all candidates failed"))

    row = hub.recorder.dump()["entries"][0]
    assert row["status"] == "not sent"
    assert "slot_queue_full" in row["answer"]


@respx.mock
async def test_a_request_with_no_candidate_is_recorded(client: httpx.AsyncClient, hub) -> None:
    hub.recorder.start(5)

    response = await client.post(
        "/v1/chat/completions", json=dict(CHAT, model="no-such/model"), headers={"X-Hub-App": "app-a"}
    )

    assert response.status_code >= 400
    rows = hub.recorder.dump()["entries"]
    assert [row["status"] for row in rows] == ["not sent"]
