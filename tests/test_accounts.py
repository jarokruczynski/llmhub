from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from llmhub.accounts import (
    AccountError,
    account_id_in,
    apply_context_fills,
    backup_registry_file,
    caps_for_model_id,
    discover_model_rows,
    discover_models,
    merge_model,
    model_specs_from_rows,
    normalize_api_key,
    parse_model_ids,
    parse_model_rows,
    refresh_context_report,
)
from llmhub.config import Registry, example_registry_path, load_registry
from llmhub.providers_catalog import (
    chat_model_ids,
    is_chat_model_id,
    known_providers,
    normalize_model_id,
    template,
)
from llmhub.runtime import Hub

from .conftest import entry_of

ALPHA_URL = "https://alpha.test/v1/chat/completions"
ALPHA_MODELS_URL = "https://alpha.test/v1/models"
SECRET = "sk-secret-value-123"

OK_PAYLOAD = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}

SSE = (
    b'data: {"id":"1","choices":[{"delta":{"content":"he"},"index":0}]}\n\n'
    b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    b"data: [DONE]\n\n"
)


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


def env_file(hub: Hub, provider: str) -> Path:
    return hub.settings.env_dir / f"{provider}.env"


async def create_openrouter(client: httpx.AsyncClient, key: str = SECRET) -> httpx.Response:
    return await client.post(
        "/api/accounts",
        json={
            "provider": "openrouter",
            "account_id": "openrouter-main",
            "api_key": key,
            "template": "openrouter",
        },
    )


def test_normalize_api_key_strips_quotes_and_rejects_whitespace() -> None:
    assert normalize_api_key('  "sk-abc"  ') == "sk-abc"
    assert normalize_api_key("'sk-abc'") == "sk-abc"
    for bad in ("sk abc", "sk-abc\nMORE=1", "", "   ", "sk\tabc"):
        with pytest.raises(AccountError):
            normalize_api_key(bad)


def test_parse_model_ids_handles_openai_and_plain_shapes() -> None:
    assert parse_model_ids({"data": [{"id": "b"}, {"id": "a"}]}) == ["a", "b"]
    assert parse_model_ids({"models": [{"name": "x"}]}) == ["x"]
    assert parse_model_ids(["z", "y"]) == ["y", "z"]
    assert parse_model_ids({"nothing": 1}) == []


def test_parse_model_ids_reads_the_pinned_field() -> None:
    cloudflare = {"result": [{"id": "1a2b-uuid", "name": "@cf/meta/llama-3.1-8b-instruct"}]}
    assert parse_model_ids(cloudflare, "name") == ["@cf/meta/llama-3.1-8b-instruct"]
    assert parse_model_ids(cloudflare) == ["1a2b-uuid"]


def test_parse_model_rows_reads_every_documented_context_shape() -> None:
    payload = {
        "data": [
            {"id": "a", "context_length": 32768},  # OpenRouter, Cohere compat, DeepInfra
            {"id": "b", "context_window": 131072},  # groq
            {"id": "c", "inputTokenLimit": 1048576},  # gemini
            {"id": "d", "max_context_length": 32000},  # Mistral
            {"id": "e", "context_size": 8192},  # Novita
            {"id": "f", "max_model_len": 16384},  # vLLM-style
            {"id": "g", "max_input_tokens": 4096},
            {"id": "h", "properties": [{"property_id": "context_window", "value": "131072"}]},
            {"id": "i", "top_provider": {"context_length": 65536}},
            {"id": "j", "limits": {"context": 4000}},
            {"id": "k", "limits": {"input": 2000}},
            {"id": "l"},  # no context anywhere
            {"id": "m", "context_length": 0},  # zero is not a real ceiling
            {"id": "n", "context_length": None},
        ]
    }
    rows = {row["id"]: row["context"] for row in parse_model_rows(payload)}
    assert rows == {
        "a": 32768,
        "b": 131072,
        "c": 1048576,
        "d": 32000,
        "e": 8192,
        "f": 16384,
        "g": 4096,
        "h": 131072,
        "i": 65536,
        "j": 4000,
        "k": 2000,
        "l": None,
        "m": None,
        "n": None,
    }


def test_parse_model_rows_first_field_wins_and_dedupes() -> None:
    # context_length beats context_window when a row somehow carries both
    rows = parse_model_rows({"data": [{"id": "x", "context_length": 111, "context_window": 222}]})
    assert rows == [{"id": "x", "context": 111}]
    # a repeated id keeps only the first row seen
    rows = parse_model_rows({"data": [{"id": "y", "context_length": 1}, {"id": "y", "context_length": 2}]})
    assert rows == [{"id": "y", "context": 1}]


def test_parse_model_ids_is_unaffected_by_the_context_field() -> None:
    # the thin wrapper still returns exactly what it always did
    assert parse_model_ids({"data": [{"id": "b", "context_length": 8}, {"id": "a"}]}) == ["a", "b"]


def test_caps_for_a_discovered_model_come_off_the_name() -> None:
    assert caps_for_model_id("@cf/meta/llama-3.1-8b-instruct") == ["text"]
    assert caps_for_model_id("@cf/meta/llama-3.2-11b-vision-instruct") == ["text", "vision"]
    assert caps_for_model_id("@cf/qwen/qwen2.5-vl-7b") == ["text", "vision"]
    assert caps_for_model_id("@cf/llava-hf/llava-1.5-7b-hf") == ["text", "vision"]


def test_non_chat_ids_are_dropped_from_discovery() -> None:
    for model_id in (
        "embed-v4.0",
        "rerank-v3.5",
        "cohere-transcribe-03-2026",
        "parse-v5.0",
        "gemini-2.5-flash-preview-tts",
        "imagen-4.0-generate-image-001",
        "gemini-live-2.5-flash",
        "gemini-robotics-er-1.5-preview",
        "gemini-2.5-computer-use-preview",
        "command-a-translate-08-2025",
        "whisper-large-v3",
        "@cf/meta/llama-guard-3-8b",
        "omni-moderation-latest",
        "tiny-aya-fire",
    ):
        assert not is_chat_model_id(model_id), model_id
    for model_id in (
        "gemini-3.8-flash",
        "command-a-plus-05-2026",
        "@cf/qwen/qwq-32b",
        "openai/gpt-oss-120b",
    ):
        assert is_chat_model_id(model_id), model_id


def test_gemini_ids_lose_the_models_prefix() -> None:
    assert normalize_model_id("models/gemini-3.8-flash", "gemini") == "gemini-3.8-flash"
    # only the template that lists them prefixed strips anything
    assert normalize_model_id("models/gemini-3.8-flash", "groq") == "models/gemini-3.8-flash"
    assert normalize_model_id("gemini-3.8-flash", "gemini") == "gemini-3.8-flash"


def test_chat_model_ids_strips_filters_and_dedupes() -> None:
    listed = [
        "models/gemini-3.8-flash",
        "models/gemini-3.8-flash",
        "gemini-3.8-flash",
        "models/gemini-embedding-001",
        "models/gemini-2.5-flash-preview-tts",
    ]
    assert chat_model_ids(listed, "gemini") == ["gemini-3.8-flash"]
    assert chat_model_ids(["@cf/qwq-32b", "@cf/meta/llama-guard-3-8b"], "cloudflare-workers-ai") == [
        "@cf/qwq-32b"
    ]


@respx.mock
async def test_discover_returns_only_routable_chat_ids(hub: Hub) -> None:
    respx.get(ALPHA_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "models/gemini-3.8-flash"},
                    {"id": "models/gemini-embedding-001"},
                    {"id": "models/gemini-live-2.5-flash"},
                ]
            },
        )
    )
    ids, response = await discover_models(
        hub.client, base_url="https://alpha.test/v1", api_key="k", template_id="gemini"
    )
    assert (ids, response.status_code) == (["gemini-3.8-flash"], 200)


@respx.mock
async def test_discover_models_output_is_unchanged_when_rows_carry_context(hub: Hub) -> None:
    """`discover_models` keeps its 2-tuple of bare ids: its two callers in api.py are unaware
    of `discover_model_rows` and must keep working exactly as before."""
    respx.get(ALPHA_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "gamma-one", "context_length": 32768}]})
    )
    ids, response = await discover_models(hub.client, base_url="https://alpha.test/v1", api_key="k")
    assert (ids, response.status_code) == (["gamma-one"], 200)


@respx.mock
async def test_discover_model_rows_normalizes_ids_and_carries_context(hub: Hub) -> None:
    respx.get(ALPHA_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "models/gemini-3.8-flash", "inputTokenLimit": 1048576},
                    {"id": "models/gemini-3.8-flash"},  # duplicate once normalized, first wins
                    {"id": "models/gemini-embedding-001", "inputTokenLimit": 2048},  # non-chat, dropped
                    {"id": "models/gemini-live-2.5-flash"},  # non-chat, dropped
                ]
            },
        )
    )
    rows, response = await discover_model_rows(
        hub.client, base_url="https://alpha.test/v1", api_key="k", template_id="gemini"
    )
    assert response.status_code == 200
    assert rows == [{"id": "gemini-3.8-flash", "context": 1048576}]


@respx.mock
async def test_discover_model_rows_reports_no_context_as_none(hub: Hub) -> None:
    respx.get(ALPHA_MODELS_URL).mock(return_value=httpx.Response(200, json={"data": [{"id": "plain"}]}))
    rows, response = await discover_model_rows(hub.client, base_url="https://alpha.test/v1", api_key="k")
    assert response.status_code == 200
    assert rows == [{"id": "plain", "context": None}]


@respx.mock
async def test_discover_model_rows_reports_the_http_failure(hub: Hub) -> None:
    respx.get(ALPHA_MODELS_URL).mock(return_value=httpx.Response(401, json={"error": "nope"}))
    rows, response = await discover_model_rows(hub.client, base_url="https://alpha.test/v1", api_key="k")
    assert (rows, response.status_code) == ([], 401)


def test_model_specs_from_rows_sets_context_only_when_known() -> None:
    specs = model_specs_from_rows(
        [{"id": "with-ctx", "context": 4096}, {"id": "no-ctx", "context": None}],
        notes="discovered",
    )
    by_id = {spec["id"]: spec for spec in specs}
    assert by_id["with-ctx"]["context"] == 4096
    assert "context" not in by_id["no-ctx"]
    assert by_id["with-ctx"]["free"] == {}
    assert by_id["with-ctx"]["notes"] == "discovered"


def test_merge_model_never_overwrites_an_existing_context() -> None:
    current = {"id": "m1", "caps": ["text"], "context": 100000, "notes": "old"}
    merged = merge_model(dict(current), {"context": 32768, "notes": "rediscovered"})
    assert merged["context"] == 100000
    assert merged["notes"] == "rediscovered"

    # a model with no context yet takes the incoming value
    fresh = merge_model({"id": "m2", "caps": ["text"]}, {"context": 32768})
    assert fresh["context"] == 32768


def test_account_id_is_read_out_of_free_text_and_dashboard_urls() -> None:
    account = "0123456789abcdef0123456789abcdef"
    assert account_id_in(f"https://dash.cloudflare.com/{account}/ai/workers-ai") == account
    assert account_id_in(f"  {account.upper()}  ") == account
    assert account_id_in("https://dash.cloudflare.com/profile/api-tokens") is None
    assert account_id_in(f"x{account}") is None
    assert account_id_in(None) is None


@respx.mock
async def test_discover_through_a_template_spec_needs_its_fields(hub: Hub) -> None:
    spec = template("cloudflare-workers-ai")["discover"]
    route = respx.get(url__startswith="https://api.cloudflare.com/client/v4/accounts/acc1/ai")
    route.mock(return_value=httpx.Response(200, json={"result": [{"name": "@cf/tiny"}]}))

    ids, response = await discover_models(
        hub.client,
        base_url="https://unused.test/v1",
        api_key="cf-token",
        spec=spec,
        fields={"account_id": "acc1"},
    )
    assert (ids, response.status_code) == (["@cf/tiny"], 200)
    assert "models/search" in str(route.calls[0].request.url)

    with pytest.raises(AccountError):
        await discover_models(hub.client, base_url="https://unused.test/v1", api_key="cf-token", spec=spec)


def test_catalog_covers_the_designed_providers() -> None:
    ids = [item["id"] for item in known_providers()]
    assert ids == [
        "explabs",
        "zai",
        "dashscope",
        "openrouter",
        "gemini",
        "groq",
        "cerebras",
        "sambanova",
        "opencode-zen",
        "cloudflare-workers-ai",
        "nvidia-nim",
        "mistral",
        "cohere",
        "huggingface",
        "moonshot",
        "deepseek",
        "xai",
        "fireworks",
        "together",
        "deepinfra",
        "minimax",
        "inception",
        "ai21",
        "upstage",
        "siliconflow",
        "novita",
        "hyperbolic",
        "nebius",
        "chutes",
        "ollama",
        "requesty",
        "scaleway",
        "zenmux",
        "pollinations",
        "vercel-ai-gateway",
        "modelscope",
        "tencent-hunyuan",
        "ovh-ai-endpoints",
        "kilo",
        "perplexity",
        "featherless",
        "baseten",
        "friendli",
        "poe",
        "ollama-cloud",
        "antigravity",
        "copilot",
        "custom",
    ]
    openrouter = template("openrouter")
    assert openrouter["base_url"] == "https://openrouter.ai/api/v1"
    assert openrouter["api_key_env"] == "OPENROUTER_API_KEY"
    assert [model["id"] for model in openrouter["models"]] == [
        "minimax/minimax-m3:free",
        "thinkingmachines/inkling-small:free",
        "nvidia/nemotron-3.5-lightning:free",
        "poolside/laguna-s-2.1:free",
        "openrouter/free",
    ]
    assert all(model["free"] == {} for model in openrouter["models"])
    assert "20 RPM, 50 RPD" in openrouter["notes"]

    gemini = template("gemini")
    assert gemini["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert "AI Studio" in gemini["notes"]

    cloudflare = template("cloudflare-workers-ai")
    assert cloudflare["fields"] == ["account_id"]
    assert "{account_id}" in cloudflare["base_url"]
    assert cloudflare["api_key_env"] == "CLOUDFLARE_API_TOKEN"
    assert {"cf", "workers ai", "workersai", "cloudflare ai"} <= set(cloudflare["aliases"])
    assert {"dash.cloudflare.com", "gateway.ai.cloudflare.com"} <= set(cloudflare["hostnames"])
    assert "Account / Workers AI / Read" in cloudflare["notes"]
    # Workers AI serves no /models under base_url; discovery goes to the model search instead
    assert cloudflare["discover"]["url"].endswith("/ai/models/search?task=Text%20Generation")
    assert cloudflare["discover"]["id_field"] == "name"
    assert cloudflare["discover"]["notes"] == "10k neurons/day shared across the account"

    assert template("ollama")["api_key_env"] is None
    assert template("custom")["models"] == []

    nvidia = template("nvidia-nim")
    assert nvidia["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert nvidia["api_key_env"] == "NVIDIA_API_KEY"
    assert [model["id"] for model in nvidia["models"]] == [
        "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4-pro",
    ]
    assert all("unverified free status" in model["notes"] for model in nvidia["models"])

    cohere = template("cohere")
    assert cohere["base_url"] == "https://api.cohere.com/compatibility/v1"
    assert cohere["api_key_env"] == "COHERE_API_KEY"
    # the compatibility endpoint takes response_format json_object, the native one does not
    assert [model["id"] for model in cohere["models"]] == [
        "command-a-plus-05-2026",
        "command-a-03-2025",
    ]
    assert all("json" in model["caps"] for model in cohere["models"])

    for name, base_url, env_name in (
        ("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
        ("huggingface", "https://router.huggingface.co/v1", "HF_TOKEN"),
        ("moonshot", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
        ("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
        ("xai", "https://api.x.ai/v1", "XAI_API_KEY"),
        ("fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
        ("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
        ("deepinfra", "https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY"),
        ("minimax", "https://api.minimax.io/v1", "MINIMAX_API_KEY"),
        ("inception", "https://api.inceptionlabs.ai/v1", "INCEPTION_API_KEY"),
        ("ai21", "https://api.ai21.com/studio/v1", "AI21_API_KEY"),
        ("upstage", "https://api.upstage.ai/v1", "UPSTAGE_API_KEY"),
        ("siliconflow", "https://api.siliconflow.com/v1", "SILICONFLOW_API_KEY"),
        ("novita", "https://api.novita.ai/v3/openai", "NOVITA_API_KEY"),
        ("hyperbolic", "https://api.hyperbolic.xyz/v1", "HYPERBOLIC_API_KEY"),
        ("nebius", "https://api.tokenfactory.nebius.com/v1", "NEBIUS_API_KEY"),
        ("chutes", "https://llm.chutes.ai/v1", "CHUTES_API_KEY"),
        ("requesty", "https://router.requesty.ai/v1", "REQUESTY_API_KEY"),
        ("scaleway", "https://api.scaleway.ai/v1", "SCW_API_KEY"),
        ("vercel-ai-gateway", "https://ai-gateway.vercel.sh/v1", "AI_GATEWAY_API_KEY"),
        ("tencent-hunyuan", "https://api.hunyuan.cloud.tencent.com/v1", "HUNYUAN_API_KEY"),
        ("ovh-ai-endpoints", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "OVH_AI_ENDPOINTS_API_KEY"),
        ("kilo", "https://api.kilo.ai/api/gateway", "KILO_API_KEY"),
        ("perplexity", "https://api.perplexity.ai", "PERPLEXITY_API_KEY"),
        ("featherless", "https://api.featherless.ai/v1", "FEATHERLESS_API_KEY"),
        ("baseten", "https://inference.baseten.co/v1", "BASETEN_API_KEY"),
        ("friendli", "https://api.friendli.ai/serverless/v1", "FRIENDLI_TOKEN"),
        ("poe", "https://api.poe.com/v1", "POE_API_KEY"),
        ("ollama-cloud", "https://ollama.com/v1", "OLLAMA_API_KEY"),
    ):
        row = template(name)
        assert row["base_url"] == base_url
        assert row["api_key_env"] == env_name
        # no verified free list: these rely on discover
        assert row["models"] == []

    zenmux = template("zenmux")
    assert [model["id"] for model in zenmux["models"]] == ["moonshotai/kimi-k3-free"]
    assert all(model["free"] == {} for model in zenmux["models"])
    assert all("unverified" in model["notes"] for model in zenmux["models"])

    pollinations = template("pollinations")
    assert [model["id"] for model in pollinations["models"]] == [
        "openai-fast",
        "mistral",
        "llama-fast-roblox",
    ]
    assert all(model["free"] == {} for model in pollinations["models"])
    assert all("unverified" in model["notes"] for model in pollinations["models"])

    modelscope = template("modelscope")
    assert [model["id"] for model in modelscope["models"]] == ["Qwen/Qwen3-8B"]
    assert all(model["free"] == {} for model in modelscope["models"])
    assert all("unverified" in model["notes"] for model in modelscope["models"])


def test_every_template_carries_unambiguous_aliases_and_hostnames() -> None:
    owners: dict[str, list[str]] = {}
    for item in known_providers():
        assert isinstance(item["aliases"], list)
        assert isinstance(item["hostnames"], list)
        for word in (*item["aliases"], *item["hostnames"]):
            owners.setdefault(word, [])
            if item["id"] not in owners[word]:
                owners[word].append(item["id"])
    assert {word: ids for word, ids in owners.items() if len(ids) > 1} == {}
    assert "z.ai" in template("zai")["aliases"]
    assert "api.groq.com" in template("groq")["hostnames"]
    assert template("custom")["aliases"] == []

    # ollama-cloud owns the ollama.com hostname alone; the local template keeps only "ollama"
    assert template("ollama")["hostnames"] == []
    assert template("ollama")["aliases"] == ["ollama"]
    assert template("ollama-cloud")["hostnames"] == ["ollama.com"]
    assert {"ollama cloud", "ollama.com"} <= set(template("ollama-cloud")["aliases"])

    assert "CO_API_KEY" in template("cohere")["notes"]


def test_catalog_seeds_max_request_tokens_only_where_a_vendor_publishes_it() -> None:
    groq = template("groq")
    assert {model["id"]: model["max_request_tokens"] for model in groq["models"]} == {
        "openai/gpt-oss-120b": 8000,
        "qwen/qwen3-32b": 8000,
    }

    cerebras = template("cerebras")
    assert {model["id"]: model["max_request_tokens"] for model in cerebras["models"]} == {
        "gpt-oss-120b": 30000,
        "qwen-3.8-27b": 30000,
    }

    # every other template is left unset - no invented numbers
    for name in ("openrouter", "nvidia-nim", "explabs", "zai", "dashscope", "gemini"):
        for model in template(name)["models"]:
            assert model.get("max_request_tokens") is None


def test_catalog_matches_the_shipped_example_registry() -> None:
    example = yaml.safe_load(example_registry_path().read_text(encoding="utf-8"))["providers"]
    for name in ("explabs", "zai", "dashscope", "ollama"):
        known = template(name)
        shipped = example[name]
        assert known["kind"] == shipped["kind"]
        assert known["base_url"] == shipped["base_url"]
        assert known["api_key_env"] == shipped["accounts"][0]["api_key_env"]
        assert [model["id"] for model in known["models"]] == [model["id"] for model in shipped["models"]]
        for known_model, shipped_model in zip(known["models"], shipped["models"], strict=False):
            assert known_model["caps"] == shipped_model["caps"]
            assert known_model["free"] == shipped_model.get("free")
            assert known_model.get("extra_body") == shipped_model.get("extra_body")


async def test_providers_known_endpoint(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/api/providers/known")).json()
    assert {item["id"] for item in payload["providers"]} >= {"openrouter", "gemini", "custom"}
    row = next(item for item in payload["providers"] if item["id"] == "groq")
    assert row["base_url"] == "https://api.groq.com/openai/v1"
    assert row["api_key_env"] == "GROQ_API_KEY"
    assert row["docs_url"]


async def test_create_account_from_template_writes_yaml_env_and_routes(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    response = await create_openrouter(client)
    assert response.status_code == 200
    body = response.json()
    assert body["created_provider"] is True
    assert body["api_key_env"] == "OPENROUTER_API_KEY"
    assert body["key_present"] is True
    assert body["env_file"] == str(env_file(hub, "openrouter"))
    assert body["reloaded"]["providers"] == 4
    assert "warning" not in body

    data = registry_yaml(hub)
    provider = data["providers"]["openrouter"]
    assert provider["base_url"] == "https://openrouter.ai/api/v1"
    assert provider["template"] == "openrouter"
    assert provider["accounts"] == [{"id": "openrouter-main", "api_key_env": "OPENROUTER_API_KEY"}]
    assert len(provider["models"]) == 5
    assert set(data["providers"]) == {"alpha", "beta", "gamma", "openrouter"}
    assert data["aliases"]["auto"]["prefer"] == ["alpha/m1", "beta/m2"]

    path = env_file(hub, "openrouter")
    assert path.read_text(encoding="utf-8") == f"OPENROUTER_API_KEY={SECRET}\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    listed = (await client.get("/v1/models")).json()["data"]
    ids = [row["id"] for row in listed]
    assert "openrouter/minimax/minimax-m3:free" in ids
    assert "openrouter/openrouter/free" in ids

    registry = (await client.get("/api/registry")).json()["providers"]["openrouter"]
    assert registry["template"] == "openrouter"
    assert registry["docs_url"] == "https://openrouter.ai/docs"
    assert registry["accounts"][0]["env_file"] == str(path)
    assert registry["accounts"][0]["key_present"] is True


async def test_create_account_on_custom_provider_needs_base_url(client: httpx.AsyncClient) -> None:
    missing = await client.post(
        "/api/accounts",
        json={"provider": "newvendor", "account_id": "n1", "api_key": SECRET},
    )
    assert missing.status_code == 400
    assert "base_url" in missing.json()["detail"]

    created = await client.post(
        "/api/accounts",
        json={
            "provider": "newvendor",
            "account_id": "n1",
            "api_key": SECRET,
            "base_url": "https://api.newvendor.test/v1",
            "models": [{"id": "small", "caps": ["text"], "free": {}}],
        },
    )
    assert created.status_code == 200
    assert created.json()["api_key_env"] == "NEWVENDOR_API_KEY"
    assert created.json()["models"] == ["small"]


async def test_create_account_substitutes_template_fields(client: httpx.AsyncClient, hub: Hub) -> None:
    response = await client.post(
        "/api/accounts",
        json={
            "provider": "cloudflare-workers-ai",
            "account_id": "cf-main",
            "api_key": SECRET,
            "template": "cloudflare-workers-ai",
            "fields": {"account_id": "abc123"},
        },
    )
    assert response.status_code == 200
    block = registry_yaml(hub)["providers"]["cloudflare-workers-ai"]
    assert block["base_url"] == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"


async def test_create_account_rejects_bad_key_and_duplicates(client: httpx.AsyncClient) -> None:
    bad = await client.post(
        "/api/accounts",
        json={"provider": "openrouter", "account_id": "x", "api_key": "sk key", "template": "openrouter"},
    )
    assert bad.status_code == 400

    assert (await create_openrouter(client)).status_code == 200
    duplicate = await create_openrouter(client)
    assert duplicate.status_code == 409

    with_models = await client.post(
        "/api/accounts",
        json={
            "provider": "openrouter",
            "account_id": "openrouter-main",
            "api_key": SECRET,
            "template": "openrouter",
            "models": [{"id": "extra", "caps": ["text"], "free": {}}],
        },
    )
    assert with_models.status_code == 409
    assert with_models.json()["hint"] == ("use POST api/accounts/openrouter/openrouter-main/models")


async def test_rotate_replaces_one_line_and_keeps_the_rest(client: httpx.AsyncClient, hub: Hub) -> None:
    path = env_file(hub, "openrouter")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# keys\nOTHER_KEY=keepme\nOPENROUTER_API_KEY=old\n", encoding="utf-8")

    await create_openrouter(client, "sk-first")
    rotated = await client.put("/api/accounts/openrouter/openrouter-main/key", json={"api_key": "sk-second"})
    assert rotated.status_code == 200
    assert rotated.json()["rotated"] is True
    assert rotated.json()["key_present"] is True
    assert path.read_text(encoding="utf-8") == ("# keys\nOTHER_KEY=keepme\nOPENROUTER_API_KEY=sk-second\n")
    assert os.environ["OPENROUTER_API_KEY"] == "sk-second"
    assert "sk-second" not in rotated.text


async def test_rotate_unknown_account_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.put("/api/accounts/ghost/g1/key", json={"api_key": "sk-x"})).status_code == 404
    await create_openrouter(client)
    assert (
        await client.put("/api/accounts/openrouter/nope/key", json={"api_key": "sk-x"})
    ).status_code == 404


async def test_delete_keeps_env_line_unless_purged(client: httpx.AsyncClient, hub: Hub) -> None:
    await create_openrouter(client)
    path = env_file(hub, "openrouter")

    removed = await client.delete("/api/accounts/openrouter/openrouter-main")
    assert removed.status_code == 200
    assert removed.json()["purged_key"] is False
    assert registry_yaml(hub)["providers"]["openrouter"]["accounts"] == []
    assert f"OPENROUTER_API_KEY={SECRET}" in path.read_text(encoding="utf-8")

    await create_openrouter(client)
    purged = await client.delete("/api/accounts/openrouter/openrouter-main?purge_key=1")
    assert purged.json()["purged_key"] is True
    assert "OPENROUTER_API_KEY" not in path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY" not in os.environ


@respx.mock
async def test_account_test_records_usage_under_hub_test(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    response = await client.post("/api/accounts/alpha/alpha-1/test", json={"model": "m1"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == "alpha/m1"
    assert body["attempts"][0]["status"] == "ok"
    assert body["usage"]["total_tokens"] == 4

    rows = hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["app"] == "hub-test"
    assert rows[0]["model"] == "alpha/m1"
    assert rows[0]["account"] == "alpha-1"


@respx.mock
async def test_account_test_returns_vendor_error_body(client: httpx.AsyncClient) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
    )
    body = (await client.post("/api/accounts/alpha/alpha-1/test", json={"model": "m1"})).json()
    assert body["ok"] is False
    assert body["status"] == "quota"
    assert body["error"]["error"]["code"] == "insufficient_quota"


async def test_account_test_refuses_paid_model_without_allow_paid(
    client: httpx.AsyncClient,
) -> None:
    refused = await client.post("/api/accounts/alpha/alpha-1/test", json={"model": "paid1"})
    assert refused.status_code == 409
    assert "allow_paid" in refused.json()["detail"]


@respx.mock
async def test_account_test_allows_paid_when_asked(client: httpx.AsyncClient) -> None:
    respx.post(ALPHA_URL).mock(return_value=httpx.Response(200, json=OK_PAYLOAD))
    ok = await client.post("/api/accounts/alpha/alpha-1/test", json={"model": "paid1", "allow_paid": True})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


async def test_account_test_unknown_targets(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/accounts/ghost/g1/test")).status_code == 404
    assert (await client.post("/api/accounts/alpha/nope/test")).status_code == 404
    assert (await client.post("/api/accounts/alpha/alpha-1/test", json={"model": "ghost"})).status_code == 404


@respx.mock
async def test_discover_returns_sorted_ids(client: httpx.AsyncClient) -> None:
    route = respx.get(ALPHA_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "zeta"}, {"id": "alpha-one"}]})
    )
    response = await client.post("/api/providers/alpha/discover", json={"account_id": "alpha-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["models"] == ["alpha-one", "zeta"]
    assert body["note"] == "limits unknown"
    assert body["registered"] == ["m1", "paid1"]
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key-1"


@respx.mock
async def test_discover_reports_vendor_failure(client: httpx.AsyncClient) -> None:
    respx.get(ALPHA_MODELS_URL).mock(return_value=httpx.Response(401, json={"error": "nope"}))
    body = (await client.post("/api/providers/alpha/discover")).json()
    assert body["ok"] is False
    assert body["http_status"] == 401
    assert body["models"] == []


async def test_discover_unknown_provider_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/providers/ghost/discover")).status_code == 404


async def test_reload_endpoint_picks_up_a_hand_edited_registry(client: httpx.AsyncClient, hub: Hub) -> None:
    data = registry_yaml(hub)
    data["providers"]["beta"]["models"].append({"id": "m3", "caps": ["text"], "free": {}})
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert hub.registry.entry("beta/m3") is None

    response = await client.post("/api/registry/reload")
    assert response.status_code == 200
    assert response.json()["reloaded"] is True
    assert hub.registry.entry("beta/m3") is not None


async def test_reload_swaps_the_router_atomically(hub: Hub) -> None:
    old_router = hub.router
    old_registry = hub.registry
    cooled = entry_of(hub, "alpha/m1", "alpha-1")
    old_router.set_cooldown(cooled)
    semaphores = old_router._semaphores

    hub.reload()

    assert hub.router is not old_router
    assert hub.registry is not old_registry
    # the object an in-flight request is holding still points at the registry it started with
    assert old_router.registry is old_registry
    assert hub.router.registry is hub.registry
    assert hub.router._semaphores is semaphores
    now = datetime.now(UTC)
    assert hub.router.in_cooldown(entry_of(hub, "alpha/m1", "alpha-1"), now) is not None
    assert hub.router.retry_delays == old_router.retry_delays


@respx.mock
async def test_reload_during_a_streaming_request(client: httpx.AsyncClient, hub: Hub) -> None:
    respx.post(ALPHA_URL).mock(
        return_value=httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
    )
    chunks: list[bytes] = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "alpha/m1",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            "stream": True,
        },
        headers={"X-Hub-App": "my-app"},
    ) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_bytes():
            if not chunks:
                hub.reload()
            chunks.append(chunk)

    assert b"".join(chunks) == SSE
    rows = hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["in_tokens"] == 7
    assert rows[0]["out_tokens"] == 3


async def test_key_never_leaks_into_responses_or_logs(
    client: httpx.AsyncClient, hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    created = await create_openrouter(client)
    rotated = await client.put("/api/accounts/openrouter/openrouter-main/key", json={"api_key": SECRET})
    reads = [
        created,
        rotated,
        await client.get("/api/registry"),
        await client.get("/api/status"),
        await client.get("/api/events", params={"limit": 100}),
        await client.get("/api/providers/known"),
        await client.get("/v1/models"),
        await client.post("/api/registry/reload"),
    ]
    for response in reads:
        assert SECRET not in response.text

    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args)

    dumped = hub.store.query("SELECT * FROM events") + hub.store.query("SELECT * FROM usage")
    assert SECRET not in str(dumped)


async def test_mutations_need_a_token_from_the_lan_and_carry_the_warning(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    lan = {"X-Forwarded-For": "192.168.1.44"}
    denied = await client.post(
        "/api/accounts",
        json={"provider": "openrouter", "account_id": "o1", "api_key": SECRET, "template": "openrouter"},
        headers=lan,
    )
    assert denied.status_code == 401

    object.__setattr__(hub.settings, "token", "secret")
    allowed = await client.post(
        "/api/accounts",
        json={"provider": "openrouter", "account_id": "o1", "api_key": SECRET, "template": "openrouter"},
        headers={**lan, "Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["warning"] == "plain HTTP from LAN; add keys from the Mac when possible"

    reloaded = await client.post("/api/registry/reload", headers={**lan, "Authorization": "Bearer secret"})
    assert reloaded.json()["warning"]


async def test_second_account_on_an_existing_provider_appends_models(
    client: httpx.AsyncClient, hub: Hub
) -> None:
    response = await client.post(
        "/api/accounts",
        json={
            "provider": "beta",
            "account_id": "beta-2",
            "api_key": "sk-beta-two",
            "models": [{"id": "m9", "caps": ["text"], "free": {}}],
        },
    )
    assert response.status_code == 200
    assert response.json()["created_provider"] is False
    assert response.json()["api_key_env"] == "BETA_API_KEY"

    block = registry_yaml(hub)["providers"]["beta"]
    assert [account["id"] for account in block["accounts"]] == ["beta-1", "beta-2"]
    assert [model["id"] for model in block["models"]] == ["m2", "m9"]
    assert block["base_url"] == "https://beta.test/v1"
    assert hub.registry.entry("beta/m9", "beta-2") is not None


async def test_add_models_appends_to_the_provider(client: httpx.AsyncClient, hub: Hub) -> None:
    response = await client.post(
        "/api/accounts/beta/beta-1/models",
        json={"models": [{"id": "m7", "caps": ["text", "tools"], "free": {}, "context": 4096}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == ["m7"]
    assert body["updated"] == []
    assert body["models"] == ["m2", "m7"]
    assert body["reloaded"]["providers"] == 3

    block = registry_yaml(hub)["providers"]["beta"]
    assert [model["id"] for model in block["models"]] == ["m2", "m7"]
    assert block["models"][1]["context"] == 4096
    assert hub.registry.entry("beta/m7", "beta-1") is not None


async def test_add_models_updates_an_existing_id_in_place(client: httpx.AsyncClient, hub: Hub) -> None:
    response = await client.post(
        "/api/accounts/alpha/alpha-2/models",
        json={
            "models": [
                {
                    "id": "m1",
                    "caps": ["text", "vision"],
                    "free": {"daily": {"in_tokens": 42}},
                    "notes": "halved",
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == []
    assert body["updated"] == ["m1"]
    assert body["models"] == ["m1", "paid1"]

    model = registry_yaml(hub)["providers"]["alpha"]["models"][0]
    assert model["caps"] == ["text", "vision"]
    assert model["notes"] == "halved"
    # request wins per window, the untouched window and the other fields survive
    assert model["free"] == {"hourly": {"out_tokens": 1000}, "daily": {"in_tokens": 42}}
    assert model["context"] == 100000
    assert model["extra_body"] == {"thinking": {"type": "disabled"}}

    entry = entry_of(hub, "alpha/m1", "alpha-2")
    assert "vision" in entry.model.caps


async def test_add_models_unknown_account_or_provider_is_404(client: httpx.AsyncClient) -> None:
    unknown_account = await client.post("/api/accounts/beta/ghost/models", json={"models": [{"id": "m8"}]})
    assert unknown_account.status_code == 404
    assert "ghost" in unknown_account.json()["detail"]

    unknown_provider = await client.post("/api/accounts/ghost/g1/models", json={"models": [{"id": "m8"}]})
    assert unknown_provider.status_code == 404


# --- refresh-context ------------------------------------------------------------------------


@respx.mock
async def test_refresh_context_report_skips_a_model_that_already_has_a_context(hub: Hub) -> None:
    respx.get(ALPHA_MODELS_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "m1", "context_length": 999}, {"id": "paid1"}]}
        )
    )
    reports = await refresh_context_report(hub.client, hub.registry, provider_filter="alpha")
    assert len(reports) == 1
    report = reports[0]
    assert report["provider"] == "alpha"
    assert report["models"] == 2
    # m1 already carries context: 100000 in the fixture registry; the listing's 999 is ignored
    assert report["already_set"] == 1
    assert report["filled"] == []
    assert report["unknown"] == ["paid1"]
    assert report["status"] == "ok"


@respx.mock
async def test_refresh_context_report_fills_a_model_with_no_context(hub: Hub) -> None:
    respx.get("https://beta.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m2", "context_length": 32768}]})
    )
    reports = await refresh_context_report(hub.client, hub.registry, provider_filter="beta")
    report = reports[0]
    assert report["filled"] == [("m2", 32768)]
    assert report["already_set"] == 0
    assert report["unknown"] == []
    assert report["status"] == "ok"


async def test_refresh_context_report_skips_a_provider_with_no_usable_key(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPHA_KEY_1")
    monkeypatch.delenv("ALPHA_KEY_2")
    reports = await refresh_context_report(hub.client, hub.registry, provider_filter="alpha")
    assert reports[0]["status"] == "no_account_key"
    assert reports[0]["filled"] == []


@respx.mock
async def test_refresh_context_report_reports_a_vendor_http_failure(hub: Hub) -> None:
    respx.get("https://gamma.test/v1/models").mock(return_value=httpx.Response(401, json={"error": "nope"}))
    reports = await refresh_context_report(hub.client, hub.registry, provider_filter="gamma")
    assert reports[0]["status"] == "http_401"


@respx.mock
async def test_refresh_context_report_reports_an_empty_listing(hub: Hub) -> None:
    respx.get("https://gamma.test/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    reports = await refresh_context_report(hub.client, hub.registry, provider_filter="gamma")
    assert reports[0]["status"] == "no_models"


def gemini_registry() -> Registry:
    return Registry.model_validate(
        {
            "providers": {
                "gemini": {
                    "kind": "openai",
                    "template": "gemini",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                    "accounts": [{"id": "g1", "api_key_env": "GEMINI_API_KEY"}],
                    "models": [
                        {"id": "gemini-3.8-flash", "caps": ["text"]},
                        {"id": "gemini-3.5-flash-lite", "caps": ["text"]},
                    ],
                }
            }
        }
    )


@respx.mock
async def test_refresh_context_report_falls_back_to_the_native_gemini_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key")
    respx.get("https://generativelanguage.googleapis.com/v1beta/openai/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "models/gemini-3.8-flash", "object": "model"}]},
        )
    )
    native = respx.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-3.8-flash", "inputTokenLimit": 1048576},
                    {"name": "models/gemini-3.5-flash-lite", "inputTokenLimit": 32768},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(client, gemini_registry())
    report = reports[0]
    assert sorted(report["filled"]) == [
        ("gemini-3.5-flash-lite", 32768),
        ("gemini-3.8-flash", 1048576),
    ]
    assert report["from_catalog"] == []
    assert report["unknown"] == []
    # the compat listing has no key on it, so the native call carries the vendor's own header
    assert native.calls[0].request.headers["x-goog-api-key"] == "gm-key"
    assert "authorization" not in native.calls[0].request.headers


def cohere_registry() -> Registry:
    return Registry.model_validate(
        {
            "providers": {
                "cohere": {
                    "kind": "openai",
                    "template": "cohere",
                    "base_url": "https://api.cohere.com/compatibility/v1",
                    "accounts": [{"id": "c1", "api_key_env": "COHERE_API_KEY"}],
                    "models": [{"id": "command-a-03-2025", "caps": ["text"]}],
                }
            }
        }
    )


@respx.mock
async def test_refresh_context_report_falls_back_to_the_native_cohere_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "co-key")
    respx.get("https://api.cohere.com/compatibility/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "command-a-03-2025"}]})
    )
    native = respx.get("https://api.cohere.com/v1/models").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "command-a-03-2025", "context_length": 256000}]}
        )
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(client, cohere_registry())
    report = reports[0]
    assert report["filled"] == [("command-a-03-2025", 256000)]
    assert report["from_catalog"] == []
    assert native.calls[0].request.headers["authorization"] == "Bearer co-key"


@respx.mock
async def test_refresh_context_report_skips_the_native_call_when_nothing_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No point spending a second request once the compat listing already answered everything."""
    monkeypatch.setenv("COHERE_API_KEY", "co-key")
    respx.get("https://api.cohere.com/compatibility/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "command-a-03-2025", "context_length": 256000}]}
        )
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(client, cohere_registry())
    report = reports[0]
    assert report["filled"] == [("command-a-03-2025", 256000)]
    native_calls = [call for call in respx.calls if "api.cohere.com/v1/models" in str(call.request.url)]
    assert native_calls == []


def cerebras_registry(extra_model: dict[str, Any] | None = None) -> Registry:
    models = [
        {"id": "gpt-oss-120b", "caps": ["text"]},
        {"id": "qwen-3.8-27b", "caps": ["text"]},
    ]
    if extra_model:
        models.append(extra_model)
    return Registry.model_validate(
        {
            "providers": {
                "cerebras": {
                    "kind": "openai",
                    "template": "cerebras",
                    "base_url": "https://api.cerebras.ai/v1",
                    "accounts": [{"id": "cb1", "api_key_env": "CEREBRAS_API_KEY"}],
                    "models": models,
                }
            }
        }
    )


@respx.mock
async def test_refresh_context_report_fills_from_the_catalog_when_no_listing_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "cb-key")
    respx.get("https://api.cerebras.ai/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-oss-120b", "object": "model"},
                    {"id": "qwen-3.8-27b", "object": "model"},
                    {"id": "gemma-4-31b", "object": "model"},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(
            client, cerebras_registry(extra_model={"id": "gemma-4-31b", "caps": ["text"]})
        )
    report = reports[0]
    assert sorted(report["filled"]) == [("gpt-oss-120b", 65536), ("qwen-3.8-27b", 65536)]
    assert sorted(report["from_catalog"]) == ["gpt-oss-120b", "qwen-3.8-27b"]
    # the catalog has no context on this id (it was discovered later, never in the template)
    assert report["unknown"] == ["gemma-4-31b"]


@respx.mock
async def test_refresh_context_report_catalog_fallback_never_overwrites_owner_value() -> None:
    registry = Registry.model_validate(
        {
            "providers": {
                "cerebras": {
                    "kind": "openai",
                    "template": "cerebras",
                    "base_url": "https://api.cerebras.ai/v1",
                    "accounts": [{"id": "cb1", "api_key_env": None}],
                    "models": [{"id": "gpt-oss-120b", "caps": ["text"], "context": 12345}],
                }
            }
        }
    )
    respx.get("https://api.cerebras.ai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "gpt-oss-120b"}]})
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(client, registry)
    report = reports[0]
    assert report["already_set"] == 1
    assert report["filled"] == []
    assert report["from_catalog"] == []


async def test_refresh_context_report_skips_cli_providers() -> None:
    registry = Registry.model_validate(
        {
            "providers": {
                "agentcli": {
                    "kind": "cli",
                    "command": "agy",
                    "accounts": [{"id": "a1", "api_key_env": None}],
                    "models": [{"id": "m1", "caps": ["text"]}],
                }
            }
        }
    )
    async with httpx.AsyncClient() as client:
        reports = await refresh_context_report(client, registry)
    assert reports == []


def test_backup_registry_file_copies_the_current_yaml(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    backup = backup_registry_file(path, tmp_path / "backups")
    assert backup.parent == tmp_path / "backups"
    assert backup.read_text(encoding="utf-8") == "providers: {}\n"
    assert backup.name.startswith("providers-") and backup.name.endswith(".yaml")


def test_apply_context_fills_writes_only_missing_fields_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        """
providers:
  alpha:
    kind: openai
    base_url: https://alpha.test/v1
    accounts: []
    models:
      - id: m1
        caps: [text]
        context: 100000
      - id: paid1
        caps: [text]
""",
        encoding="utf-8",
    )
    reports = [
        {
            "provider": "alpha",
            # m1 already has a context: the write must skip it even though it is listed here
            "filled": [("m1", 32768), ("paid1", 8192)],
        }
    ]
    written = apply_context_fills(path, reports)
    assert written == 1

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = {model["id"]: model for model in data["providers"]["alpha"]["models"]}
    assert models["m1"]["context"] == 100000
    assert models["paid1"]["context"] == 8192

    registry = load_registry(path)
    assert registry.providers["alpha"].models[1].context == 8192


def test_apply_context_fills_is_a_noop_with_nothing_to_write(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    original = "providers:\n  alpha:\n    kind: openai\n    base_url: https://a.test/v1\n    models: []\n"
    path.write_text(original, encoding="utf-8")
    written = apply_context_fills(path, [{"provider": "alpha", "filled": []}])
    assert written == 0
    assert path.read_text(encoding="utf-8") == original
