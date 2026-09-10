from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
import yaml

from llmhub.accounts import SourceUnresolved, candidate_base_urls, default_account_id, resolve_source
from llmhub.runtime import Hub

GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
SAMBA_CHAT = "https://api.sambanova.ai/v1/chat/completions"
SAMBA_MODELS = "https://api.sambanova.ai/v1/models"
SECRET = "gsk_quick_add_secret_value"
GROQ_ACCOUNT = default_account_id("groq")
SAMBANOVA_ACCOUNT = default_account_id("sambanova")

CF_ACCOUNT = "0123456789abcdef0123456789abcdef"
CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1"
CF_MODELS = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/models/search"
CF_MODELS_PAYLOAD = {
    "result": [
        {"id": "1a2b", "name": "@cf/meta/llama-3.1-8b-instruct"},
        {"id": "3c4d", "name": "@cf/meta/llama-3.2-11b-vision-instruct"},
        {"id": "5e6f", "name": "@cf/qwen/qwen2.5-vl-7b"},
    ]
}

OK_PAYLOAD = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


@pytest.fixture(autouse=True)
def restore_environ() -> Iterator[None]:
    before = dict(os.environ)
    yield
    for name in set(os.environ) - set(before):
        os.environ.pop(name, None)
    for name, value in before.items():
        os.environ[name] = value


def registry_yaml(hub: Hub) -> dict[str, Any]:
    return yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source, expected, kind",
    [
        ("groq", "groq", "id"),
        ("openrouter", "openrouter", "id"),
        ("nvidia-nim", "nvidia-nim", "id"),
        ("  DeepSeek  ", "deepseek", "id"),
        ("z.ai", "zai", "alias"),
        ("hugging face", "huggingface", "alias"),
        ("grok credits", "xai", "alias"),
        ("nvidia nim", "nvidia-nim", "alias"),
        ("promo mail from Together AI", "together", "id"),
        ("key here: https://aistudio.google.com/apikey", "gemini", "hostname"),
        ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini", "hostname"),
        ("see https://dash.cloudflare.com/profile", "cloudflare-workers-ai", "alias"),
        ("https://platform.minimaxi.com/login", "minimax", "hostname"),
        ("sambanovacloud trial", "sambanova", "token"),
        ("https://www.inceptionlabs.ai/blog/mercury-2", "inception", "alias"),
        ("promo from Inception Labs", "inception", "id"),
        ("siliconflow free tier", "siliconflow", "id"),
        ("https://studio.nebius.com/settings/api-keys", "nebius", "id"),
        ("https://router.requesty.ai/dashboard", "requesty", "id"),
        ("scaleway generative apis free tier", "scaleway", "id"),
        ("https://text.pollinations.ai/openai", "pollinations", "id"),
    ],
)
def test_resolution_table_over_ids_aliases_hostnames(source: str, expected: str, kind: str) -> None:
    template_id, confidence, reason = resolve_source(source)
    assert template_id == expected
    assert reason.startswith(kind)
    assert 0.5 <= confidence <= 1.0


@pytest.mark.parametrize(
    "key, expected",
    [
        ("sk-or-v1-abcdef", "openrouter"),
        ("AIzaSyABCDEF", "gemini"),
        ("gsk_abcdef", "groq"),
        ("csk-abcdef", "cerebras"),
        ("xpl_abcdef", "explabs"),
        ("nvapi-abcdef", "nvidia-nim"),
        ("hf_abcdef", "huggingface"),
    ],
)
def test_resolution_falls_back_to_the_key_prefix(key: str, expected: str) -> None:
    template_id, confidence, reason = resolve_source(None, None, key)
    assert (template_id, confidence) == (expected, 0.5)
    assert reason.startswith("key_prefix")


def test_resolution_prefers_the_promo_row_over_the_key_shape() -> None:
    promo = {"provider": "cerebras", "url": "https://cloud.cerebras.ai/"}
    template_id, confidence, reason = resolve_source("whatever", promo, "gsk_looks_like_groq")
    assert template_id == "cerebras"
    assert confidence == 0.95
    assert "promo" in reason


def test_resolution_reads_the_host_out_of_a_promo_url() -> None:
    promo = {"provider": "some newsletter", "url": "https://openrouter.ai/settings/keys"}
    assert resolve_source(None, promo)[0] == "openrouter"


def test_resolution_reads_a_new_catalog_host_out_of_a_promo_url() -> None:
    # the promo the map was drawn on: a vendor name no template knows, and a blog url
    promo = {"provider": "diffusion LLM newsletter", "url": "https://www.inceptionlabs.ai/blog/mercury-2"}
    template_id, confidence, reason = resolve_source(None, promo)
    assert (template_id, confidence) == ("inception", 0.95)
    assert "promo url" in reason


def test_resolution_ambiguity_lists_the_candidates() -> None:
    with pytest.raises(SourceUnresolved) as raised:
        resolve_source("deepseek or deepinfra, cannot remember")
    assert raised.value.status_code == 422
    assert raised.value.needs == ["source"]
    assert raised.value.guess == ["deepinfra", "deepseek"]


def test_resolution_unknown_source_asks_for_one() -> None:
    with pytest.raises(SourceUnresolved) as raised:
        resolve_source("a key someone handed me")
    assert raised.value.needs == ["source"]
    assert raised.value.guess == []


def test_resolution_known_name_without_a_template_asks_for_base_url() -> None:
    with pytest.raises(SourceUnresolved) as raised:
        resolve_source(None, None, "sk-ant-api03-xyz")
    assert raised.value.needs == ["base_url"]
    assert raised.value.guess == ["anthropic"]


def test_resolution_ollama_cloud_never_steals_the_plain_ollama_alias() -> None:
    # "ollama" alone still resolves to the local template, unaffected by ollama-cloud existing
    assert resolve_source("ollama")[0] == "ollama"
    # the literal id "ollama-cloud" contains the local id as a bounded prefix, so asking for it
    # by name is ambiguous rather than silently landing on the wrong template
    with pytest.raises(SourceUnresolved) as raised:
        resolve_source("ollama-cloud api key")
    assert sorted(raised.value.guess) == ["ollama", "ollama-cloud"]


async def test_quick_add_unresolved_is_422_with_needs(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/accounts/quick", json={"api_key": "abc123", "source": "no idea"})
    assert response.status_code == 422
    assert response.json()["needs"] == ["source"]
    assert response.json()["guess"] == []


async def test_quick_add_ambiguous_is_422_with_guess(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/accounts/quick",
        json={"api_key": "abc123", "source": "deepseek albo deepinfra"},
    )
    assert response.status_code == 422
    assert response.json()["guess"] == ["deepinfra", "deepseek"]


async def test_quick_add_template_with_placeholders_needs_the_account_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/accounts/quick", json={"api_key": "abc123", "source": "cloudflare workers ai"}
    )
    assert response.status_code == 422
    assert response.json()["needs"] == ["account_id"]
    assert response.json()["guess"] == ["cloudflare-workers-ai"]
    assert "base_url" in response.json()["detail"]


@respx.mock
async def test_quick_add_reads_the_account_id_out_of_a_dashboard_url(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.get(url__startswith=CF_MODELS).mock(return_value=httpx.Response(200, json=CF_MODELS_PAYLOAD))
    respx.post(f"{CF_BASE}/chat/completions").mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    body = (
        await client.post(
            "/api/accounts/quick",
            json={
                "api_key": "cf-token",
                "source": f"token from https://dash.cloudflare.com/{CF_ACCOUNT}/ai/workers-ai",
            },
        )
    ).json()
    assert body["provider"] == "cloudflare-workers-ai"
    assert body["created_provider"] is True
    assert body["discovered"] is True
    assert body["models"] == [
        "@cf/meta/llama-3.1-8b-instruct",
        "@cf/meta/llama-3.2-11b-vision-instruct",
        "@cf/qwen/qwen2.5-vl-7b",
    ]
    assert body["test"]["ok"] is True

    block = registry_yaml(hub)["providers"]["cloudflare-workers-ai"]
    assert block["base_url"] == CF_BASE
    assert block["fields"] == {"account_id": CF_ACCOUNT}
    assert block["accounts"][0]["api_key_env"] == "CLOUDFLARE_API_TOKEN"
    # the search endpoint answers with result[].name; the uuid in result[].id is unusable
    caps = {model["id"]: model["caps"] for model in block["models"]}
    assert caps["@cf/meta/llama-3.1-8b-instruct"] == ["text"]
    assert caps["@cf/meta/llama-3.2-11b-vision-instruct"] == ["text", "vision"]
    assert caps["@cf/qwen/qwen2.5-vl-7b"] == ["text", "vision"]
    assert all(model["free"] == {} for model in block["models"])
    assert all(model["notes"] == "10k neurons/day shared across the account" for model in block["models"])
    assert (
        hub.registry.entry(
            "cloudflare-workers-ai/@cf/qwen/qwen2.5-vl-7b", default_account_id("cloudflare-workers-ai")
        )
        is not None
    )


@respx.mock
async def test_quick_add_takes_the_account_id_from_the_fields_body(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.get(url__startswith=CF_MODELS).mock(
        return_value=httpx.Response(200, json={"result": [{"name": "@cf/tiny", "id": "uuid"}]})
    )
    respx.post(f"{CF_BASE}/chat/completions").mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    body = (
        await client.post(
            "/api/accounts/quick",
            json={
                "api_key": "cf-token",
                "source": "workers ai",
                "fields": {"account_id": CF_ACCOUNT},
                "account_id": "cf-main",
            },
        )
    ).json()
    assert body["provider"] == "cloudflare-workers-ai"
    assert body["account_id"] == "cf-main"
    assert body["models"] == ["@cf/tiny"]
    assert registry_yaml(hub)["providers"]["cloudflare-workers-ai"]["base_url"] == CF_BASE


@respx.mock
async def test_quick_add_from_a_template_writes_yaml_env_and_reloads(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post(
        "/api/accounts/quick", json={"api_key": SECRET, "source": "console.groq.com key"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "groq"
    assert body["account_id"] == GROQ_ACCOUNT
    assert body["created_provider"] is True
    assert body["discovered"] is False
    assert body["models"] == ["openai/gpt-oss-120b", "qwen/qwen3-32b"]
    assert body["test"] == {
        "ok": True,
        "status": "ok",
        "model": "groq/openai/gpt-oss-120b",
        "latency_ms": body["test"]["latency_ms"],
    }
    assert "rotated" not in body
    assert "promo_id" not in body
    assert "warning" not in body

    block = registry_yaml(hub)["providers"]["groq"]
    assert block["base_url"] == "https://api.groq.com/openai/v1"
    assert block["accounts"] == [{"id": GROQ_ACCOUNT, "api_key_env": "GROQ_API_KEY"}]

    env_path = hub.settings.env_dir / "groq.env"
    assert env_path.read_text(encoding="utf-8") == f"GROQ_API_KEY={SECRET}\n"
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"

    # hot reload: routable without a restart, and the probe landed under hub-test
    assert hub.registry.entry("groq/qwen/qwen3-32b", GROQ_ACCOUNT) is not None
    rows = hub.store.query("SELECT * FROM usage")
    assert [row["app"] for row in rows] == ["hub-test"]


@respx.mock
async def test_quick_add_from_a_promo_row_marks_it_used(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    promo = (
        await client.post(
            "/api/promos",
            json={"provider": "groq", "url": "https://console.groq.com/keys", "note": "free tier"},
        )
    ).json()
    assert promo["account_key"] is None

    body = (
        await client.post("/api/accounts/quick", json={"api_key": SECRET, "promo_id": promo["id"]})
    ).json()
    assert body["provider"] == "groq"
    assert body["promo_id"] == promo["id"]

    row = (await client.get("/api/promos")).json()["promos"][0]
    assert row["status"] == "used"
    assert row["account_key"] == f"groq/{GROQ_ACCOUNT}"


async def test_quick_add_unknown_promo_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/accounts/quick", json={"api_key": SECRET, "promo_id": 4242})
    assert response.status_code == 404


@respx.mock
async def test_quick_add_discovers_models_when_the_template_has_none(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.get(SAMBA_MODELS).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "Meta-Llama-4", "context_length": 131072}, {"id": "Qwen4-32B"}]},
        )
    )
    respx.post(SAMBA_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))

    body = (
        await client.post("/api/accounts/quick", json={"api_key": "sk-samba", "source": "sambanova"})
    ).json()
    assert body["discovered"] is True
    assert body["models"] == ["Meta-Llama-4", "Qwen4-32B"]
    assert body["test"]["ok"] is True
    assert body["test"]["model"] == "sambanova/Meta-Llama-4"

    models = registry_yaml(hub)["providers"]["sambanova"]["models"]
    assert [model["id"] for model in models] == ["Meta-Llama-4", "Qwen4-32B"]
    assert all(model["free"] == {} for model in models)
    assert all(model["notes"] == "limits unknown, discovered" for model in models)
    assert hub.registry.entry("sambanova/Qwen4-32B", SAMBANOVA_ACCOUNT) is not None

    by_id = {model["id"]: model for model in models}
    assert by_id["Meta-Llama-4"]["context"] == 131072
    assert "context" not in by_id["Qwen4-32B"]


@respx.mock
async def test_quick_add_survives_a_failed_discover(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.get(SAMBA_MODELS).mock(return_value=httpx.Response(401, json={"error": "nope"}))
    body = (
        await client.post("/api/accounts/quick", json={"api_key": "sk-samba", "source": "sambanova"})
    ).json()
    assert body["discovered"] is False
    assert body["models"] == []
    assert body["test"] == {"ok": False, "status": "no_free_model", "model": None, "latency_ms": 0}
    assert registry_yaml(hub)["providers"]["sambanova"]["accounts"][0]["id"] == SAMBANOVA_ACCOUNT


@respx.mock
async def test_quick_add_on_an_existing_account_rotates_the_key(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    first = await client.post("/api/accounts/quick", json={"api_key": "gsk_first", "source": "groq"})
    assert first.json()["created_provider"] is True

    second = await client.post("/api/accounts/quick", json={"api_key": "gsk_second", "source": "groq"})
    assert second.status_code == 200
    body = second.json()
    assert body["rotated"] is True
    assert body["created_provider"] is False
    assert body["account_id"] == GROQ_ACCOUNT

    env_path = hub.settings.env_dir / "groq.env"
    assert env_path.read_text(encoding="utf-8") == "GROQ_API_KEY=gsk_second\n"
    assert os.environ["GROQ_API_KEY"] == "gsk_second"
    accounts = registry_yaml(hub)["providers"]["groq"]["accounts"]
    assert [account["id"] for account in accounts] == [GROQ_ACCOUNT]


@respx.mock
async def test_quick_add_reports_a_failed_test_without_failing(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(GROQ_CHAT).mock(
        return_value=httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
    )
    response = await client.post("/api/accounts/quick", json={"api_key": SECRET, "source": "groq"})
    assert response.status_code == 200
    body = response.json()
    assert body["test"]["ok"] is False
    assert body["test"]["status"] == "quota"
    assert body["test"]["error"]["error"]["code"] == "insufficient_quota"
    # the key is registered anyway: a bad probe is a report, not a rollback
    assert registry_yaml(hub)["providers"]["groq"]["accounts"][0]["id"] == GROQ_ACCOUNT
    assert hub.registry.entry("groq/qwen/qwen3-32b", GROQ_ACCOUNT) is not None


@respx.mock
async def test_quick_add_never_leaks_the_key(
    client: httpx.AsyncClient, hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    created = await client.post("/api/accounts/quick", json={"api_key": SECRET, "source": "groq"})
    reads = [
        created,
        await client.get("/api/registry"),
        await client.get("/api/events", params={"limit": 100}),
        await client.get("/api/promos"),
    ]
    for response in reads:
        assert SECRET not in response.text
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args)
    dumped = hub.store.query("SELECT * FROM events") + hub.store.query("SELECT * FROM usage")
    assert SECRET not in str(dumped)


@respx.mock
async def test_quick_add_needs_a_token_from_the_lan_and_warns(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    lan = {"X-Forwarded-For": "192.168.1.44"}
    denied = await client.post("/api/accounts/quick", json={"api_key": SECRET, "source": "groq"}, headers=lan)
    assert denied.status_code == 401

    object.__setattr__(hub.settings, "token", "secret")
    allowed = await client.post(
        "/api/accounts/quick",
        json={"api_key": SECRET, "source": "groq"},
        headers={**lan, "Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["warning"] == "plain HTTP from LAN; add keys from the Mac when possible"


@respx.mock
async def test_quick_add_falls_back_to_base_url_for_a_vendor_off_the_catalog(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    respx.get("https://api.newvendor.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "small"}]})
    )
    respx.post("https://api.newvendor.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_PAYLOAD)
    )
    body = (
        await client.post(
            "/api/accounts/quick",
            json={
                "api_key": "sk-new",
                "source": "a key someone handed me",
                "base_url": "https://api.newvendor.test/v1",
            },
        )
    ).json()
    assert body["provider"] == "newvendor"
    assert body["account_id"] == default_account_id("newvendor")
    assert body["created_provider"] is True
    assert body["discovered"] is True
    assert body["models"] == ["small"]
    assert body["test"]["ok"] is True

    block = registry_yaml(hub)["providers"]["newvendor"]
    assert block["base_url"] == "https://api.newvendor.test/v1"
    assert block["accounts"][0]["api_key_env"] == "NEWVENDOR_API_KEY"


@respx.mock
async def test_quick_add_takes_the_account_id_from_the_body(client: httpx.AsyncClient) -> None:
    respx.get(SAMBA_MODELS).mock(return_value=httpx.Response(401, json={"error": "nope"}))
    body = (
        await client.post(
            "/api/accounts/quick",
            json={"api_key": "sk-samba", "source": "sambanova", "account_id": "samba-work"},
        )
    ).json()
    assert body["account_id"] == "samba-work"


async def test_promos_carry_account_key_through_post_and_patch(client: httpx.AsyncClient) -> None:
    promo = (await client.post("/api/promos", json={"provider": "zai", "account_key": "zai/zai-main"})).json()
    assert promo["account_key"] == "zai/zai-main"

    patched = await client.patch(
        f"/api/promos/{promo['id']}", json={"status": "used", "account_key": "zai/zai-2"}
    )
    assert patched.json()["account_key"] == "zai/zai-2"
    assert patched.json()["status"] == "used"

    listed = (await client.get("/api/promos")).json()["promos"][0]
    assert listed["account_key"] == "zai/zai-2"


@respx.mock
async def test_quick_add_uses_the_base_url_carried_by_the_promo(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.get("https://api.othervendor.test/v2/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ov-small"}]})
    )
    respx.post("https://api.othervendor.test/v2/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_PAYLOAD)
    )
    promo = (
        await client.post(
            "/api/promos",
            json={
                "provider": "Other Vendor",
                "url": "https://blog.othervendor.test/free-tier",
                "base_url": "https://api.othervendor.test/v2",
                "api_key_env": "OTHERVENDOR_TOKEN",
            },
        )
    ).json()

    body = (
        await client.post("/api/accounts/quick", json={"api_key": "sk-other", "promo_id": promo["id"]})
    ).json()
    assert body["provider"] == "other-vendor"
    assert body["created_provider"] is True
    assert body["discovered"] is True
    assert body["models"] == ["ov-small"]
    assert body["test"]["ok"] is True

    block = registry_yaml(hub)["providers"]["other-vendor"]
    assert block["base_url"] == "https://api.othervendor.test/v2"
    # the promo named the env var, so nothing is derived from the provider name
    assert block["accounts"][0]["api_key_env"] == "OTHERVENDOR_TOKEN"
    env_path = hub.settings.env_dir / "other-vendor.env"
    assert env_path.read_text(encoding="utf-8") == "OTHERVENDOR_TOKEN=sk-other\n"


@respx.mock
async def test_quick_add_guesses_the_endpoint_and_verifies_it(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.get("https://api.guessme.test/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "gm-large"}, {"id": "gm-embed-1"}]})
    )
    respx.post("https://api.guessme.test/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_PAYLOAD)
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404, json={"error": "no such endpoint"}))

    body = (
        await client.post(
            "/api/accounts/quick",
            json={
                "api_key": "sk-guess",
                "source": "free credits, sign up at https://blog.guessme.test/launch",
            },
        )
    ).json()
    assert body["provider"] == "guessme"
    assert body["created_provider"] is True
    assert body["discovered"] is True
    # the discover filter drops the embedding row the vendor lists next to the chat one
    assert body["models"] == ["gm-large"]
    assert body["test"]["ok"] is True

    block = registry_yaml(hub)["providers"]["guessme"]
    assert block["base_url"] == "https://api.guessme.test/openai/v1"
    assert block["accounts"][0]["api_key_env"] == "GUESSME_API_KEY"
    assert hub.registry.entry("guessme/gm-large", default_account_id("guessme")) is not None


@respx.mock
async def test_quick_add_reports_a_guess_that_rejected_the_key(client: httpx.AsyncClient) -> None:
    respx.get("https://api.picky.test/openai/v1/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404, json={"error": "no such endpoint"}))

    response = await client.post(
        "/api/accounts/quick",
        json={"api_key": "sk-picky", "source": "https://docs.picky.test/quickstart"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["needs"] == ["base_url"]
    # the endpoint that knew the key was wrong is the one the form should prefill
    assert body["guesses"][0] == {
        "url": "https://api.picky.test/openai/v1",
        "status": "exists_key_rejected",
    }
    assert [row["url"] for row in body["guesses"][1:]] == [
        "https://api.picky.test/v1",
        "https://picky.test/v1",
        "https://picky.test/api/v1",
        "https://api.picky.test/v1beta/openai",
    ]
    assert {row["status"] for row in body["guesses"][1:]} == {"not_found"}
    assert "none served a model list" in body["detail"]


@respx.mock
async def test_quick_add_422_lists_the_endpoints_it_tried(client: httpx.AsyncClient) -> None:
    respx.route(method="GET").mock(side_effect=httpx.ConnectError("no route to host"))

    response = await client.post(
        "/api/accounts/quick",
        json={"api_key": "sk-void", "source": "key from https://www.voidvendor.test/blog/free"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["needs"] == ["base_url"]
    assert body["guess"] == []
    assert [row["url"] for row in body["guesses"]] == [
        "https://api.voidvendor.test/v1",
        "https://api.voidvendor.test/openai/v1",
        "https://voidvendor.test/v1",
        "https://voidvendor.test/api/v1",
        "https://api.voidvendor.test/v1beta/openai",
    ]
    assert {row["status"] for row in body["guesses"]} == {"no_response"}


def test_guess_candidates_drop_the_link_label_and_cap_the_list() -> None:
    urls = candidate_base_urls(["https://blog.foo.test/post and https://console.bar.test/keys"])
    assert urls[:2] == ["https://api.foo.test/v1", "https://api.foo.test/openai/v1"]
    # six candidates at most, so the second domain only gets its first shape
    assert len(urls) == 6
    assert urls[-1] == "https://api.bar.test/v1"

    # a vendor whose endpoint no pattern reaches is tried from the map first, and an api host
    # in the source url is reduced to the bare domain before anything is built from it
    assert candidate_base_urls(["https://api.z.ai/manage-apikey"])[0] == "https://api.z.ai/api/paas/v4"
    assert candidate_base_urls(["https://chutes.ai/pricing"])[0] == "https://llm.chutes.ai/v1"
    assert candidate_base_urls(["no url here"]) == []


async def test_promos_carry_base_url_and_api_key_env_through_post_and_patch(
    client: httpx.AsyncClient,
) -> None:
    promo = (
        await client.post(
            "/api/promos",
            json={"provider": "New Vendor", "base_url": "https://api.nv.test/v1", "api_key_env": "NV_KEY"},
        )
    ).json()
    assert (promo["base_url"], promo["api_key_env"]) == ("https://api.nv.test/v1", "NV_KEY")

    patched = await client.patch(
        f"/api/promos/{promo['id']}",
        json={"base_url": "https://api.nv.test/v2", "api_key_env": "NV_TOKEN"},
    )
    assert patched.json()["base_url"] == "https://api.nv.test/v2"

    listed = (await client.get("/api/promos")).json()["promos"][0]
    assert (listed["base_url"], listed["api_key_env"]) == ("https://api.nv.test/v2", "NV_TOKEN")
