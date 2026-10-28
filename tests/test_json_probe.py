from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llmhub.config import Settings
from llmhub.model_probe import JSON_CAP, ModelProber, json_answer_ok, json_verdict
from llmhub.runtime import Hub
from llmhub.status import model_rows
from llmhub.store import to_iso

REGISTRY = """
providers:
  wide:
    kind: openai
    base_url: https://wide.test/v1
    accounts:
      - {id: wide-1, api_key_env: WIDE_KEY}
    models:
      - {id: plain, caps: [text], free: {}}
      - {id: prose, caps: [text], free: {}}
      - {id: listed, caps: [text, json], free: {}}
      - {id: optin, caps: [text], free: {}, opt_in_only: true}
"""

GOOD = '{"answer": "yes"}'


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hub]:
    home = tmp_path / "home"
    home.mkdir()
    (home / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("LLMHUB_HOME", str(home))
    monkeypatch.setenv("LLMHUB_REGISTRY", str(home / "providers.yaml"))
    monkeypatch.setenv("LLMHUB_DB", str(home / "hub.db"))
    monkeypatch.setenv("LLMHUB_ENV_DIR", str(env_dir))
    monkeypatch.setenv("WIDE_KEY", "k")
    monkeypatch.setenv("LLMHUB_MODEL_PROBE_PARALLEL", "1")
    instance = Hub.create(Settings.from_env())
    instance.router.retry_delays = ()
    instance.settings = Settings(**{**instance.settings.__dict__, "model_probe_gap_s": 0.0})
    yield instance
    instance.store.close()


def prober_for(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, json_answers: dict[str, dict]
) -> tuple[ModelProber, list[str], list[str]]:
    """Liveness probes always succeed; json probes answer from `json_answers`, default GOOD."""
    live: list[str] = []
    asked: list[str] = []

    async def fake_probe(_hub: Hub, entry, prompt: str = "ping", body: dict | None = None) -> dict:
        if body is None:
            live.append(entry.key)
            return {"ok": True, "status": "ok", "latency_ms": 1}
        asked.append(entry.key)
        assert body["response_format"]["type"] == "json_schema"
        assert body["max_tokens"] == 64
        return json_answers.get(entry.key, {"ok": True, "status": "ok", "content": GOOD})

    monkeypatch.setattr("llmhub.api.probe_entry", fake_probe)
    return ModelProber(hub), live, asked


def learned(hub: Hub) -> dict[str, set[str]]:
    return {model: caps for (_account, model), caps in hub.store.learned_caps_all().items()}


def test_only_a_bare_object_the_schema_accepts_counts() -> None:
    assert json_answer_ok('{"answer": "yes"}')
    assert json_answer_ok(' {"answer":"No"}\n')
    assert not json_answer_ok("Yes.")
    assert not json_answer_ok('```json\n{"answer": "yes"}\n```')
    assert not json_answer_ok('{"answer": "maybe"}')
    assert not json_answer_ok('{"answer": "yes", "why": "sky"}')
    assert not json_answer_ok('{"answer": "ye')


def test_only_answers_and_refusals_of_the_format_are_verdicts() -> None:
    assert json_verdict({"ok": True, "content": GOOD}) == (True, "ok")
    assert json_verdict({"ok": True, "content": "Yes"}) == (False, "not_json")
    assert json_verdict({"ok": False, "status": "truncated"}) == (False, "truncated")
    assert json_verdict({"ok": False, "status": "error", "http_status": 400}) == (False, "rejected")
    assert json_verdict({"ok": False, "status": "quota", "http_status": 429}) == (None, "quota")
    assert json_verdict({"ok": False, "status": "timeout"}) == (None, "timeout")


async def test_a_text_only_pair_that_answers_in_json_is_learned_as_json(hub: Hub, monkeypatch) -> None:
    prober, live, asked = prober_for(hub, monkeypatch, {"wide/prose": {"ok": True, "content": "Yes."}})

    run = await prober.run_once()

    # the catalog already says json for `listed`, and opt-in-only is kept out of automatic traffic
    assert sorted(asked) == ["wide/plain", "wide/prose"]
    assert run.json_probed == 2 and run.json_ok == 1
    assert learned(hub) == {"wide/plain": {JSON_CAP}}
    # a second run within the verdict window asks nobody
    again = await prober.run_once()
    assert again.json_probed == 0


async def test_the_router_and_the_model_list_count_a_learned_json_cap(hub: Hub, monkeypatch) -> None:
    prober, _live, _asked = prober_for(hub, monkeypatch, {"wide/prose": {"ok": True, "content": "Yes."}})
    before = hub.router.select(model_request="wide/plain", require=[JSON_CAP])
    assert not before.candidates

    await prober.run_once()

    after = hub.router.select(model_request="wide/plain", require=[JSON_CAP])
    assert [entry.key for entry in after.candidates] == ["wide/plain"]
    refused = hub.router.select(model_request="wide/prose", require=[JSON_CAP])
    assert not refused.candidates
    # a failed probe never takes away a cap the catalog wrote down
    listed = hub.router.select(model_request="wide/listed", require=[JSON_CAP])
    assert [entry.key for entry in listed.candidates] == ["wide/listed"]

    rows = {row["key"]: row for row in model_rows(hub, datetime.now(UTC))}
    assert JSON_CAP in rows["wide/plain"]["caps"]
    assert rows["wide/plain"]["learned_caps"] == [JSON_CAP]
    assert JSON_CAP not in rows["wide/prose"]["caps"]


async def test_a_pair_proven_by_traffic_still_gets_its_json_probe(hub: Hub, monkeypatch) -> None:
    prober, live, asked = prober_for(hub, monkeypatch, {})
    hub.store.start_usage(
        app="real",
        provider="wide",
        account="wide-1",
        model="wide/plain",
        status="ok",
        latency_ms=1,
        attempt=1,
        ts=to_iso(datetime.now(UTC) - timedelta(minutes=10)),
    )

    await prober.run_once()

    assert "wide/plain" not in live
    assert "wide/plain" in asked
    assert JSON_CAP in learned(hub)["wide/plain"]


async def test_a_call_without_a_verdict_keeps_the_old_one_and_comes_back_soon(hub: Hub, monkeypatch) -> None:
    prober, _live, _asked = prober_for(hub, monkeypatch, {})
    await prober.run_once()
    assert JSON_CAP in learned(hub)["wide/plain"]

    later = datetime.now(UTC) + timedelta(days=8)
    hub.store.record_learned_cap(
        "wide-1", "wide/plain", JSON_CAP, ok=None, status="quota", ts=to_iso(later), next_due=to_iso(later)
    )

    assert JSON_CAP in learned(hub)["wide/plain"]
    row = hub.store.learned_caps_rows(JSON_CAP)[("wide-1", "wide/plain")]
    assert row["status"] == "quota" and row["ok"] == 1


async def test_a_pair_that_rejected_response_format_loses_the_learned_cap(hub: Hub, monkeypatch) -> None:
    prober, _live, _asked = prober_for(hub, monkeypatch, {})
    await prober.run_once()
    assert JSON_CAP in learned(hub)["wide/plain"]

    hub.store.record_unsupported_param("wide-1", "wide/plain", "response_format")

    assert "wide/plain" not in learned(hub)
    assert not hub.router.select(model_request="wide/plain", require=[JSON_CAP]).candidates


async def test_the_json_probe_can_be_turned_off(hub: Hub, monkeypatch) -> None:
    prober, live, asked = prober_for(hub, monkeypatch, {})
    hub.settings = Settings(**{**hub.settings.__dict__, "model_probe_json_every_h": 0})

    await prober.run_once()

    assert live and not asked
