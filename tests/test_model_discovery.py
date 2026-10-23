from __future__ import annotations

import httpx
import respx
import yaml

from llmhub.config import CLI_KIND
from llmhub.model_discovery import (
    REASON_VANISHED,
    Listing,
    apply_pass,
    diff_listing,
    discovery_summary,
    new_model_spec,
    plan_pass,
    run_pass,
)
from llmhub.runtime import Hub


def listing(*ids: str) -> Listing:
    return Listing("ok", listed=set(ids), offered=[{"id": model_id, "context": None} for model_id in ids])


def test_diff_splits_new_vanished_and_present() -> None:
    diff = diff_listing(["a", "b", "c"], listing("b", "c", "d"))

    assert [row["id"] for row in diff["new"]] == ["d"]
    assert diff["vanished"] == ["a"]
    assert diff["present"] == ["b", "c"]
    assert diff["suspect"] is False


def test_a_failed_listing_says_nothing_vanished() -> None:
    diff = diff_listing(["a", "b"], Listing("error", error="boom"))

    assert diff == {"new": [], "vanished": [], "present": [], "suspect": False}


def test_a_listing_that_drops_almost_everything_is_suspect() -> None:
    registered = [f"m{n}" for n in range(10)]

    diff = diff_listing(registered, listing("m0", "other"))

    assert len(diff["vanished"]) == 9
    assert diff["suspect"] is True


def test_listed_but_not_offered_still_counts_as_present() -> None:
    found = Listing("ok", listed={"embed-1", "chat-1"}, offered=[{"id": "chat-1", "context": None}])

    diff = diff_listing(["embed-1"], found)

    assert diff["vanished"] == []
    assert [row["id"] for row in diff["new"]] == ["chat-1"]


def test_new_cli_model_routes_with_text_caps_only() -> None:
    spec = new_model_spec(CLI_KIND, "antigravity", {"id": "gemini-4-argon-high"}, "2026-10-23")

    assert spec is not None
    assert spec["caps"] == ["text"]
    assert spec["free"] == {}
    assert "free tier unknown" in spec["notes"]


def test_new_http_model_is_registered_only_when_its_id_says_free() -> None:
    paid = new_model_spec("openai", None, {"id": "vendor/big-model", "context": 32000}, "d")
    free = new_model_spec("openai", None, {"id": "vendor/big-model:free", "context": 32000}, "d")

    assert paid is None
    assert free is not None
    assert free["context"] == 32000


def test_a_local_server_model_is_left_to_the_owner() -> None:
    assert new_model_spec("openai", None, {"id": "gemma4:26b"}, "d") is None


def test_new_model_vetted_by_the_catalog_takes_its_caps() -> None:
    spec = new_model_spec(CLI_KIND, "antigravity", {"id": "gemini-3.8-flash-high"}, "d")

    assert spec is not None
    assert "vision" in spec["caps"]


@respx.mock
async def test_dry_run_writes_nothing(hub: Hub) -> None:
    respx.get("https://beta.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m3:free"}]})
    )
    before = hub.settings.registry_path.read_text(encoding="utf-8")

    result = await run_pass(hub, dry_run=True, provider_filter="beta")

    assert result["dry_run"] is True
    report = result["reports"][0]
    assert report["new"] == ["m3:free"]
    assert report["vanished"] == ["m2"]
    assert hub.settings.registry_path.read_text(encoding="utf-8") == before
    assert hub.store.disabled_models() == set()
    assert hub.store.last_discovery_run() is None


@respx.mock
async def test_apply_registers_free_new_offers_the_rest_and_disables_vanished(hub: Hub) -> None:
    respx.get("https://beta.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m3:free"}, {"id": "pricey"}]})
    )

    reports = await plan_pass(hub, provider_filter="beta")
    applied = apply_pass(hub, reports)

    assert applied["added"] == 1
    assert applied["backup"]
    written = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    ids = [model["id"] for model in written["providers"]["beta"]["models"]]
    assert ids == ["m2", "m3:free"]
    assert written["providers"]["beta"]["models"][1]["free"] == {}
    disabled = hub.store.disabled_models()
    assert disabled == {"beta/m2"}
    rows = hub.store.model_listing_all()
    assert rows["beta/m2"]["disabled_reason"] == REASON_VANISHED
    assert rows["beta/pricey"]["offered"] == 1
    assert hub.registry.entry("beta/m3:free") is not None
    summary = discovery_summary(hub)
    assert [row["key"] for row in summary["new_models"]] == ["beta/m3:free"]
    assert [row["key"] for row in summary["vanished"]] == ["beta/m2"]
    assert summary["offered_count"] == 1
    messages = [event["message"] for event in hub.store.events() if event["kind"] == "discovery"]
    assert any("new model available on beta: m3:free" in message for message in messages)
    assert any("not registered: pricey" in message for message in messages)


@respx.mock
async def test_an_offered_model_is_announced_once_and_dropped_when_unlisted(hub: Hub) -> None:
    route = respx.get("https://beta.test/v1/models")
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "m2"}, {"id": "pricey"}]}))
    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))
    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))
    messages = [event["message"] for event in hub.store.events() if "pricey" in event["message"]]
    assert len(messages) == 1

    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "m2"}]}))
    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))

    assert "beta/pricey" not in hub.store.model_listing_all()


@respx.mock
async def test_a_model_that_comes_back_is_re_enabled_only_if_discovery_disabled_it(hub: Hub) -> None:
    route = respx.get("https://beta.test/v1/models")
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "other"}]}))
    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))
    assert "beta/m2" in hub.store.disabled_models()

    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "m2"}, {"id": "other"}]}))
    reports = await plan_pass(hub, provider_filter="beta")
    assert reports[0]["returned"] == ["m2"]
    apply_pass(hub, reports)

    assert "beta/m2" not in hub.store.disabled_models()
    assert hub.store.model_listing_all()["beta/m2"]["vanished_at"] is None


@respx.mock
async def test_an_owner_override_survives_vanish_and_return(hub: Hub) -> None:
    hub.store.set_model_disabled("beta/m2", True, "owner")
    route = respx.get("https://beta.test/v1/models")
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "other"}]}))
    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "m2"}, {"id": "other"}]}))

    apply_pass(hub, await plan_pass(hub, provider_filter="beta"))

    assert "beta/m2" in hub.store.disabled_models()


@respx.mock
async def test_a_failing_listing_changes_nothing(hub: Hub) -> None:
    respx.get("https://beta.test/v1/models").mock(return_value=httpx.Response(500, text="down"))

    reports = await plan_pass(hub, provider_filter="beta")
    apply_pass(hub, reports)

    assert reports[0]["status"] == "error"
    assert hub.store.disabled_models() == set()


@respx.mock
async def test_listing_is_the_union_of_every_account(hub: Hub) -> None:
    respx.get("https://alpha.test/v1/models").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"id": "m1"}]}),
            httpx.Response(200, json={"data": [{"id": "paid1"}]}),
        ]
    )

    reports = await plan_pass(hub, provider_filter="alpha")

    assert reports[0]["vanished"] == []
    assert reports[0]["new"] == []


async def test_free_windows_of_a_registered_model_are_never_rewritten(hub: Hub) -> None:
    reports = [
        {
            "provider": "alpha",
            "kind": "openai",
            "status": "ok",
            "error": None,
            "listed": 2,
            "registered": 2,
            "present": ["m1", "paid1"],
            "new": [],
            "offered": [],
            "vanished": [],
            "returned": [],
            "suspect": False,
        }
    ]
    before = hub.settings.registry_path.read_text(encoding="utf-8")

    applied = apply_pass(hub, reports)

    assert applied["added"] == 0
    assert hub.settings.registry_path.read_text(encoding="utf-8") == before


async def test_discovery_endpoints(client: httpx.AsyncClient, hub: Hub) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://beta.test/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "m2"}, {"id": "m4-free"}]})
        )
        mock.route(host="127.0.0.1").pass_through()
        dry = await client.post("/api/discovery/run", params={"dry_run": "true", "provider": "beta"})
    assert dry.status_code == 200
    assert dry.json()["reports"][0]["new"] == ["m4-free"]

    status = await client.get("/api/discovery")
    assert status.status_code == 200
    assert status.json()["last_run_at"] is None

    unknown = await client.post("/api/discovery/run", params={"provider": "nope"})
    assert unknown.status_code == 404
