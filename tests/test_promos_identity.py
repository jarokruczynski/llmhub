from __future__ import annotations

from typing import Any

import httpx
import respx
import yaml

from llmhub.accounts import default_account_id
from llmhub.config import Registry
from llmhub.promo_identity import dedupe_promos, identify, provider_for_base_url
from llmhub.runtime import Hub
from llmhub.store import Store


def post(store: Store, provider: str, **fields: Any) -> dict[str, Any]:
    row, _ = store.upsert_promo(
        identity=identify(provider, fields.get("url"), fields.get("base_url"), store).key,
        provider=provider,
        **fields,
    )
    return row


# --- resolution ---------------------------------------------------------------------


def test_resolves_via_catalog_template(hub: Hub) -> None:
    identity = identify("OpenRouter :free", "https://openrouter.ai/api/v1/models", None, hub.store)
    assert (identity.key, identity.kind) == ("openrouter", "template")


def test_resolves_via_registry_host(hub: Hub) -> None:
    # 'alpha' is in the test registry and nowhere in the provider catalog
    identity = identify("Alpha launch credits", "https://alpha.test/promo", None, hub.store, hub.registry)
    assert (identity.key, identity.kind) == ("alpha", "registry")


def test_falls_back_to_the_provider_name_and_learns_nothing(hub: Hub) -> None:
    identity = identify("Some Unknown Vendor", None, None, hub.store, hub.registry)
    assert (identity.key, identity.kind) == ("some-unknown-vendor", "fallback")
    assert hub.store.promo_aliases() == []


def test_a_shared_host_never_names_the_vendor(hub: Hub) -> None:
    identity = identify("Freebuff CLI", "https://github.com/CodebuffAI/freebuff", None, hub.store)
    assert identity.kind == "fallback"


def test_resolution_is_learned_and_reused(hub: Hub) -> None:
    first = identify("Groq", "https://console.groq.com/docs/rate-limits", None, hub.store)
    assert first.kind == "template"
    aliases = {row["alias"]: row["key"] for row in hub.store.promo_aliases()}
    assert aliases == {"name:groq": "groq", "host:groq.com": "groq"}

    # the same host under a name the catalog cannot read now short-circuits to the same key
    again = identify("free tokens newsletter", "https://console.groq.com/promo", None, hub.store)
    assert (again.key, again.kind) == ("groq", "alias")


# --- merging ------------------------------------------------------------------------


def test_merge_keeps_the_oldest_row_and_appends_the_note(hub: Hub) -> None:
    first = post(hub.store, "Groq", url="https://groq.test/a", note="14400 rpd", source="pepper.pl")
    second = post(hub.store, "groq cloud free tier", note="UPDATE: 1000 rpd now", source="scout")

    assert second["id"] == first["id"]
    assert second["found_at"] == first["found_at"]
    assert second["updates_count"] == 1
    assert second["updated_at"] is not None
    assert second["note"].splitlines()[0] == "14400 rpd"
    assert second["note"].splitlines()[1].endswith(" scout] 1000 rpd now")
    assert "UPDATE:" not in second["note"]
    assert len(hub.store.promos()) == 1


def test_merge_keeps_the_url_and_fills_in_what_was_missing(hub: Hub) -> None:
    post(hub.store, "Groq", url="https://groq.test/a")
    merged = post(
        hub.store,
        "Groq",
        url="https://groq.test/b",
        base_url="https://api.groq.com/openai/v1",
        expires_at="2026-12-31",
    )
    assert merged["url"] == "https://groq.test/a"
    assert merged["base_url"] == "https://api.groq.com/openai/v1"
    assert merged["expires_at"] == "2026-12-31"


def test_status_only_moves_forward(hub: Hub) -> None:
    post(hub.store, "Groq", status="used")
    assert post(hub.store, "Groq", status="new")["status"] == "used"
    assert post(hub.store, "Groq", status="known")["status"] == "used"

    post(hub.store, "Cerebras", status="new")
    assert post(hub.store, "Cerebras", status="known")["status"] == "known"
    assert post(hub.store, "Cerebras", status="expired")["status"] == "expired"


# --- migration ----------------------------------------------------------------------


FIXTURE_ROWS: tuple[tuple[str, str | None, str, str], ...] = (
    ("OpenRouter :free", "https://openrouter.ai/api/v1/models", "300 free calls", "2026-09-01"),
    ("Google Gemini API", "https://ai.google.dev/gemini-api/docs/pricing", "free tier", "2026-09-02"),
    ("Groq", "https://console.groq.com/docs/rate-limits", "14400 rpd", "2026-09-03"),
    ("OpenRouter :free", "https://openrouter.ai/api/v1/models", "UPDATE: 50 calls", "2026-09-07"),
    ("openrouter free models", None, "still free", "2026-09-08"),
    (
        "Google Gemini API",
        "https://ai.google.dev/gemini-api/docs/rate-limits",
        "UPDATE: 100 rpd",
        "2026-09-08",
    ),
    ("LOCAL IBM Granite 4.2-30B", "https://ollama.com/library/granite4.2", "30b moe", "2026-09-07"),
    ("LOCAL IBM Granite 4.2-30B", "https://ollama.com/library/granite4.2", "UPDATE: q4", "2026-09-08"),
)


def seed_fixture(store: Store) -> None:
    for provider, url, note, day in FIXTURE_ROWS:
        store.add_promo(provider, url, note, "new", "promo-hunt", found_at=f"{day}T08:00:00+00:00")


def test_migration_collapses_the_duplicate_groups(hub: Hub) -> None:
    seed_fixture(hub.store)
    assert len(hub.store.promos()) == 8

    outcome = dedupe_promos(hub.store, hub.registry)

    assert (outcome["before"], outcome["after"]) == (8, 4)
    keys = {row["identity"] for row in hub.store.promos()}
    assert keys == {"openrouter", "gemini", "groq", "local-ibm-granite-4-2-30b"}
    openrouter = next(row for row in hub.store.promos() if row["identity"] == "openrouter")
    assert openrouter["id"] == 1 and openrouter["updates_count"] == 2
    # the notes of the merged rows carry the day they were found, in order
    assert [line[:11] for line in openrouter["note"].splitlines()] == [
        "300 free ca",
        "[2026-09-07",
        "[2026-09-08",
    ]
    assert "UPDATE:" not in openrouter["note"]


def test_migration_dry_run_writes_nothing(hub: Hub) -> None:
    seed_fixture(hub.store)
    outcome = dedupe_promos(hub.store, hub.registry, dry_run=True)
    assert (outcome["before"], outcome["after"]) == (8, 4)
    assert len(hub.store.promos()) == 8
    assert hub.store.promo_aliases() == []


# --- api ----------------------------------------------------------------------------


async def test_post_merges_a_known_provider(client: httpx.AsyncClient, hub: Hub) -> None:
    created = await client.post(
        "/api/promos", json={"provider": "Groq", "url": "https://console.groq.com/x", "note": "14400 rpd"}
    )
    assert created.status_code == 201
    assert created.json()["created"] is True and created.json()["merged_into"] is None

    merged = await client.post("/api/promos", json={"provider": "groqcloud", "note": "UPDATE: 1000 rpd"})
    assert merged.status_code == 200
    body = merged.json()
    assert body["created"] is False
    assert body["merged_into"] == created.json()["id"]
    assert body["note"].splitlines()[-1].endswith("] 1000 rpd")
    assert len((await client.get("/api/promos")).json()["promos"]) == 1


async def test_post_marks_a_registered_provider_used(client: httpx.AsyncClient) -> None:
    row = (await client.post("/api/promos", json={"provider": "alpha", "note": "free tier"})).json()
    assert row["status"] == "used"
    assert row["account_key"] == "alpha/alpha-1"


async def test_duplicates_view_lists_the_groups(client: httpx.AsyncClient, hub: Hub) -> None:
    seed_fixture(hub.store)
    dedupe_promos(hub.store, hub.registry)
    body = (await client.get("/api/promos", params={"duplicates": 1})).json()
    assert body["duplicates"] == []

    hub.store.add_promo("Groq", None, "second row", "new", "manual", identity="openrouter")
    groups = (await client.get("/api/promos", params={"duplicates": 1})).json()["duplicates"]
    assert groups[0]["identity"] == "openrouter" and groups[0]["count"] == 2


# --- quick add ----------------------------------------------------------------------


def test_quick_add_reuses_a_provider_that_already_calls_the_host(hub: Hub) -> None:
    assert provider_for_base_url(hub.registry, "https://alpha.test/v1") == "alpha"
    assert provider_for_base_url(hub.registry, "https://unknown.test/v1") is None


def test_two_providers_on_one_host_are_left_alone() -> None:
    registry = Registry.model_validate(
        {
            "providers": {
                "one": {"base_url": "https://shared.test/v1", "accounts": [], "models": []},
                "two": {"base_url": "https://shared.test/openai/v1", "accounts": [], "models": []},
            }
        }
    )
    assert provider_for_base_url(registry, "https://shared.test/v1") is None


@respx.mock
async def test_quick_add_reuses_the_registered_provider_for_that_host(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    # a vendor already in the registry under a name nobody would guess from its endpoint
    registry_path = hub.settings.registry_path
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    raw["providers"]["vx-cloud"] = {
        "kind": "openai",
        "base_url": "https://api.vendorx.test/v1",
        "accounts": [],
        "models": [],
    }
    registry_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert (await client.post("/api/registry/reload")).status_code == 200

    respx.get("https://api.vendorx.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vx-large"}]})
    )
    respx.post("https://api.vendorx.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404, json={"error": "no such endpoint"}))

    body = (
        await client.post(
            "/api/accounts/quick",
            json={"api_key": "sk-vx", "source": "free credits at https://blog.vendorx.test/launch"},
        )
    ).json()

    assert body["provider"] == "vx-cloud"
    assert body["provider_reused"] is True
    assert body["created_provider"] is False
    written = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["providers"]
    assert "vendorx" not in written
    assert [account["id"] for account in written["vx-cloud"]["accounts"]] == [default_account_id("vx-cloud")]
    assert hub.store.promo_alias("host:vendorx.test") == "vx-cloud"
