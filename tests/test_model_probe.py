from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llmhub.config import Settings
from llmhub.model_probe import ModelProber, account_level
from llmhub.runtime import Hub
from llmhub.store import to_iso

REGISTRY = """
providers:
  wide:
    kind: openai
    base_url: https://wide.test/v1
    accounts:
      - {id: wide-1, api_key_env: WIDE_KEY}
    models:
      - {id: a, caps: [text], free: {}}
      - {id: b, caps: [text], free: {}}
      - {id: c, caps: [text], free: {}}
      - {id: d, caps: [text], free: {}}
      - {id: e, caps: [text], free: {}}
      - {id: paid, caps: [text]}
  other:
    kind: openai
    base_url: https://other.test/v1
    accounts:
      - {id: other-1, api_key_env: OTHER_KEY}
    models:
      - {id: x, caps: [text], free: {}}
      - {id: y, caps: [text], free: {}}
"""


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
    monkeypatch.setenv("OTHER_KEY", "k")
    monkeypatch.setenv("LLMHUB_MODEL_PROBE_PARALLEL", "1")
    instance = Hub.create(Settings.from_env())
    instance.router.retry_delays = ()
    yield instance
    instance.store.close()


def prober_for(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict]
) -> tuple[ModelProber, list[str]]:
    """A prober whose vendor answers from `answers` by model key; unknown keys succeed."""
    probed: list[str] = []

    async def fake_probe(_hub: Hub, entry, prompt: str = "ping") -> dict:
        probed.append(entry.key)
        return answers.get(entry.key, {"ok": True, "status": "ok", "latency_ms": 1})

    monkeypatch.setattr("llmhub.api.probe_entry", fake_probe)
    prober = ModelProber(hub)
    hub.settings = Settings(**{**hub.settings.__dict__, "model_probe_gap_s": 0.0})
    return prober, probed


def test_balance_text_and_401_name_the_account_but_a_plain_403_does_not() -> None:
    assert account_level({"ok": False, "status": "auth", "http_status": 401})
    assert account_level({"ok": False, "status": "error", "http_status": 402})
    assert account_level(
        {"ok": False, "status": "not_found", "http_status": 403, "error": "Not enough balance"}
    )
    assert not account_level(
        {"ok": False, "status": "not_found", "http_status": 403, "error": "no permission"}
    )
    assert not account_level({"ok": False, "status": "not_found", "http_status": 404, "error": "model gone"})
    assert not account_level({"ok": True})


async def test_every_free_model_of_every_account_is_probed_once(hub: Hub, monkeypatch) -> None:
    prober, probed = prober_for(hub, monkeypatch, {})

    run = await prober.run_once()

    assert sorted(probed) == ["other/x", "other/y", "wide/a", "wide/b", "wide/c", "wide/d", "wide/e"]
    assert run.probed == 7 and run.ok == 7
    assert run.skipped.get("not_free") == 1
    # proven for the window: a second run asks nobody
    again = await prober.run_once()
    assert again.probed == 0


async def test_refusing_the_whole_account_parks_the_rest_after_three_models(hub: Hub, monkeypatch) -> None:
    refused = {
        "ok": False,
        "status": "not_found",
        "http_status": 402,
        "error": "insufficient balance",
        "error_code": "402",
    }
    prober, probed = prober_for(hub, monkeypatch, {f"wide/{m}": refused for m in "abcde"})

    run = await prober.run_once()

    wide = [key for key in probed if key.startswith("wide/")]
    assert len(wide) == 3
    assert run.accounts_parked == ["wide-1"]
    now = datetime.now(UTC)
    parked = hub.store.unavailable_all(now)
    assert {key for (acc, key) in parked if acc == "wide-1"} == {"wide/d", "wide/e"}
    # the other account is untouched
    assert ("other-1", "other/x") not in parked


async def test_one_model_refusing_alone_does_not_park_its_siblings(hub: Hub, monkeypatch) -> None:
    gone = {
        "ok": False,
        "status": "not_found",
        "http_status": 404,
        "error": "model gone",
        "error_code": "404",
    }
    prober, probed = prober_for(hub, monkeypatch, {"wide/a": gone})

    run = await prober.run_once()

    assert run.accounts_parked == []
    assert len([k for k in probed if k.startswith("wide/")]) == 5


async def test_a_success_between_refusals_resets_the_account_verdict(hub: Hub, monkeypatch) -> None:
    refused = {"ok": False, "status": "auth", "http_status": 401, "error": "bad key", "error_code": "401"}
    prober, probed = prober_for(hub, monkeypatch, {"wide/a": refused, "wide/b": refused, "wide/d": refused})

    run = await prober.run_once()

    assert run.accounts_parked == []
    assert len([k for k in probed if k.startswith("wide/")]) == 5


async def test_a_failed_pair_is_due_again_when_its_park_expires(hub: Hub, monkeypatch) -> None:
    prober, _ = prober_for(hub, monkeypatch, {})
    now = datetime.now(UTC)
    until = now + timedelta(days=2)
    hub.store.mark_unavailable("other-1", "other/x", "not_found", "404", "gone", until)

    entries, skipped = prober.due_entries(now)

    assert "other/x" not in {e.key for e in entries}
    assert skipped.get("parked") == 1
    row = hub.store.model_probes_all()[("other-1", "other/x")]
    assert row["next_due_ts"] == to_iso(until)

    later, _ = prober.due_entries(until + timedelta(minutes=1))
    assert "other/x" in {e.key for e in later}


async def test_real_traffic_inside_the_window_replaces_the_probe(hub: Hub, monkeypatch) -> None:
    prober, probed = prober_for(hub, monkeypatch, {})
    hub.store.start_usage(
        app="real",
        provider="other",
        account="other-1",
        model="other/x",
        status="ok",
        latency_ms=1,
        attempt=1,
        ts=to_iso(datetime.now(UTC) - timedelta(minutes=10)),
    )

    run = await prober.run_once()

    assert "other/x" not in probed
    assert run.skipped.get("recently_served") == 1


async def test_a_run_stops_at_its_budget_and_the_next_one_continues(hub: Hub, monkeypatch) -> None:
    prober, probed = prober_for(hub, monkeypatch, {})
    hub.settings = Settings(**{**hub.settings.__dict__, "model_probe_max_per_run": 3})

    run = await prober.run_once()

    assert run.probed == 3
    assert run.skipped.get("run_budget", 0) >= 1
    # the next run picks up what the budget left, oldest first
    second = await prober.run_once()
    assert second.probed == 3
    assert len(set(probed)) == 6


async def test_missing_key_and_disabled_models_are_not_probed(hub: Hub, monkeypatch) -> None:
    monkeypatch.delenv("OTHER_KEY")
    prober, probed = prober_for(hub, monkeypatch, {})

    run = await prober.run_once()

    assert not any(key.startswith("other/") for key in probed)
    assert run.skipped.get("missing_api_key") == 2


async def test_the_api_reports_status_and_starts_a_run(hub: Hub, monkeypatch) -> None:
    import httpx

    from llmhub.app import create_app

    app = create_app(hub=hub)
    prober, _ = prober_for(hub, monkeypatch, {})
    hub.model_prober = prober
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8800") as client:
        status = (await client.get("/api/health/models")).json()
        assert status["running"] is False and status["tracked_pairs"] == 0
        started = (await client.post("/api/health/models?force=true")).json()
        assert started["started"] is True
        await prober._run_task
        done = (await client.get("/api/health/models")).json()
        assert done["last"]["probed"] == 7
