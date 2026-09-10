from __future__ import annotations

import copy
import re
from typing import Any

FIELD_RE = re.compile(r"\{([a-z0-9_]+)\}")

# Built-in provider templates offered by the dashboard "Add account" form. A template is a
# starting point only: what actually routes is ~/.llmhub/providers.yaml. Window shapes follow
# the registry (`free: null` = paid, `free: {}` = free with unknown limits, otherwise the
# hourly/daily/monthly/allowance dicts). `fields` lists extra values the form must collect
# because base_url carries a placeholder for them.
#
# `aliases` and `hostnames` feed quick add: aliases are the names a human writes or pastes
# ("z.ai", "hugging face", "grok"), hostnames are the domains that turn up in a promo url.
# Keep aliases unambiguous across templates - two templates answering the same word turn a
# quick add into a 422 asking the owner which one they meant.
#
# `discover` is for vendors that do not serve OpenAI-style `GET {base_url}/models`: `url` is
# the full endpoint (it may carry the same {placeholders} as base_url), `id_field` names the
# key holding the model id in each row, `notes` is what the discovered models get registered
# with. Without it discovery falls back to `GET {base_url}/models`.

UNVERIFIED_FREE = "unverified free status; registered because the owner added the key on purpose"

EXPLABS_PROMO_FREE: dict[str, Any] = {
    "hourly": {"out_tokens": 30000},
    "daily": {"in_tokens": 375000, "out_tokens": 75000},
}

ANTIGRAVITY_MODEL_NOTE = (
    "free via the signed-in agy CLI; no vision and no tools (the agent runs sandboxed in an "
    "empty directory), limits unknown"
)

COPILOT_MODEL_NOTE = (
    "a Copilot seat that is not the owner's to spend freely; the seat holder sees usage and audit logs"
)
# The CLI has no `models` subcommand, so there is nothing to discover: the ids below are what
# it prints under the `model` setting in `copilot help config` (1.0.83, read 2026-09-08), plus
# `auto` (Copilot routes it - the live probe resolved auto to mai-code-1.1-flash). Ids use
# dots, not dashes; an id that is not on this list is refused with
# `Error: Model "x" from --model flag is not available.` before any model call, so a stale
# entry costs a wasted candidate rather than credits. Re-read that help topic after a CLI
# update.
COPILOT_MODEL_IDS: tuple[str, ...] = (
    "auto",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5.1",
    "claude-fable-5",
    "claude-opus-4.8",
    "claude-opus-4.8-fast",
    "claude-opus-4.7",
    "claude-sonnet-4.6",
    "claude-haiku-4.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5-mini",
    "mai-code-1.1-flash",
    "mai-code-1-flash-picker",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "grok-4.5",
    "kimi-k3",
    "kimi-k2.7-code",
)
# `--max-ai-credits` is a per-session soft cap the CLI enforces itself (minimum 30), so one
# runaway agent loop cannot spend a month of a shared allowance in a single call.
COPILOT_MAX_AI_CREDITS = 60

PROVIDER_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "explabs",
        "kind": "openai",
        "base_url": "https://api.experientiallabs.ai/v1",
        "api_key_env": "EXPLABS_API_KEY",
        "docs_url": "https://platform.experientiallabs.ai/",
        "aliases": ["explabs", "experiential labs", "experientiallabs"],
        "hostnames": ["api.experientiallabs.ai", "experientiallabs.ai"],
        "fields": [],
        "notes": "promo free daily tier; daily numbers read off the 429 body, hourly cap from the docs",
        "models": [
            {
                "id": "gpt-6-astra",
                "caps": ["text", "vision", "tools", "json", "reasoning"],
                "context": 1050000,
                "reset_tz": "UTC",
                "free": EXPLABS_PROMO_FREE,
                "notes": "promo row; not ZDR, public data only",
            },
            {
                "id": "claude-fable-5.1",
                "caps": ["text", "vision", "tools", "json", "reasoning"],
                "context": 1000000,
                "reset_tz": "UTC",
                "free": EXPLABS_PROMO_FREE,
                "notes": "promo row; windows assumed same as gpt-6-astra",
            },
            {
                "id": "gpt-5.6-luna",
                "caps": ["text", "tools", "json"],
                "reset_tz": "UTC",
                "free": EXPLABS_PROMO_FREE,
                "notes": "promo row; vision not confirmed",
            },
            {
                "id": "deepseek-v4-flash",
                "caps": ["text", "tools", "json"],
                "reset_tz": "UTC",
                "free": EXPLABS_PROMO_FREE,
                "notes": "promo row; vision not confirmed",
            },
            {
                "id": "qwen3.8-27b",
                "caps": ["text", "tools", "json"],
                "reset_tz": "UTC",
                "free": EXPLABS_PROMO_FREE,
                "notes": "promo row; vision not confirmed",
            },
        ],
    },
    {
        "id": "zai",
        "kind": "openai",
        "base_url": "https://api.z.ai/api/paas/v4",
        "api_key_env": "ZAI_API_KEY",
        "docs_url": "https://z.ai/manage-apikey/rate-limits",
        "aliases": ["zai", "z.ai", "z ai", "zhipu", "bigmodel", "glm"],
        "hostnames": ["api.z.ai", "z.ai", "open.bigmodel.cn", "bigmodel.cn"],
        "fields": [],
        "notes": "flash models are free on the public pricing page; no numeric RPM published",
        "models": [
            {
                "id": "glm-4.5-flash",
                "caps": ["text", "tools"],
                "free": {},
                "extra_body": {"thinking": {"type": "disabled"}},
                "notes": "free on the public pricing page",
            },
            {
                "id": "glm-4.6v-flash",
                "caps": ["text", "vision"],
                "free": {},
                "notes": "free on the public pricing page; seen returning code 1305 (transient overload)",
            },
        ],
    },
    {
        "id": "dashscope",
        "kind": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/",
        "aliases": ["dashscope", "model studio", "alibaba", "alibaba cloud", "aliyun", "bailian", "qwen"],
        "hostnames": [
            "dashscope-intl.aliyuncs.com",
            "dashscope.aliyuncs.com",
            "aliyuncs.com",
            "alibabacloud.com",
            "bailian.console.aliyun.com",
        ],
        "fields": [],
        "notes": "one-time 1M token bucket per model, 90 days from activation, no refill",
        "models": [
            {
                "id": "qwen3.7-plus",
                "caps": ["text", "tools", "json"],
                "free": {"allowance": {"total_tokens": 1000000, "expires_at": None}},
                "extra_body": {"enable_thinking": False},
                "notes": "set expires_at to activation date plus 90 days once known",
            },
            {
                "id": "qwen3-vl-plus",
                "caps": ["text", "vision"],
                "free": {"allowance": {"total_tokens": 1000000, "expires_at": None}},
                "notes": "same one-time 1M/90-day allowance",
            },
        ],
    },
    {
        "id": "openrouter",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "docs_url": "https://openrouter.ai/docs",
        "aliases": ["openrouter", "open router"],
        "hostnames": ["openrouter.ai"],
        "fields": [],
        "notes": "20 RPM, 50 RPD without top-up",
        "models": [
            {
                "id": "minimax/minimax-m3:free",
                "caps": ["text", "vision"],
                "free": {},
                "notes": "20 RPM, 50 RPD without top-up",
            },
            {
                "id": "thinkingmachines/inkling-small:free",
                "caps": ["text", "vision"],
                "free": {},
                "notes": "20 RPM, 50 RPD without top-up",
            },
            {
                "id": "nvidia/nemotron-3.5-lightning:free",
                "caps": ["text"],
                "free": {},
                "notes": "20 RPM, 50 RPD without top-up",
            },
            {
                "id": "poolside/laguna-s-2.1:free",
                "caps": ["text"],
                "free": {},
                "notes": "20 RPM, 50 RPD without top-up",
            },
            {
                "id": "openrouter/free",
                "caps": ["text"],
                "free": {},
                "notes": "auto-router across the free pool; 20 RPM, 50 RPD without top-up",
            },
        ],
    },
    {
        "id": "gemini",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "aliases": ["gemini", "google ai studio", "ai studio", "google gemini", "generative language"],
        "hostnames": [
            "generativelanguage.googleapis.com",
            "aistudio.google.com",
            "ai.google.dev",
        ],
        "fields": [],
        # the vendor lists its ids as "models/<id>"; the OpenAI-compatible endpoint takes the
        # bare id and answers 404 for the prefixed one
        "strip_prefix": "models/",
        # the free tier answers "You exceeded your current quota" with no window word in it,
        # and its per-minute and per-day caps both come back quickly, so a scopeless quota
        # error counts as hourly here instead of parking the model until midnight
        "quota_scope_default": "hourly",
        # the OpenAI-compatible /models listing above serves id/object/owned_by only - no
        # context field anywhere on the row. The native listing carries inputTokenLimit, but
        # wants the key as `x-goog-api-key` rather than a bearer token, and its id lives under
        # `name` (same "models/<id>" shape `strip_prefix` already handles).
        "context_discover": {
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "id_field": "name",
            "auth_header": "x-goog-api-key",
        },
        "notes": "limits per account in AI Studio",
        "models": [
            {
                "id": "gemini-3.8-flash",
                "caps": ["text", "vision", "tools", "json"],
                "context": 1048576,
                "free": {},
                "notes": "AI Studio free tier; limits per account",
            },
            {
                "id": "gemini-3.5-flash",
                "caps": ["text", "vision", "tools", "json"],
                "context": 1048576,
                "free": {},
                "notes": "AI Studio free tier; limits per account",
            },
            {
                "id": "gemini-3.5-flash-lite",
                "caps": ["text", "vision", "tools", "json"],
                "context": 1048576,
                "free": {},
                "notes": "AI Studio free tier; limits per account",
            },
        ],
    },
    {
        "id": "groq",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "docs_url": "https://console.groq.com/docs/openai",
        "aliases": ["groq", "groqcloud", "groq cloud"],
        "hostnames": ["api.groq.com", "console.groq.com", "groq.com"],
        "fields": [],
        "notes": "free tier is capped in requests per day, not tokens; the hub counts tokens only",
        "models": [
            {
                "id": "openai/gpt-oss-120b",
                "caps": ["text", "tools", "json"],
                "context": 131072,
                "free": {},
                "max_request_tokens": 8000,
                "notes": "free tier around 1000 requests/day per account; 8000 TPM cap published for the free tier",
            },
            {
                "id": "qwen/qwen3-32b",
                "caps": ["text", "tools", "json"],
                "free": {},
                "max_request_tokens": 8000,
                "notes": "free tier around 1000 requests/day per account; 8000 TPM cap published for the free tier",
            },
        ],
    },
    {
        "id": "cerebras",
        "kind": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "docs_url": "https://inference-docs.cerebras.ai/",
        "aliases": ["cerebras", "cerebras cloud"],
        "hostnames": ["api.cerebras.ai", "cloud.cerebras.ai", "cerebras.ai"],
        "fields": [],
        # /v1/models answers id/object/created/owned_by only, and the per-model detail endpoint
        # (/v1/models/<id>) answers the same four fields - Cerebras has no HTTP-discoverable
        # context window at all. The docs (inference-docs.cerebras.ai/models/overview) publish
        # it instead, split free/paid tier; the free-tier number is what refresh-context's
        # catalog fallback fills here since a free-only hub has nothing to gain from the paid
        # ceiling.
        "notes": "free tier is capped in requests and tokens per day; limits per account in the console",
        "models": [
            {
                "id": "gpt-oss-120b",
                "caps": ["text", "tools", "json"],
                "context": 65536,
                "free": {},
                "max_request_tokens": 30000,
                "notes": "limits per account in the Cerebras console; 30000 uncached TPM cap "
                "published; context is the free-tier window per Cerebras docs (65k free / "
                "131k paid), not discoverable over the API",
            },
            {
                "id": "qwen-3.8-27b",
                "caps": ["text", "vision", "tools", "json"],
                "context": 65536,
                "free": {},
                "max_request_tokens": 30000,
                "notes": "limits per account in the Cerebras console; 30000 uncached TPM cap "
                "published; context is the free-tier window per Cerebras docs (64k free / "
                "128k paid), not discoverable over the API",
            },
        ],
    },
    {
        "id": "sambanova",
        "kind": "openai",
        "base_url": "https://api.sambanova.ai/v1",
        "api_key_env": "SAMBANOVA_API_KEY",
        "docs_url": "https://cloud.sambanova.ai/",
        "aliases": ["sambanova", "samba nova", "sambanova cloud"],
        "hostnames": ["api.sambanova.ai", "cloud.sambanova.ai", "sambanova.ai"],
        "fields": [],
        "notes": "model list changes often; run discover with a key and tick what you need",
        "models": [],
    },
    {
        "id": "opencode-zen",
        "kind": "openai",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_API_KEY",
        "docs_url": "https://opencode.ai/zen",
        "aliases": ["opencode-zen", "opencode zen", "opencode", "zen"],
        "hostnames": ["opencode.ai"],
        "fields": [],
        "notes": "free rows are promo rows and rotate; re-run discover when one stops serving",
        "models": [
            {
                "id": "opencode/muse-spark-1.3-contributor-free",
                "caps": ["text"],
                "free": {},
                "notes": "promo row, limits unknown",
            },
        ],
    },
    {
        "id": "cloudflare-workers-ai",
        "kind": "openai",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "docs_url": "https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/",
        "aliases": [
            "cloudflare-workers-ai",
            "cloudflare workers ai",
            "workers ai",
            "workersai",
            "cloudflare ai",
            "cloudflare",
            "cf",
        ],
        "hostnames": [
            "api.cloudflare.com",
            "dash.cloudflare.com",
            "gateway.ai.cloudflare.com",
            "developers.cloudflare.com",
            "cloudflare.com",
        ],
        "fields": ["account_id"],
        "discover": {
            "url": "https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/models/search?task=Text%20Generation",
            "id_field": "name",
            "notes": "10k neurons/day shared across the account",
        },
        "notes": "base_url needs the Cloudflare account id; the token is an API token with "
        "Account / Workers AI / Read; the free allocation is a daily neuron budget",
        "models": [],
    },
    {
        "id": "nvidia-nim",
        "kind": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "docs_url": "https://build.nvidia.com/",
        "aliases": ["nvidia-nim", "nvidia nim", "nvidia", "nim"],
        "hostnames": ["integrate.api.nvidia.com", "build.nvidia.com", "nvidia.com"],
        "fields": [],
        "notes": "build.nvidia.com hands out credits per account; run discover for the full catalog",
        "models": [
            {
                "id": "moonshotai/kimi-k3",
                "caps": ["text", "tools", "json"],
                "free": {},
                "notes": UNVERIFIED_FREE,
            },
            {
                "id": "deepseek-ai/deepseek-v4-pro",
                "caps": ["text", "tools", "json", "reasoning"],
                "free": {},
                "notes": UNVERIFIED_FREE,
            },
        ],
    },
    {
        "id": "mistral",
        "kind": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "docs_url": "https://docs.mistral.ai/api/",
        "aliases": ["mistral", "mistral ai", "la plateforme", "codestral"],
        "hostnames": ["api.mistral.ai", "console.mistral.ai", "mistral.ai"],
        "fields": [],
        "notes": "free experiment tier on La Plateforme; no verified model list, run discover",
        "models": [],
    },
    {
        "id": "cohere",
        "kind": "openai",
        "base_url": "https://api.cohere.com/compatibility/v1",
        "api_key_env": "COHERE_API_KEY",
        "docs_url": "https://docs.cohere.com/docs/compatibility-api",
        "aliases": ["cohere"],
        "hostnames": ["api.cohere.com", "dashboard.cohere.com", "cohere.com"],
        "fields": [],
        # the compatibility listing above serves id/object/owned_by only; the native v1 listing
        # carries context_length under the same bearer token, id under `name` instead of `id`
        "context_discover": {
            "url": "https://api.cohere.com/v1/models",
            "id_field": "name",
        },
        "notes": "trial keys are rate limited per month; run discover for the full list; "
        "some docs use CO_API_KEY instead of COHERE_API_KEY for the same credential",
        # the compatibility API takes response_format json_object (docs.cohere.com structured
        # outputs section), which the native Cohere API spells differently - so the json cap
        # holds for these ids only through this base_url
        "models": [
            {
                "id": "command-a-plus-05-2026",
                "caps": ["text", "tools", "json"],
                "free": {},
                "notes": UNVERIFIED_FREE,
            },
            {
                "id": "command-a-03-2025",
                "caps": ["text", "tools", "json"],
                "free": {},
                "notes": UNVERIFIED_FREE,
            },
        ],
    },
    {
        "id": "huggingface",
        "kind": "openai",
        "base_url": "https://router.huggingface.co/v1",
        "api_key_env": "HF_TOKEN",
        "docs_url": "https://huggingface.co/docs/inference-providers/",
        "aliases": ["huggingface", "hugging face", "hf", "inference providers"],
        "hostnames": ["router.huggingface.co", "huggingface.co", "hf.co"],
        "fields": [],
        "notes": "router in front of many providers; the monthly credit pool decides what is free",
        "models": [],
    },
    {
        "id": "moonshot",
        "kind": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "docs_url": "https://platform.moonshot.ai/docs",
        "aliases": ["moonshot", "moonshot ai", "kimi"],
        "hostnames": ["api.moonshot.ai", "platform.moonshot.ai", "moonshot.ai", "kimi.com"],
        "fields": [],
        "notes": "signup credits, not a standing free tier; no verified model list, run discover",
        "models": [],
    },
    {
        "id": "deepseek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "docs_url": "https://api-docs.deepseek.com/",
        "aliases": ["deepseek", "deep seek"],
        "hostnames": ["api.deepseek.com", "platform.deepseek.com", "deepseek.com"],
        "fields": [],
        "notes": "off-peak discounts, not free; register only what a promo actually covers",
        "models": [],
    },
    {
        "id": "xai",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "docs_url": "https://docs.x.ai/docs/api-reference",
        "aliases": ["xai", "x.ai", "x ai", "grok"],
        "hostnames": ["api.x.ai", "console.x.ai", "x.ai"],
        "fields": [],
        "notes": "credit promos come and go; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "fireworks",
        "kind": "openai",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "api_key_env": "FIREWORKS_API_KEY",
        "docs_url": "https://docs.fireworks.ai/api-reference/introduction",
        "aliases": ["fireworks", "fireworks ai"],
        "hostnames": ["api.fireworks.ai", "app.fireworks.ai", "fireworks.ai"],
        "fields": [],
        "notes": "signup credits; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "together",
        "kind": "openai",
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "docs_url": "https://docs.together.ai/docs/openai-api-compatibility",
        "aliases": ["together", "together ai", "together.ai", "togetherai"],
        "hostnames": ["api.together.xyz", "together.xyz", "api.together.ai", "together.ai"],
        "fields": [],
        "notes": "a handful of endpoints carry a :free suffix; run discover and tick those",
        "models": [],
    },
    {
        "id": "deepinfra",
        "kind": "openai",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_key_env": "DEEPINFRA_API_KEY",
        "docs_url": "https://deepinfra.com/docs/openai_api",
        "aliases": ["deepinfra", "deep infra"],
        "hostnames": ["api.deepinfra.com", "deepinfra.com"],
        "fields": [],
        "notes": "signup credits; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "minimax",
        "kind": "openai",
        "base_url": "https://api.minimax.io/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "docs_url": "https://platform.minimax.io/docs",
        "aliases": ["minimax", "mini max"],
        "hostnames": ["api.minimax.io", "platform.minimax.io", "minimax.io", "minimaxi.com"],
        "fields": [],
        "notes": "promo rows rotate; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "inception",
        "kind": "openai",
        "base_url": "https://api.inceptionlabs.ai/v1",
        "api_key_env": "INCEPTION_API_KEY",
        "docs_url": "https://platform.inceptionlabs.ai/docs",
        # the docs host answers 403/429 to a plain scripted GET often enough that scout should
        # not hit it every run - once a week is plenty for a page that rarely changes
        "scout_polite": True,
        "aliases": ["inception", "inception labs", "inceptionlabs", "mercury"],
        "hostnames": ["api.inceptionlabs.ai", "platform.inceptionlabs.ai", "inceptionlabs.ai"],
        "fields": [],
        "notes": "diffusion LLM vendor; promo credits rotate, run discover",
        "models": [],
    },
    {
        "id": "ai21",
        "kind": "openai",
        "base_url": "https://api.ai21.com/studio/v1",
        "api_key_env": "AI21_API_KEY",
        "docs_url": "https://docs.ai21.com/",
        "aliases": ["ai21", "ai21 labs", "ai21 studio", "jamba"],
        "hostnames": ["api.ai21.com", "studio.ai21.com", "ai21.com"],
        "fields": [],
        "notes": "trial credits per account; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "upstage",
        "kind": "openai",
        "base_url": "https://api.upstage.ai/v1",
        "api_key_env": "UPSTAGE_API_KEY",
        "docs_url": "https://developers.upstage.ai/",
        "aliases": ["upstage", "upstage ai", "solar pro"],
        "hostnames": ["api.upstage.ai", "console.upstage.ai", "upstage.ai"],
        "fields": [],
        "notes": "signup credits; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "siliconflow",
        "kind": "openai",
        "base_url": "https://api.siliconflow.com/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "docs_url": "https://docs.siliconflow.com/",
        "aliases": ["siliconflow", "silicon flow", "siliconcloud"],
        "hostnames": ["api.siliconflow.com", "siliconflow.com", "api.siliconflow.cn", "siliconflow.cn"],
        "fields": [],
        "notes": "a few endpoints are free, the rest bill credits; run discover and tick those",
        "models": [],
    },
    {
        "id": "novita",
        "kind": "openai",
        "base_url": "https://api.novita.ai/v3/openai",
        "api_key_env": "NOVITA_API_KEY",
        # /docs/api-reference/ 404s (verified 2026-09-09); this is the current landing page
        "docs_url": "https://novita.ai/docs/guides/introduction",
        "aliases": ["novita", "novita ai"],
        "hostnames": ["api.novita.ai", "novita.ai"],
        "fields": [],
        "notes": "signup credits; no verified free model list, run discover; "
        "non-standard v3 version segment, older docs referencing v2 are stale",
        "models": [],
    },
    {
        "id": "hyperbolic",
        "kind": "openai",
        "base_url": "https://api.hyperbolic.xyz/v1",
        "api_key_env": "HYPERBOLIC_API_KEY",
        "docs_url": "https://docs.hyperbolic.xyz/",
        "aliases": ["hyperbolic", "hyperbolic labs"],
        "hostnames": ["api.hyperbolic.xyz", "app.hyperbolic.xyz", "hyperbolic.xyz"],
        "fields": [],
        "notes": "signup credits; no verified free model list, run discover",
        "models": [],
    },
    {
        "id": "nebius",
        "kind": "openai",
        "base_url": "https://api.tokenfactory.nebius.com/v1",
        "api_key_env": "NEBIUS_API_KEY",
        "docs_url": "https://docs.tokenfactory.nebius.com/quickstart",
        "aliases": ["nebius", "nebius ai studio", "nebius studio", "nebius token factory", "token factory"],
        "hostnames": [
            "api.tokenfactory.nebius.com",
            "tokenfactory.nebius.com",
            "studio.nebius.ai",
            "nebius.com",
        ],
        "fields": [],
        "notes": "trial credits per account; no verified free model list, run discover; "
        "rebranded from Nebius AI Studio, legacy host api.studio.nebius.ai still responds",
        "models": [],
    },
    {
        "id": "chutes",
        "kind": "openai",
        "base_url": "https://llm.chutes.ai/v1",
        "api_key_env": "CHUTES_API_KEY",
        "docs_url": "https://chutes.ai/",
        "aliases": ["chutes", "chutes ai"],
        "hostnames": ["llm.chutes.ai", "api.chutes.ai", "chutes.ai"],
        "fields": [],
        "notes": "quota is a daily request budget per account; run discover for the catalog",
        "models": [],
    },
    {
        "id": "ollama",
        "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None,
        # the docs/openai.md path in the ollama repo 404s (verified 2026-09-09); the OpenAI
        # compatibility page moved to the docs site
        "docs_url": "https://docs.ollama.com/openai",
        "aliases": ["ollama"],
        "hostnames": [],
        "fields": [],
        "notes": "local, no key; set concurrency so the Mac does not run two generations at once; "
        "distinct from ollama-cloud, which is the hosted https://ollama.com/v1 service",
        "models": [
            {
                "id": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "caps": ["text", "json"],
                "free": {},
                "concurrency": 1,
            },
            {
                "id": "gemma3:27b",
                "caps": ["text", "vision"],
                "free": {},
                "concurrency": 1,
                "notes": "vision cap taken from the model family, not exercised locally",
            },
        ],
    },
    {
        "id": "requesty",
        "kind": "openai",
        "base_url": "https://router.requesty.ai/v1",
        "api_key_env": "REQUESTY_API_KEY",
        "docs_url": "https://docs.requesty.ai/quickstart",
        "aliases": ["requesty", "requesty router"],
        "hostnames": ["requesty.ai", "router.requesty.ai"],
        "fields": [],
        "notes": "EU data residency variant at router.eu.requesty.ai; model ids use provider/model format, "
        "e.g. openai/gpt-4.1",
        "models": [],
    },
    {
        "id": "scaleway",
        "kind": "openai",
        "base_url": "https://api.scaleway.ai/v1",
        "api_key_env": "SCW_API_KEY",
        "docs_url": "https://www.scaleway.com/en/docs/generative-apis/reference-content/openai-compatibility/",
        "aliases": ["scaleway", "scaleway generative apis"],
        "hostnames": ["scaleway.com", "scaleway.ai"],
        "fields": [],
        "notes": "docs also call the credential SCW_SECRET_KEY depending on the code sample; "
        "dedicated deployments use https://<deployment-uuid>.ifr.fr-par.scaleway.com/v1 instead",
        "models": [],
    },
    {
        "id": "zenmux",
        "kind": "openai",
        "base_url": "https://zenmux.ai/api/v1",
        "api_key_env": "ZENMUX_API_KEY",
        "docs_url": "https://docs.zenmux.ai/guide/quickstart",
        "aliases": ["zenmux", "zen mux"],
        "hostnames": ["zenmux.ai"],
        "fields": [],
        "notes": "separate Anthropic-protocol base at https://zenmux.ai/api/anthropic",
        "models": [
            {
                "id": "moonshotai/kimi-k3-free",
                "caps": ["text"],
                "free": {},
                "notes": "from docs, unverified",
            },
        ],
    },
    {
        "id": "pollinations",
        "kind": "openai",
        "base_url": "https://text.pollinations.ai/openai",
        "api_key_env": "POLLINATIONS_API_KEY",
        "docs_url": "https://gen.pollinations.ai/docs",
        "aliases": ["pollinations", "pollinations.ai", "pollinations ai"],
        "hostnames": ["pollinations.ai", "gen.pollinations.ai", "text.pollinations.ai"],
        "fields": [],
        "notes": "newer unified gateway also documented at base_url=gen.pollinations.ai; "
        "many text models work without a key on the rate-limited anonymous tier",
        "models": [
            {
                "id": "openai-fast",
                "caps": ["text"],
                "free": {},
                "notes": "from docs, unverified",
            },
            {
                "id": "mistral",
                "caps": ["text"],
                "free": {},
                "notes": "from docs, unverified",
            },
            {
                "id": "llama-fast-roblox",
                "caps": ["text"],
                "free": {},
                "notes": "from docs, unverified",
            },
        ],
    },
    {
        "id": "vercel-ai-gateway",
        "kind": "openai",
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "api_key_env": "AI_GATEWAY_API_KEY",
        "docs_url": "https://vercel.com/docs/ai-gateway/pricing",
        "aliases": ["vercel-ai-gateway", "vercel ai gateway", "ai gateway"],
        "hostnames": ["vercel.com", "ai-gateway.vercel.sh"],
        "fields": [],
        "notes": "also accepts a Vercel OIDC token instead of a static API key when run inside a Vercel deployment",
        "models": [],
    },
    {
        "id": "modelscope",
        "kind": "openai",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "api_key_env": "MODELSCOPE_API_KEY",
        "docs_url": "https://modelscope.ai/docs/model-service/API-Inference/limits",
        "aliases": ["modelscope", "model scope"],
        "hostnames": ["modelscope.ai", "modelscope.cn"],
        "fields": [],
        "notes": "requires Alibaba Cloud account binding plus real-name verification; "
        "model ids use org/model format despite the .cn host being reachable globally",
        "models": [
            {
                "id": "Qwen/Qwen3-8B",
                "caps": ["text"],
                "free": {},
                "notes": "from docs, unverified",
            },
        ],
    },
    {
        "id": "tencent-hunyuan",
        "kind": "openai",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "api_key_env": "HUNYUAN_API_KEY",
        "docs_url": "https://cloud.tencent.com/document/product/1729/97731",
        "aliases": ["tencent-hunyuan", "tencent hunyuan", "hunyuan"],
        "hostnames": ["cloud.tencent.com", "hunyuan.cloud.tencent.com"],
        "fields": [],
        "notes": "credential is a Tencent Cloud API key (SecretId/SecretKey-derived), "
        "not a distinct Hunyuan-only secret",
        "models": [],
    },
    {
        "id": "ovh-ai-endpoints",
        "kind": "openai",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "api_key_env": "OVH_AI_ENDPOINTS_API_KEY",
        "docs_url": "https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-capabilities",
        "aliases": ["ovh-ai-endpoints", "ovh ai endpoints", "ovhcloud ai endpoints", "ovhcloud"],
        "hostnames": ["ovhcloud.com", "ovh.net"],
        "fields": [],
        "notes": "overridable via OVHCLOUD_BASE_URL; 'kepler' is the current region/cluster name and may vary; "
        "key comes from Manager > Public Cloud > AI & Machine Learning > AI Endpoints",
        "models": [],
    },
    {
        "id": "kilo",
        "kind": "openai",
        "base_url": "https://api.kilo.ai/api/gateway",
        "api_key_env": "KILO_API_KEY",
        "docs_url": "https://kilo.ai/docs/gateway/models-and-providers",
        "aliases": ["kilo", "kilo ai", "kilo gateway", "kilocode"],
        "hostnames": ["kilo.ai"],
        "fields": [],
        "notes": "no /v1 segment; model ids are namespaced kilocode/<provider>/<model>",
        "models": [],
    },
    {
        "id": "perplexity",
        "kind": "openai",
        "base_url": "https://api.perplexity.ai",
        "api_key_env": "PERPLEXITY_API_KEY",
        "docs_url": "https://docs.perplexity.ai",
        "aliases": ["perplexity", "perplexity ai"],
        "hostnames": ["perplexity.ai", "api.perplexity.ai"],
        "fields": [],
        "notes": "no /models endpoint, discover will fail; register models by hand",
        "models": [],
    },
    {
        "id": "featherless",
        "kind": "openai",
        "base_url": "https://api.featherless.ai/v1",
        "api_key_env": "FEATHERLESS_API_KEY",
        "docs_url": "https://featherless.ai/docs/quickstart-guide",
        "aliases": ["featherless", "featherless ai"],
        "hostnames": ["featherless.ai"],
        "fields": [],
        "notes": "serverless hosting of 37,000+ open-weight models; free tier is heavily rate/context limited",
        "models": [],
    },
    {
        "id": "baseten",
        "kind": "openai",
        "base_url": "https://inference.baseten.co/v1",
        "api_key_env": "BASETEN_API_KEY",
        "docs_url": "https://docs.baseten.co/inference/model-apis/overview",
        "aliases": ["baseten"],
        "hostnames": ["baseten.co"],
        "fields": [],
        "notes": "model id must be a Baseten model slug, not a generic name; "
        "also exposes a beta Anthropic Messages-compatible path at /v1/messages",
        "models": [],
    },
    {
        "id": "friendli",
        "kind": "openai",
        "base_url": "https://api.friendli.ai/serverless/v1",
        "api_key_env": "FRIENDLI_TOKEN",
        "docs_url": "https://friendli.ai/docs/guides/serverless_endpoints/openai-compatibility",
        "aliases": ["friendli", "friendli ai", "friendli serverless"],
        "hostnames": ["friendli.ai"],
        "fields": [],
        "notes": "dedicated (non-serverless) endpoints use /dedicated/v1 instead; "
        "key comes from Friendli Suite > Personal Settings > API Keys",
        "models": [],
    },
    {
        "id": "poe",
        "kind": "openai",
        "base_url": "https://api.poe.com/v1",
        "api_key_env": "POE_API_KEY",
        "docs_url": "https://creator.poe.com/docs/external-applications/openai-compatible-api",
        "aliases": ["poe"],
        "hostnames": ["poe.com"],
        "fields": [],
        "notes": "models are Poe bot names (e.g. Claude-Sonnet-4.6, GPT-5.4), paid via Poe points/subscription "
        "rather than a pay-as-you-go key balance",
        "models": [],
    },
    {
        "id": "ollama-cloud",
        "kind": "openai",
        "base_url": "https://ollama.com/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "docs_url": "https://docs.ollama.com/api/openai-compatibility",
        "aliases": ["ollama cloud", "ollama.com"],
        "hostnames": ["ollama.com"],
        "fields": [],
        "notes": "distinct from local Ollama's http://127.0.0.1:11434/v1 (no auth needed locally); "
        "cloud requires an Ollama account API key",
        "models": [],
    },
    {
        # not an HTTP endpoint: the Antigravity CLI is logged in interactively and the hub runs
        # it headless. `command` replaces base_url, and the account carries no key.
        "id": "antigravity",
        "kind": "cli",
        "base_url": "",
        "command": "agy",
        "api_key_env": None,
        "docs_url": "https://antigravity.google/pricing",
        "aliases": ["antigravity", "agy", "google antigravity"],
        "hostnames": ["antigravity.google"],
        "fields": [],
        # the CLI says "quota"/"rate limit" without naming a window; the free plan is a daily
        # allowance, so a scopeless quota error parks the model until midnight
        "quota_scope_default": "daily",
        "notes": "free plan of the Antigravity agent CLI; quota unpublished and reset window "
        "unconfirmed; Google may use the data, so public data only. Sign in with `agy` in a "
        "terminal - there is no key to paste.",
        "models": [
            {
                "id": "gemini-3.8-flash-high",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.8-flash-medium",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.8-flash-low",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.7-flash-high",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.7-flash-medium",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.7-flash-low",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.6-flash-high",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.6-flash-medium",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.6-flash-low",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.1-pro-high",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gemini-3.1-pro-low",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "claude-sonnet-4-6",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "claude-opus-4-6-thinking",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
            {
                "id": "gpt-oss-120b-medium",
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "notes": ANTIGRAVITY_MODEL_NOTE,
            },
        ],
    },
    {
        # also not an HTTP endpoint, and not free either: the seat is not the owner's to spend
        # freely, so this one is registered outside the routing aliases and reached only on
        # purpose.
        "id": "copilot",
        "kind": "cli",
        "base_url": "",
        "command": "copilot",
        "api_key_env": None,
        "docs_url": "https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli",
        "aliases": ["copilot", "github copilot", "gh copilot"],
        "hostnames": ["github.com", "githubcopilot.com"],
        "fields": [],
        # usage is metered in AI credits (legacy accounts: premium requests), and both renew
        # on the billing month, so a quota error that names no window parks the model till then
        "quota_scope_default": "monthly",
        # The CLI's own error text names these three as ways to authenticate, and `gh` on this
        # machine is signed in to a personal account: an inherited token would silently pick
        # that identity over the CLI's login. COPILOT_ALLOW_ALL is the env form of
        # `--allow-all`, which the hub must never grant.
        "env_deny": ["GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_ALLOW_ALL"],
        "extra_args": [],
        "notes": "a Copilot seat that is not the owner's to spend freely, not a free tier: the "
        "seat holder sees usage and audit logs, so do not route private-project traffic here. "
        "Registered outside auto/vision/fast/strong - "
        "reachable as copilot/<model> or the `copilot` alias only. Sign in with `copilot login` "
        "in a terminal; there is no key to paste, and no model listing either (the ids come "
        "from this template).",
        "models": [
            {
                "id": model_id,
                "caps": ["text", "json", "reasoning"],
                "free": {},
                "max_ai_credits": COPILOT_MAX_AI_CREDITS,
                "notes": COPILOT_MODEL_NOTE,
            }
            for model_id in COPILOT_MODEL_IDS
        ],
    },
    {
        "id": "custom",
        "kind": "openai",
        "base_url": "",
        "api_key_env": None,
        "docs_url": None,
        "aliases": [],
        "hostnames": [],
        "fields": [],
        "notes": "anything OpenAI-compatible; fill base_url and the env var name by hand",
        "models": [],
    },
)

TEMPLATES_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in PROVIDER_TEMPLATES}


def known_providers() -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in PROVIDER_TEMPLATES]


def template(template_id: str) -> dict[str, Any] | None:
    found = TEMPLATES_BY_ID.get(template_id)
    return copy.deepcopy(found) if found else None


def discover_spec(template_id: str | None) -> dict[str, Any] | None:
    """The template's own discovery endpoint, or None to use `GET {base_url}/models`."""
    known = TEMPLATES_BY_ID.get(template_id or "")
    spec = known.get("discover") if known else None
    if isinstance(spec, dict) and spec.get("url"):
        return copy.deepcopy(spec)
    return None


def context_discover_spec(template_id: str | None) -> dict[str, Any] | None:
    """Where to find the context window when it is not on the listing `discover`/base_url
    already use to register accounts.

    Gemini and Cohere both front an OpenAI-compatible `/models` that answers id/object/owned_by
    only; their own native listing carries the context field but wants different auth (gemini:
    an `x-goog-api-key` header instead of a bearer token) and reads the id off a different key
    (`name`, not `id`). `refresh_context_report` tries this endpoint only for models the primary
    listing left without a context.
    """
    known = TEMPLATES_BY_ID.get(template_id or "")
    spec = known.get("context_discover") if known else None
    if isinstance(spec, dict) and spec.get("url"):
        return copy.deepcopy(spec)
    return None


def catalog_context(template_id: str | None) -> dict[str, int]:
    """model id -> context, for the ids the template hard-codes one on.

    Last-resort fallback for `refresh_context_report`: a vendor like Cerebras publishes no
    context anywhere over HTTP (not on `/v1/models`, not on the per-model detail endpoint
    either), so the only source left is what the catalog entry itself was seeded with from the
    vendor's docs.
    """
    known = TEMPLATES_BY_ID.get(template_id or "")
    out: dict[str, int] = {}
    for model in (known or {}).get("models") or ():
        if not isinstance(model, dict):
            continue
        context = model.get("context")
        model_id = model.get("id")
        if isinstance(context, int) and context > 0 and isinstance(model_id, str):
            out[model_id] = context
    return out


def quota_scope_default(template_id: str | None) -> str | None:
    """Window a quota error from this vendor means when its text names none."""
    known = TEMPLATES_BY_ID.get(template_id or "")
    scope = (known or {}).get("quota_scope_default")
    return str(scope) if scope else None


def render_base_url(base_url: str, fields: dict[str, str]) -> str:
    rendered = base_url
    for name, value in fields.items():
        rendered = rendered.replace("{" + name + "}", str(value).strip())
    return rendered


def missing_fields(base_url: str) -> list[str]:
    return FIELD_RE.findall(base_url or "")


# Vendors serve one /models list for the whole product: embeddings, rerankers, speech, image
# and robotics endpoints sit next to the chat ones and answer a chat completion with a 400 or
# a 404. Discovery registers what it finds, so the markers below keep the non-chat half out of
# the registry instead of leaving it to fail at routing time.
NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed",
    "embedding",
    "rerank",
    "transcribe",
    "parse",
    "tts",
    "-image",
    "image-",
    "audio",
    "live",
    "robotics",
    "computer-use",
    "translate",
    "whisper",
    "guard",
    "moderation",
    "tiny-aya",
)


def deny_markers(template_id: str | None = None) -> tuple[str, ...]:
    """Global markers plus whatever the template adds in `deny_markers`."""
    known = TEMPLATES_BY_ID.get(template_id or "")
    extra = (known or {}).get("deny_markers") or ()
    return NON_CHAT_MARKERS + tuple(str(item).lower() for item in extra)


def id_prefix(template_id: str | None = None) -> str:
    """The prefix the vendor puts on the ids it lists but not on the ids it accepts."""
    known = TEMPLATES_BY_ID.get(template_id or "")
    return str((known or {}).get("strip_prefix") or "")


def normalize_model_id(model_id: str, template_id: str | None = None) -> str:
    prefix = id_prefix(template_id)
    text = str(model_id).strip()
    return text[len(prefix) :] if prefix and text.startswith(prefix) else text


def is_chat_model_id(model_id: str, template_id: str | None = None) -> bool:
    name = str(model_id).lower()
    return not any(marker in name for marker in deny_markers(template_id))


def chat_model_ids(ids: list[str], template_id: str | None = None) -> list[str]:
    """Discovery output a chat gateway can actually route: prefix stripped, non-chat dropped."""
    kept: list[str] = []
    for model_id in ids:
        name = normalize_model_id(model_id, template_id)
        if name and is_chat_model_id(name, template_id) and name not in kept:
            kept.append(name)
    return kept
