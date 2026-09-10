from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx
import yaml

from llmhub.config import example_registry_path, load_registry
from llmhub.runtime import Hub

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
REASON = "paraphrases the input instead of quoting it verbatim"
BETA_URL = "https://beta.test/v1/chat/completions"

BODY = {"model": "auto", "messages": [{"role": "user", "content": "hello there"}], "max_tokens": 64}


def keys(selection) -> list[tuple[str, str]]:
    return [(entry.key, entry.account_id) for entry in selection.candidates]


def models(selection) -> list[str]:
    return list(dict.fromkeys(entry.key for entry in selection.candidates))


def strict(hub: Hub) -> None:
    """Turn the spread off so an assertion reads the sort key and not the LRU rotation."""
    hub.registry.aliases["auto"].spread = 1


def with_reasoning(hub: Hub) -> None:
    hub.registry.providers["alpha"].models[0].caps = ["text", "tools", "reasoning"]


def record_latency(hub: Hub, account: str, model: str, latency_ms: int) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider=model.split("/")[0],
        account=account,
        model=model,
        status="ok",
        latency_ms=latency_ms,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=1, out_tokens=1)


# --- constraints: alias and headers ---------------------------------------------------


def test_alias_and_header_constraints_combine_to_the_stricter_one(hub: Hub) -> None:
    alias = hub.registry.aliases["auto"]
    alias.min_context = 10000
    alias.avoid = ["reasoning"]
    alias.max_latency_ms = 30000
    selection = hub.router.select(
        model_request="auto",
        min_context=24000,
        avoid=["vision"],
        max_latency_ms=5000,
        now=NOW,
    )
    assert selection.constraints == {
        "min_context": 24000,
        "avoid": ["reasoning", "vision"],
        "max_latency_ms": 5000,
    }


def test_the_alias_wins_when_it_is_the_stricter_side(hub: Hub) -> None:
    alias = hub.registry.aliases["auto"]
    alias.min_context = 50000
    alias.max_latency_ms = 10000
    selection = hub.router.select(model_request="auto", min_context=1000, max_latency_ms=90000, now=NOW)
    assert selection.constraints["min_context"] == 50000
    assert selection.constraints["max_latency_ms"] == 10000


def test_no_constraints_anywhere_reports_none(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    assert selection.constraints == {"min_context": None, "avoid": [], "max_latency_ms": None}


# --- min_context: a requirement, not a preference -------------------------------------


def test_min_context_rejects_an_unknown_context(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", min_context=24000, now=NOW)
    assert models(selection) == ["alpha/m1"]  # the only entry that declares a context
    rejected = [row for row in selection.rejected if row["reason"] == "context_unknown"]
    assert {row["model"] for row in rejected} >= {"beta/m2", "gamma/fixed", "gamma/openended"}


def test_min_context_rejects_a_context_that_is_too_small(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", min_context=200000, now=NOW)
    assert selection.candidates == []
    row = next(item for item in selection.rejected if item["reason"] == "context_small")
    assert row["model"] == "alpha/m1"
    assert row["detail"] == 100000


def test_min_context_from_the_alias_applies_without_a_header(hub: Hub) -> None:
    hub.registry.aliases["auto"].min_context = 24000
    selection = hub.router.select(model_request="auto", now=NOW)
    assert models(selection) == ["alpha/m1"]
    assert selection.constraints["min_context"] == 24000


# --- avoid and max_latency_ms: ordering only ------------------------------------------


def test_an_avoided_cap_sorts_below_the_rest_of_the_pool(hub: Hub) -> None:
    strict(hub)
    with_reasoning(hub)
    before = keys(hub.router.select(model_request="auto", now=NOW))
    after = keys(hub.router.select(model_request="auto", avoid=["reasoning"], now=NOW))
    assert before[0] == ("alpha/m1", "alpha-1")
    assert after[0] == ("beta/m2", "beta-1")
    # ordering, never a filter: the same pool, in a different order
    assert sorted(after) == sorted(before)


def test_a_model_slower_than_max_latency_sorts_below_an_unmeasured_one(hub: Hub) -> None:
    strict(hub)
    record_latency(hub, "alpha-1", "alpha/m1", 45000)
    record_latency(hub, "alpha-2", "alpha/m1", 45000)
    selection = hub.router.select(model_request="auto", max_latency_ms=30000, now=NOW)
    assert keys(selection)[0] == ("beta/m2", "beta-1")
    assert ("alpha/m1", "alpha-1") in keys(selection)


def test_a_model_inside_max_latency_keeps_its_place(hub: Hub) -> None:
    strict(hub)
    record_latency(hub, "alpha-1", "alpha/m1", 900)
    record_latency(hub, "alpha-2", "alpha/m1", 900)
    selection = hub.router.select(model_request="auto", max_latency_ms=30000, now=NOW)
    assert keys(selection)[0] == ("alpha/m1", "alpha-1")


def test_both_penalties_stack_below_a_single_one(hub: Hub) -> None:
    strict(hub)
    with_reasoning(hub)
    hub.registry.providers["beta"].models[0].caps = ["text", "vision", "reasoning"]
    record_latency(hub, "alpha-1", "alpha/m1", 45000)
    record_latency(hub, "alpha-2", "alpha/m1", 45000)
    selection = hub.router.select(model_request="auto", avoid=["reasoning"], max_latency_ms=30000, now=NOW)
    # gamma carries neither penalty, beta/m2 the avoided cap, alpha/m1 that plus the latency
    ordered = models(selection)
    assert ordered.index("gamma/fixed") < ordered.index("beta/m2") < ordered.index("alpha/m1")


def test_avoiding_everything_never_empties_the_pool(hub: Hub) -> None:
    plain = hub.router.select(model_request="auto", now=NOW)
    avoided = hub.router.select(model_request="auto", avoid=["text"], now=NOW)
    assert sorted(keys(avoided)) == sorted(keys(plain))
    assert "avoid" not in avoided.rejected_reasons()


def test_header_prefer_beats_the_penalty(hub: Hub) -> None:
    strict(hub)
    with_reasoning(hub)
    selection = hub.router.select(model_request="auto", prefer=["alpha/m1"], avoid=["reasoning"], now=NOW)
    assert keys(selection)[0] == ("alpha/m1", "alpha-1")


def test_an_avoided_alias_entry_sorts_below_an_unnamed_one(hub: Hub) -> None:
    strict(hub)
    with_reasoning(hub)
    # gamma/fixed is on nobody's prefer list; alpha/m1 heads the alias one and is avoided
    selection = hub.router.select(model_request="auto", avoid=["reasoning"], now=NOW)
    ordered = models(selection)
    assert ordered.index("gamma/fixed") < ordered.index("alpha/m1")


# --- per-app bans ---------------------------------------------------------------------


def test_a_ban_keeps_the_model_out_of_that_apps_pool(hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    selection = hub.router.select(model_request="auto", app="batch-ocr", now=NOW)
    assert "alpha/m1" not in models(selection)
    row = next(item for item in selection.rejected if item["reason"] == "app_banned")
    assert row["model"] == "alpha/m1"
    assert row["detail"] == REASON


def test_a_ban_binds_one_app_only(hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    other = hub.router.select(model_request="auto", app="my-app", now=NOW)
    assert "alpha/m1" in models(other)
    assert "app_banned" not in other.rejected_reasons()


def test_unbanning_re_admits_the_model(hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    assert hub.store.unban_model("batch-ocr", "alpha/m1") is True
    selection = hub.router.select(model_request="auto", app="batch-ocr", now=NOW)
    assert "alpha/m1" in models(selection)
    assert hub.store.bans_for("batch-ocr") == []


def test_a_second_ban_of_the_same_pair_replaces_the_reason(hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    hub.store.ban_model("batch-ocr", "alpha/m1", "ignored the json schema three times running")
    rows = hub.store.bans_for("batch-ocr")
    assert len(rows) == 1
    assert rows[0]["reason"] == "ignored the json schema three times running"


def test_bans_all_groups_by_app(hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    hub.store.ban_model("batch-ocr", "beta/m2", "truncates long inputs without saying so")
    hub.store.ban_model("my-app", "beta/m2", "returns prose around the json")
    grouped = hub.store.bans_all()
    assert sorted(grouped) == ["batch-ocr", "my-app"]
    assert len(grouped["batch-ocr"]) == 2


# --- ban API --------------------------------------------------------------------------


async def test_ban_round_trip_over_the_api(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post("/api/apps/batch-ocr/bans", json={"model": "alpha/m1", "reason": REASON})
    assert created.status_code == 201
    assert created.json()["model"] == "alpha/m1"
    assert created.json()["reason"] == REASON

    listed = (await client.get("/api/apps/batch-ocr/bans")).json()
    assert [row["model"] for row in listed["bans"]] == ["alpha/m1"]
    assert any(event["message"] == f"batch-ocr banned alpha/m1: {REASON}" for event in hub.store.events())

    removed = await client.delete("/api/apps/batch-ocr/bans/alpha/m1")
    assert removed.status_code == 200
    assert removed.json()["banned"] is False
    assert (await client.get("/api/apps/batch-ocr/bans")).json()["bans"] == []
    assert any(event["message"] == "batch-ocr unbanned alpha/m1" for event in hub.store.events())


async def test_a_ban_without_a_reason_is_422(client: httpx.AsyncClient) -> None:
    answer = await client.post("/api/apps/batch-ocr/bans", json={"model": "alpha/m1"})
    assert answer.status_code == 422

    short = await client.post("/api/apps/batch-ocr/bans", json={"model": "alpha/m1", "reason": "bad"})
    assert short.status_code == 422
    assert "reason" in short.text


async def test_banning_an_unknown_model_is_404(client: httpx.AsyncClient) -> None:
    answer = await client.post("/api/apps/batch-ocr/bans", json={"model": "nope/nope", "reason": REASON})
    assert answer.status_code == 404


async def test_unbanning_something_that_is_not_banned_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/api/apps/batch-ocr/bans/alpha/m1")).status_code == 404


async def test_the_apps_endpoint_carries_the_ban_count(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    rows = (await client.get("/api/apps")).json()["apps"]
    row = next(item for item in rows if item["app"] == "batch-ocr")
    assert row["bans"] == 1


# --- the wire ------------------------------------------------------------------------


@respx.mock
async def test_a_banned_model_is_skipped_on_a_sync_call(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    respx.post(BETA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )
    )
    answer = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "batch-ocr"})
    assert answer.status_code == 200
    assert answer.headers["x-hub-model"] == "beta/m2"


@respx.mock
async def test_a_job_from_a_banned_app_skips_the_model_too(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.store.ban_model("batch-ocr", "alpha/m1", REASON)
    respx.post(BETA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-job",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )
    )
    created = await client.post(
        "/jobs",
        json={
            "app": "batch-ocr",
            "model": "auto",
            "request": {"messages": [{"role": "user", "content": "extract"}], "max_tokens": 32},
        },
    )
    job_id = created.json()["id"]
    await hub.jobs.run_once()
    done = (await client.get(f"/jobs/{job_id}")).json()
    assert done["state"] == "done"
    assert done["served_by"] == "beta/m2#beta-1"


async def test_the_429_body_echoes_the_constraints(client: httpx.AsyncClient) -> None:
    answer = await client.post(
        "/v1/chat/completions",
        json=BODY,
        headers={
            "X-Hub-App": "batch-ocr",
            "X-Hub-Min-Context": "24000000",
            "X-Hub-Avoid": "reasoning",
            "X-Hub-Max-Latency-Ms": "30000",
        },
    )
    assert answer.status_code == 429
    error = answer.json()["error"]
    assert error["constraints"] == {
        "min_context": 24000000,
        "avoid": ["reasoning"],
        "max_latency_ms": 30000,
    }
    assert error["rejected"]
    assert {row["reason"] for row in error["rejected"]} == {"context_unknown", "context_small"}


async def test_an_app_banning_everything_gets_a_429_naming_its_own_bans(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    for key in hub.registry.keys():
        hub.store.ban_model("batch-ocr", key, REASON)
    answer = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "batch-ocr"})
    assert answer.status_code == 429
    assert {row["reason"] for row in answer.json()["error"]["rejected"]} == {"app_banned"}


# --- the shipped example --------------------------------------------------------------


def test_the_example_registry_still_loads_with_the_extract_alias() -> None:
    registry = load_registry(example_registry_path())
    alias = registry.aliases["extract"]
    assert alias.min_context == 24000
    assert alias.avoid == ["reasoning"]
    assert alias.max_latency_ms == 30000
    assert alias.require == ["text", "json"]
    assert alias.prefer[0] == "gemini/gemini-3.5-flash-lite"


def test_the_example_extract_alias_is_valid_yaml_of_the_documented_shape() -> None:
    raw = yaml.safe_load(example_registry_path().read_text(encoding="utf-8"))["aliases"]["extract"]
    assert set(raw) == {"spread", "require", "min_context", "avoid", "max_latency_ms", "prefer"}
