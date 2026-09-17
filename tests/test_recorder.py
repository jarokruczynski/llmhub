from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from llmhub.recorder import MAX_CHARS, MAX_ENTRIES, Recorder

LAN = {"X-Forwarded-For": "192.168.1.44"}
BODY = {"model": "auto", "messages": [{"role": "user", "content": "say ok"}]}
ANSWER = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def note(rec: Recorder, content: str = "say ok", answer: str = "ok", now: datetime | None = None) -> None:
    rec.note(
        app="ytsb",
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

    note_failure(hub, FakeEntry(), BODY, "ytsb", "sync", FakeUpstream())

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

    note_failure(hub, FakeEntry(), BODY, "ytsb", "sync", FakeUpstream())

    assert hub.recorder.dump()["entries"] == []
