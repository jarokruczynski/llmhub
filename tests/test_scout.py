from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from llmhub.config import Settings
from llmhub.runtime import Hub
from llmhub.scout.apply import apply_decisions, build_report
from llmhub.scout.collect import collect, html_to_text, strip_tags
from llmhub.scout.curate import curate
from llmhub.scout.extract import extract_offers
from llmhub.scout.llm import BudgetExhausted, HubLLM, LLMError, parse_first_json_object
from llmhub.scout.runner import ScoutBusy, ScoutService, curate_prefer, next_run_at
from llmhub.scout.sources import Source, load_sources, resolve_sources

HUB_URL = "http://127.0.0.1:8800/v1/chat/completions"
PAGE_URL = "https://src.test/a"
OTHER_URL = "https://src.test/b"

SOURCES_YAML = f"""
pepper:
  enabled: false
  keywords: []
catalog_docs: false
openrouter_free: false
pages:
- {PAGE_URL}
feeds: []
search:
  enabled: false
  queries: []
"""

OFFER_JSON = {
    "offers": [
        {
            "provider": "groq",
            "url": "https://console.groq.com/docs/rate-limits",
            "base_url": "https://api.groq.com/openai/v1",
            "what_is_free": "free tier on all hosted models",
            "limits": "14400 requests per day",
            "expires_at": None,
            "vision": False,
            "tools": True,
            "friction": "",
            "evidence_quote": "Free tier: 14,400 requests per day",
        }
    ]
}

DECISION_JSON = {
    "decisions": [
        {
            "action": "new",
            "row": {
                "provider": "groq",
                "url": "https://console.groq.com/docs/rate-limits",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
                "expires_at": None,
                "note": "free tier, 14400 rpd",
            },
            "reason": "vendor docs, quoted limits",
        }
    ]
}


def chat_response(content: Any, model: str = "alpha/m1", in_tokens: int = 100, out_tokens: int = 20):
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-scout",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": body}}],
            "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens},
        },
        headers={"x-hub-model": model},
    )


def hub_side_effect(extract_body: Any, curate_body: Any):
    """The extract and curate stages share one endpoint; the prompt says which stage it is."""

    def handler(request: httpx.Request) -> httpx.Response:
        text = request.content.decode()
        if "Catalog template ids" in text:
            return chat_response(curate_body, model="explabs/gpt-6-astra", in_tokens=400, out_tokens=90)
        return chat_response(extract_body, model="zai/glm-4.5-flash")

    return handler


@pytest.fixture
def sources_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "scout_sources.yaml"
    path.write_text(SOURCES_YAML, encoding="utf-8")
    monkeypatch.setenv("LLMHUB_SCOUT_SOURCES", str(path))
    return path


@pytest.fixture
def scout_hub(hub: Hub, sources_file: Path) -> Hub:
    hub.settings = Settings.from_env()
    hub.scout = ScoutService(hub)
    return hub


# --- sources -----------------------------------------------------------------------


def test_sources_autocopy_and_expand(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "scout_sources.yaml"
    config = load_sources(path)
    assert path.is_file()

    assert len(config.pepper.keywords) == 14
    assert "GLM" in config.pepper.keywords and "darmowe tokeny" in config.pepper.keywords
    assert config.catalog_docs is True
    assert config.openrouter_free is True
    assert config.feeds == []
    assert len(config.search.queries) == 6

    sources = resolve_sources(config, environ={})
    kinds = {source.kind for source in sources}
    assert kinds == {"page", "openrouter_free"}
    pepper = [source for source in sources if source.id.startswith("pepper-")]
    assert len(pepper) == 14
    assert pepper[0].url.startswith("https://www.pepper.pl/search?q=")
    assert any(source.id == "docs-groq" for source in sources)
    # loading again must not overwrite the copy the owner edited
    path.write_text("catalog_docs: false\nopenrouter_free: false\n", encoding="utf-8")
    assert load_sources(path).catalog_docs is False


def test_search_sources_only_with_env(tmp_path: Path) -> None:
    config = load_sources(tmp_path / "scout_sources.yaml")
    assert not [source for source in resolve_sources(config, environ={}) if source.kind == "search"]

    with_keys = resolve_sources(
        config,
        environ={"BRAVE_SEARCH_API_KEY": "brave-key", "LLMHUB_SEARXNG_URL": "http://searx.test/"},
    )
    engines = {source.engine for source in with_keys if source.kind == "search"}
    assert engines == {"brave", "searxng"}
    brave = next(source for source in with_keys if source.engine == "brave")
    assert brave.secret == "brave-key"


# --- collect -----------------------------------------------------------------------


def test_strip_tags_drops_script_and_keeps_text() -> None:
    html = (
        "<html><head><style>a{}</style></head><body><p>Free tier</p><script>x=1</script>"
        "<p>14400 rpd</p></body></html>"
    )
    text = strip_tags(html)
    assert "Free tier" in text and "14400 rpd" in text
    assert "x=1" not in text and "a{}" not in text
    assert html_to_text(html).strip()


async def test_collect_skips_unchanged_and_survives_a_bad_source(hub: Hub) -> None:
    sources = [
        Source(id="good", kind="page", url=PAGE_URL),
        Source(id="broken", kind="page", url=OTHER_URL),
    ]
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>free tier</p>"))
        respx.get(OTHER_URL).mock(return_value=httpx.Response(500, text="boom"))
        first = await collect(hub.store, hub.client, sources)

        assert [page.status for page in first.pages] == ["changed", "error"]
        assert first.fetched == 1
        assert len(first.errors) == 1 and "broken" in first.errors[0]

        second = await collect(hub.store, hub.client, sources)
        assert second.changed == []
        assert [page.status for page in second.pages] == ["unchanged", "error"]

        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>free tier now 20k rpd</p>"))
        third = await collect(hub.store, hub.client, sources)
        assert [page.url for page in third.changed] == [PAGE_URL]

    row = hub.store.scout_page(PAGE_URL)
    assert row["status"] == "ok" and "20k rpd" in row["text"]


async def test_collect_openrouter_free_filter(hub: Hub) -> None:
    payload = {
        "data": [
            {"id": "vendor/free-1", "name": "Free 1", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "vendor/paid", "name": "Paid", "pricing": {"prompt": "0.5", "completion": "1"}},
        ]
    }
    source = Source(id="openrouter-free", kind="openrouter_free", url="https://openrouter.test/models")
    with respx.mock:
        respx.get("https://openrouter.test/models").mock(return_value=httpx.Response(200, json=payload))
        result = await collect(hub.store, hub.client, [source])
    text = result.pages[0].text
    assert "vendor/free-1" in text and "vendor/paid" not in text
    assert result.pages[0].url.endswith("#free")


async def test_polite_source_skips_fetch_inside_the_interval_then_refetches_after(hub: Hub) -> None:
    polite_source = Source(id="polite", kind="page", url=PAGE_URL, polite=True)
    hub.store.save_scout_page(PAGE_URL, "abc123", "cached free tier text", status="ok")

    with respx.mock:
        # no route registered for PAGE_URL: a real fetch attempt here would raise
        result = await collect(hub.store, hub.client, [polite_source])
    assert result.pages[0].status == "unchanged"
    assert result.pages[0].text == "cached free tier text"

    stale_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    hub.store.execute("UPDATE scout_pages SET fetched_at = ? WHERE url = ?", (stale_at, PAGE_URL))
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>fresh free tier</p>"))
        refreshed = await collect(hub.store, hub.client, [polite_source])
    assert refreshed.pages[0].status == "changed"
    assert "fresh free tier" in refreshed.pages[0].text


# --- llm ---------------------------------------------------------------------------


def test_parse_first_json_object_handles_fences_and_prose() -> None:
    assert parse_first_json_object('```json\n{"offers": []}\n```') == {"offers": []}
    assert parse_first_json_object('Sure! {"a": {"b": 1}} hope that helps') == {"a": {"b": 1}}
    assert parse_first_json_object('{"q": "} not the end"}') == {"q": "} not the end"}


async def test_llm_retries_once_on_unparseable_answer() -> None:
    answers = [chat_response("no json here, sorry"), chat_response(OFFER_JSON)]
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(side_effect=answers)
            reply = await llm.json_call([{"role": "user", "content": "go"}])
        assert route.call_count == 2
        assert reply.data == OFFER_JSON
        retry_body = json.loads(route.calls[1].request.content)
        assert retry_body["response_format"] == {"type": "json_object"}
        assert "not valid JSON" in retry_body["messages"][-1]["content"]
        assert route.calls[0].request.headers["x-hub-app"] == "scout"
        assert "x-hub-allow-paid" not in route.calls[0].request.headers


async def test_chat_sends_no_temperature_unless_the_caller_passes_one() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(return_value=chat_response("ok"))
            await llm.chat([{"role": "user", "content": "go"}])
    sent = json.loads(route.calls[0].request.content)
    assert "temperature" not in sent


async def test_llm_prefer_header_and_headers() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1", token="secret")
        with respx.mock:
            route = respx.post(HUB_URL).mock(return_value=chat_response(DECISION_JSON))
            await llm.json_call([{"role": "user", "content": "go"}], model="auto", prefer="explabs/x")
        headers = route.calls[0].request.headers
        assert headers["x-hub-prefer"] == "explabs/x"
        assert headers["authorization"] == "Bearer secret"


async def test_budget_guard_stops_after_two_429() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(
                return_value=httpx.Response(429, json={"error": {"type": "no_candidates"}})
            )
            with pytest.raises(LLMError) as first:
                await llm.chat([{"role": "user", "content": "go"}])
            assert not isinstance(first.value, BudgetExhausted)
            with pytest.raises(BudgetExhausted):
                await llm.chat([{"role": "user", "content": "go"}])


async def test_budget_guard_counter_resets_on_success() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(
                side_effect=[
                    httpx.Response(429, json={}),
                    chat_response(OFFER_JSON),
                    httpx.Response(429, json={}),
                ]
            )
            with pytest.raises(LLMError):
                await llm.chat([{"role": "user", "content": "go"}])
            await llm.chat([{"role": "user", "content": "go"}])
            with pytest.raises(LLMError) as third:
                await llm.chat([{"role": "user", "content": "go"}])
            assert not isinstance(third.value, BudgetExhausted)


# --- extract -----------------------------------------------------------------------


async def test_extract_parses_prose_json_and_drops_invalid_offers() -> None:
    payload = {
        "offers": [
            OFFER_JSON["offers"][0],
            {"provider": "", "evidence_quote": "x"},
            {"provider": "rumour-vendor", "evidence_quote": ""},
        ]
    }
    pages = [type("P", (), {"url": PAGE_URL, "text": "free tier page"})()]
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(
                return_value=chat_response("Here you go:\n```json\n" + json.dumps(payload) + "\n```")
            )
            result = await extract_offers(llm, pages)  # type: ignore[arg-type]
    assert len(result.offers) == 1
    assert result.offers[0]["provider"] == "groq"
    assert result.offers[0]["source_url"] == PAGE_URL
    assert len(result.dropped) == 2
    assert result.usage.models == ["alpha/m1"]
    assert result.usage.as_tokens() == {"in": 100, "out": 20}


async def test_extract_stops_on_budget_exhausted() -> None:
    pages = [type("P", (), {"url": f"https://src.test/{n}", "text": "text"})() for n in range(4)]
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(return_value=httpx.Response(429, json={}))
            result = await extract_offers(llm, pages)  # type: ignore[arg-type]
    assert route.call_count == 2
    assert result.stopped is not None
    assert result.offers == []


async def test_extract_caps_page_text_before_the_prompt() -> None:
    # a pepper listing page runs long; "z" never appears in the prompt template itself, so
    # counting it in the outbound content proves the truncation and nothing else
    long_page = type("P", (), {"url": PAGE_URL, "text": "z" * 20000})()
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(return_value=chat_response({"offers": []}))
            await extract_offers(llm, [long_page], max_page_chars=5000)  # type: ignore[arg-type]
    sent = json.loads(route.calls[0].request.content)
    prompt = sent["messages"][-1]["content"]
    assert prompt.count("z") == 5000


# --- curate ------------------------------------------------------------------------


async def test_curate_drops_invalid_decisions() -> None:
    payload = {
        "decisions": [
            DECISION_JSON["decisions"][0],
            {"action": "update", "row": {"provider": "x"}},
            {"action": "new", "row": {"provider": "y", "base_url": "not-a-url"}},
            {"action": "invent", "row": {"provider": "z"}},
            {"action": "skip", "promo_id": 3, "reason": "already known"},
        ]
    }
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(return_value=chat_response(payload))
            result = await curate(
                llm, offers=OFFER_JSON["offers"], promos=[], template_ids=["groq", "gemini"]
            )
    assert [decision["action"] for decision in result.decisions] == ["new", "skip"]
    assert len(result.dropped) == 3
    assert any("promo_id" in message for message in result.dropped)
    assert result.rounds == 1


# what fable is likely to have actually sent on run 3: a markdown preamble, then a fenced
# JSON *array* (not `{"decisions": [...]}`), one decision with a schema-adjacent action name
# and an expires_at that carries a time component.
MESSY_CURATOR_ANSWER = """Here are my decisions based on the offers you gave me:

```json
[
  {
    "action": "add",
    "row": {
      "provider": "cerebras",
      "url": "https://cerebras.ai/docs",
      "base_url": "https://api.cerebras.ai/v1",
      "api_key_env": "CEREBRAS_API_KEY",
      "expires_at": "2026-09-21 23:59",
      "note": "free tier, 1M tokens/day"
    },
    "reason": "vendor docs, quoted limits"
  }
]
```

Let me know if you need anything else."""


async def test_curate_parses_a_messy_prose_and_array_answer() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(return_value=chat_response(MESSY_CURATOR_ANSWER))
            result = await curate(llm, offers=OFFER_JSON["offers"], promos=[], template_ids=["cerebras"])
    assert result.dropped == []
    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision["action"] == "new"
    assert decision["row"]["provider"] == "cerebras"
    assert decision["row"]["expires_at"] == "2026-09-21"


async def test_curate_honours_needs_pages_once() -> None:
    first = {"decisions": [], "needs_pages": ["https://docs.test/api"]}
    fetched: list[list[str]] = []

    async def fetch(urls: list[str]):
        fetched.append(urls)
        return [type("P", (), {"url": urls[0], "text": "base url: https://api.vendor.test/v1"})()]

    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(side_effect=[chat_response(first), chat_response(DECISION_JSON)])
            result = await curate(
                llm,
                offers=OFFER_JSON["offers"],
                promos=[],
                template_ids=["groq"],
                fetch=fetch,  # type: ignore[arg-type]
            )
    assert route.call_count == 2
    assert fetched == [["https://docs.test/api"]]
    assert result.rounds == 2
    assert [decision["action"] for decision in result.decisions] == ["new"]
    second_prompt = json.loads(route.calls[1].request.content)["messages"][-1]["content"]
    assert "Pages you asked for" in second_prompt and "api.vendor.test" in second_prompt


async def test_curate_without_offers_makes_no_call() -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            route = respx.post(HUB_URL).mock(return_value=chat_response(DECISION_JSON))
            result = await curate(llm, offers=[], promos=[], template_ids=[])
    assert route.call_count == 0 and result.decisions == []


def test_curate_promo_view_is_minimal() -> None:
    from llmhub.scout.curate import promo_view

    view = promo_view(
        [{"id": 1, "provider": "groq", "url": "u", "status": "new", "base_url": None, "note": "x"}]
    )
    assert view == [{"id": 1, "provider": "groq", "url": "u", "status": "new", "base_url": None}]


# --- apply -------------------------------------------------------------------------


def test_apply_merges_by_identity_and_freezes_used_rows(hub: Hub) -> None:
    used = hub.store.add_promo(
        "groq", "https://groq.test/free", "old note", "used", "manual", identity="groq"
    )
    known = hub.store.add_promo(
        "cerebras", "https://cerebras.test/free/", "old", "known", "pepper.pl", identity="cerebras"
    )

    decisions = [
        # the same vendor under a different url is the same row, not a second lead
        {"action": "new", "row": {"provider": "groq", "url": "https://groq.test/other", "note": "new note"}},
        {"action": "update", "promo_id": used["id"], "row": {"provider": "groq", "note": "still new"}},
        {
            "action": "new",
            "row": {
                "provider": "cerebras cloud",
                "url": "https://cerebras.test/free",
                "note": "fresh limits",
            },
        },
        {"action": "new", "row": {"provider": "novita", "url": "https://novita.test/free", "note": "1M"}},
        {"action": "skip", "promo_id": known["id"], "reason": "unchanged"},
        {"action": "update", "promo_id": 999, "row": {"provider": "ghost"}},
    ]
    result = apply_decisions(hub.store, decisions)

    assert (result.new, result.updated, result.skipped) == (1, 3, 2)
    frozen = hub.store.promo(used["id"])
    assert frozen["status"] == "used" and frozen["source"] == "manual"
    assert frozen["url"] == "https://groq.test/free"
    assert frozen["note"].splitlines()[0] == "old note"
    assert frozen["note"].splitlines()[-1].endswith(" scout] still new")
    refreshed = hub.store.promo(known["id"])
    assert refreshed["note"].splitlines()[-1].endswith(" scout] fresh limits")
    created = next(row for row in hub.store.promos() if row["provider"] == "novita")
    assert created["source"] == "scout" and created["status"] == "new"
    assert created["identity"] == "novita"
    assert len(hub.store.promos()) == 3


def test_apply_dry_run_writes_nothing(hub: Hub) -> None:
    decisions = [{"action": "new", "row": {"provider": "novita", "url": "https://novita.test/free"}}]
    result = apply_decisions(hub.store, decisions, dry_run=True)
    assert result.new == 1
    assert hub.store.promos() == []


def test_report_is_ten_to_fifteen_lines() -> None:
    report = build_report(
        run_id=7,
        started_at="2026-09-07T08:00:00+00:00",
        counts={
            "sources": 40,
            "pages_fetched": 38,
            "pages_changed": 9,
            "offers": 5,
            "offers_dropped": 1,
            "new": 2,
            "updated": 1,
            "skipped": 2,
            "decisions_dropped": 1,
        },
        models_used={"extract": ["zai/glm-4.5-flash"], "curate": ["explabs/gpt-6-astra"]},
        tokens={"extract": {"in": 9000, "out": 800}, "curate": {"in": 4000, "out": 300}},
        errors=["docs-groq: ReadTimeout"],
        decisions=[
            {"outcome": "new", "row": {"provider": "groq", "url": "u1", "note": "free tier"}},
            {"outcome": "updated", "row": {"provider": "novita", "url": "u2", "note": "1M tokens"}},
            {"outcome": "skipped", "row": {"provider": "x"}},
        ],
    )
    lines = report.splitlines()
    assert 10 <= len(lines) <= 15
    assert lines[0].startswith("# scout run 7")
    assert any("zai/glm-4.5-flash" in line for line in lines)
    assert any("9000 in / 800 out" in line for line in lines)
    assert any("groq" in line for line in lines)


# --- runner ------------------------------------------------------------------------


async def test_run_writes_a_scout_run_row(scout_hub: Hub) -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>Free tier: 14,400 rpd</p>"))
        respx.post(HUB_URL).mock(side_effect=hub_side_effect(OFFER_JSON, DECISION_JSON))
        outcome = await scout_hub.scout.run()

    assert outcome["status"] == "done"
    assert outcome["sources"] == 1
    assert outcome["pages_fetched"] == 1 and outcome["pages_changed"] == 1
    assert outcome["offers"] == 1
    assert (outcome["new"], outcome["updated"], outcome["skipped"]) == (1, 0, 0)
    assert outcome["models_used"] == {
        "extract": ["zai/glm-4.5-flash"],
        "curate": ["explabs/gpt-6-astra"],
    }
    assert outcome["tokens"] == {"extract": {"in": 100, "out": 20}, "curate": {"in": 400, "out": 90}}
    assert outcome["errors"] == 0
    assert 10 <= len(outcome["report_md"].splitlines()) <= 15

    row = scout_hub.store.scout_run(outcome["id"])
    assert row["finished_at"] and row["status"] == "done"
    assert json.loads(row["decisions"])[0]["outcome"] == "new"
    promo = scout_hub.store.promos()[0]
    assert promo["provider"] == "groq" and promo["source"] == "scout"
    assert promo["base_url"] == "https://api.groq.com/openai/v1"
    assert scout_hub.scout.active is False
    assert outcome["invalid_reasons"] == []


async def test_run_records_invalid_reasons_on_a_bad_curator_answer(scout_hub: Hub) -> None:
    bad_decision = {"decisions": [{"action": "invent", "row": {"provider": "z"}}]}
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>Free tier: 14,400 rpd</p>"))
        respx.post(HUB_URL).mock(side_effect=hub_side_effect(OFFER_JSON, bad_decision))
        outcome = await scout_hub.scout.run()

    assert outcome["status"] == "done"
    assert (outcome["new"], outcome["updated"], outcome["skipped"]) == (0, 0, 0)
    assert len(outcome["invalid_reasons"]) == 1
    assert "action" in outcome["invalid_reasons"][0]
    assert "invalid decisions:" in outcome["report_md"]

    row = scout_hub.store.scout_run(outcome["id"])
    assert json.loads(row["invalid_reasons"])


def test_curate_prefer_reads_the_strong_alias_order(hub: Hub) -> None:
    from llmhub.config import AliasDef

    hub.registry.aliases["strong"] = AliasDef(prefer=["beta/m2", "alpha/m1"])
    assert curate_prefer(hub) == "beta/m2"


def test_curate_prefer_falls_back_to_the_hardcoded_list_without_a_strong_alias(hub: Hub) -> None:
    hub.registry.aliases.pop("strong", None)
    # none of CURATE_PREFERENCE's models exist in this test registry
    assert curate_prefer(hub) is None


async def test_run_dry_run_posts_nothing(scout_hub: Hub) -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, html="<p>Free tier</p>"))
        respx.post(HUB_URL).mock(side_effect=hub_side_effect(OFFER_JSON, DECISION_JSON))
        outcome = await scout_hub.scout.run(dry_run=True)

    assert outcome["new"] == 1
    assert outcome["dry_run"] is True
    assert scout_hub.store.promos() == []
    assert scout_hub.store.scout_run(outcome["id"])["status"] == "done"


async def test_run_records_source_errors_without_failing(scout_hub: Hub) -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(side_effect=httpx.ConnectError("refused"))
        route = respx.post(HUB_URL).mock(side_effect=hub_side_effect(OFFER_JSON, DECISION_JSON))
        outcome = await scout_hub.scout.run()
    assert route.call_count == 0
    assert outcome["status"] == "done"
    assert outcome["errors"] == 1
    assert outcome["pages_fetched"] == 0 and outcome["offers"] == 0


async def test_second_run_is_refused_while_one_is_active(scout_hub: Hub) -> None:
    scout_hub.scout.active_run_id = 42
    with pytest.raises(ScoutBusy):
        await scout_hub.scout.run()
    with pytest.raises(ScoutBusy):
        scout_hub.scout.start_run()


def test_reset_orphans_marks_a_dead_run_failed(scout_hub: Hub) -> None:
    run_id = scout_hub.store.create_scout_run()
    scout_hub.scout.reset_orphans()
    row = scout_hub.store.scout_run(run_id)
    assert row["status"] == "failed" and row["finished_at"]


def test_next_run_at_crosses_midnight() -> None:
    late = datetime.fromisoformat("2026-09-07T23:30:00+02:00")
    assert next_run_at("08:00", late) == "2026-09-08T08:00:00+02:00"

    early = datetime.fromisoformat("2026-09-07T07:00:00+02:00")
    assert next_run_at("08:00", early) == "2026-09-07T08:00:00+02:00"

    exact = datetime.fromisoformat("2026-09-07T08:00:00+02:00")
    assert next_run_at("08:00", exact) == "2026-09-08T08:00:00+02:00"

    assert next_run_at("23:59", datetime.fromisoformat("2026-09-07T23:58:00+02:00")).endswith(
        "2026-09-07T23:59:00+02:00"
    )
    assert next_run_at("", late) is None
    assert next_run_at(None, late) is None
    assert next_run_at("nonsense", late) is None


def test_schedule_off_when_env_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMHUB_SCOUT_AT", "")
    assert Settings.from_env().scout_at is None
    monkeypatch.setenv("LLMHUB_SCOUT_AT", "06:30")
    assert Settings.from_env().scout_at == "06:30"
    monkeypatch.delenv("LLMHUB_SCOUT_AT")
    assert Settings.from_env().scout_at == "08:00"


async def test_scheduler_starts_only_with_a_schedule(scout_hub: Hub) -> None:
    service = ScoutService(scout_hub)
    await service.start()
    assert service._scheduler is not None
    await service.stop()

    object.__setattr__(scout_hub.settings, "scout_at", None)
    off = ScoutService(scout_hub)
    await off.start()
    assert off._scheduler is None
    assert off.next_run_at() is None
    await off.stop()


# --- api ---------------------------------------------------------------------------


async def test_scout_api_shapes(client: httpx.AsyncClient, hub: Hub) -> None:
    empty = await client.get("/api/scout/status")
    assert empty.status_code == 200
    assert empty.json() == {
        "active": False,
        "next_run_at": hub.scout.next_run_at(),
        "last_run": None,
        "schedule": hub.settings.scout_at,
    }
    assert (await client.get("/api/scout/runs")).json() == {"runs": []}
    assert (await client.get("/api/scout/runs/1")).status_code == 404

    async def fake_pipeline(run_id: int, dry_run: bool) -> dict[str, Any]:
        from llmhub.scout.apply import write_run

        write_run(
            hub.store,
            run_id,
            counts={"sources": 3, "pages_fetched": 3, "pages_changed": 2, "offers": 4, "new": 1},
            models_used={"extract": ["zai/glm-4.5-flash"], "curate": ["explabs/gpt-6-astra"]},
            tokens={"extract": {"in": 10, "out": 2}, "curate": {"in": 5, "out": 1}},
            errors=["docs-groq: timeout"],
            decisions=[{"action": "new", "outcome": "new", "row": {"provider": "groq"}}],
            report_md="# scout run\n- ok",
        )
        return {}

    hub.scout._pipeline = fake_pipeline
    started = await client.post("/api/scout/run")
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert isinstance(run_id, int)
    await hub.scout._task

    listing = (await client.get("/api/scout/runs", params={"limit": 5})).json()
    row = listing["runs"][0]
    assert set(row) == {
        "id",
        "started_at",
        "finished_at",
        "status",
        "sources",
        "pages_fetched",
        "pages_changed",
        "offers",
        "new",
        "updated",
        "skipped",
        "models_used",
        "tokens",
        "errors",
        "dry_run",
        "invalid_reasons",
    }
    assert row["id"] == run_id and row["status"] == "done"
    assert row["sources"] == 3 and row["offers"] == 4 and row["new"] == 1
    assert row["models_used"]["curate"] == ["explabs/gpt-6-astra"]
    assert row["tokens"]["extract"] == {"in": 10, "out": 2}
    assert row["errors"] == 1

    detail = (await client.get(f"/api/scout/runs/{run_id}")).json()
    assert detail["report_md"].startswith("# scout run")
    assert detail["decisions"][0]["outcome"] == "new"
    assert detail["error_details"] == ["docs-groq: timeout"]

    status = (await client.get("/api/scout/status")).json()
    assert status["active"] is False
    assert status["last_run"]["id"] == run_id


async def test_scout_run_conflicts_while_active(client: httpx.AsyncClient, hub: Hub) -> None:
    hub.scout.active_run_id = 11
    conflict = await client.post("/api/scout/run")
    assert conflict.status_code == 409
    assert "11" in conflict.json()["detail"]
    assert (await client.get("/api/scout/status")).json()["active"] is True


async def test_scout_run_needs_token_off_loopback(client: httpx.AsyncClient, hub: Hub) -> None:
    object.__setattr__(hub.settings, "token", "secret")
    lan = {"x-forwarded-for": "192.168.1.20"}
    assert (await client.post("/api/scout/run", headers=lan)).status_code == 401
    assert (await client.get("/api/scout/status", headers=lan)).status_code == 200
