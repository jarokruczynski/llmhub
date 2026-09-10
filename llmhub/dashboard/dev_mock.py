from __future__ import annotations

import argparse
import random
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from llmhub.api import REJECT_REASON_MIN, REJECT_REASON_REQUIRED
from llmhub.dashboard.routes import router

REQUIRE_TOKEN: str | None = None
SCOUT_SOURCES_COUNT = 5
SCOUT_RUN_SECONDS = 7.0


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _models() -> list[dict[str, Any]]:
    t = now()
    return [
        {
            "key": "explabs/gpt-6-astra",
            "provider": "explabs",
            "account": "explabs-main",
            "model": "gpt-6-astra",
            "caps": ["text", "vision", "tools", "json", "reasoning"],
            "status": "exhausted",
            "windows": {
                "hourly": {
                    "used": 30000,
                    "limit": 30000,
                    "metric": "out_tokens",
                    "resets_at": iso(t + timedelta(minutes=40)),
                    "limit_source": "declared",
                },
                "daily": {
                    "used": 61240,
                    "limit": 75000,
                    "metric": "out_tokens",
                    "resets_at": iso(
                        (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    ),
                    "limit_source": "observed",
                },
                "monthly": None,
            },
            "last_error": "insufficient_quota: You exceeded your current quota for the free tier, "
            "please check your plan and billing details (request_id 8f1c2)",
            "last_error_at": iso(t - timedelta(minutes=4)),
            "last_ok_at": iso(t - timedelta(minutes=52)),
            "avg_latency_ms": 2140,
            "usage_today": {
                "requests": 340,
                "in_tokens": 612000,
                "out_tokens": 61240,
                "errors": 18,
                "last_used_at": iso(t - timedelta(minutes=4)),
            },
            "observed": {
                "daily": {"out_tokens": 75000, "observed_at": iso(t - timedelta(minutes=52))},
            },
        },
        {
            "key": "explabs/claude-fable-5.1",
            "provider": "explabs",
            "account": "explabs-main",
            "model": "claude-fable-5.1",
            "caps": ["text", "vision", "tools", "json", "reasoning"],
            "status": "ok",
            "windows": {
                "hourly": {
                    "used": 8120,
                    "limit": 30000,
                    "metric": "out_tokens",
                    "resets_at": iso(t + timedelta(minutes=40)),
                    "limit_source": "declared",
                },
                "daily": {
                    "used": 24310,
                    "limit": 75000,
                    "metric": "out_tokens",
                    "resets_at": iso(
                        (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    ),
                    "limit_source": "declared",
                },
                "monthly": None,
            },
            "last_error": None,
            "last_ok_at": iso(t - timedelta(minutes=2)),
            "avg_latency_ms": 3305,
            "usage_today": {
                "requests": 812,
                "in_tokens": 980000,
                "out_tokens": 24310,
                "errors": 0,
                "last_used_at": iso(t - timedelta(minutes=2)),
            },
        },
        {
            "key": "zai/glm-4.5-flash",
            "provider": "zai",
            "account": "zai-main",
            "model": "glm-4.5-flash",
            "caps": ["text", "tools"],
            "status": "cooldown",
            "windows": {"hourly": None, "daily": None, "monthly": None},
            "last_error": "429 Too Many Requests (code 1302), retry after 15s",
            "last_error_at": iso(t - timedelta(minutes=6)),
            "last_ok_at": iso(t - timedelta(minutes=6)),
            "avg_latency_ms": 880,
            "usage_today": {
                "requests": 58,
                "in_tokens": 41000,
                "out_tokens": 4200,
                "errors": 9,
                "last_used_at": iso(t - timedelta(minutes=6)),
            },
            "observed": {
                "hourly": {"out_tokens": 4200, "observed_at": iso(t - timedelta(minutes=6))},
            },
        },
        {
            "key": "zai/glm-4.6v-flash",
            "provider": "zai",
            "account": "zai-main",
            "model": "glm-4.6v-flash",
            "caps": ["text", "vision"],
            "status": "down",
            "windows": {"hourly": None, "daily": None, "monthly": None},
            "last_error": "httpx.ConnectError: [Errno 60] Operation timed out while connecting to "
            "api.z.ai:443 after 3 attempts",
            "last_error_at": iso(t - timedelta(minutes=5)),
            "last_ok_at": iso(t - timedelta(hours=9)),
            "avg_latency_ms": 1520,
        },
        {
            # a request-metered free tier: the daily window caps calls, not tokens, so the
            # cell reads "12/20 req" and remaining_out stays unknown
            "key": "gemini/gemini-3.8-flash",
            "provider": "gemini",
            "account": "gemini-main",
            "model": "gemini-3.8-flash",
            "caps": ["text", "vision", "tools", "json"],
            "status": "ok",
            "windows": {
                "hourly": None,
                "daily": {
                    "used": 12,
                    "limit": 20,
                    "metric": "requests",
                    "resets_at": iso(
                        (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    ),
                    "limit_source": "observed",
                },
                "monthly": None,
            },
            "remaining_requests": 8,
            "last_error": "429 RESOURCE_EXHAUSTED: quota exceeded for metric "
            "generate_content_free_tier_requests, limit 20",
            "last_error_at": iso(t - timedelta(minutes=18)),
            "last_ok_at": iso(t - timedelta(minutes=3)),
            "avg_latency_ms": 1240,
            "usage_today": {
                "requests": 12,
                "in_tokens": 84000,
                "out_tokens": 9100,
                "errors": 2,
                "last_used_at": iso(t - timedelta(minutes=3)),
            },
            "observed": {
                "daily": {"requests": 20, "observed_at": iso(t - timedelta(minutes=18))},
            },
        },
        {
            "key": "dashscope/qwen3.7-plus",
            "provider": "dashscope",
            "account": "dashscope-main",
            "model": "qwen3.7-plus",
            "caps": ["text", "tools", "json"],
            "status": "ok",
            "windows": {
                "hourly": None,
                "daily": None,
                "monthly": {
                    "used": 612400,
                    "limit": 1000000,
                    "metric": "total_tokens",
                    "resets_at": iso(
                        (
                            t.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
                        ).replace(day=1)
                    ),
                    "limit_source": "declared",
                },
            },
            "last_error": None,
            "last_ok_at": iso(t - timedelta(minutes=11)),
            "avg_latency_ms": 1970,
            "usage_today": {
                "requests": 196,
                "in_tokens": 310000,
                "out_tokens": 18400,
                "errors": 0,
                "last_used_at": iso(t - timedelta(minutes=11)),
            },
        },
        {
            "key": "dashscope/qwen3-vl-plus",
            "provider": "dashscope",
            "account": "dashscope-main",
            "model": "qwen3-vl-plus",
            "caps": ["text", "vision"],
            "status": "down",
            "reason": "disabled",
            "disabled": True,
            "windows": {
                "hourly": None,
                "daily": None,
                "monthly": {
                    "used": 118900,
                    "limit": 1000000,
                    "metric": "total_tokens",
                    "resets_at": iso(
                        (
                            t.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
                        ).replace(day=1)
                    ),
                    "limit_source": "declared",
                },
            },
            "last_error": "free quota has been exhausted (Allocation.Quota.Exhausted)",
            "last_error_at": iso(t - timedelta(days=2)),
            "last_ok_at": iso(t - timedelta(days=2)),
            "avg_latency_ms": 2610,
        },
    ]


def _promos() -> list[dict[str, Any]]:
    t = now()

    def row(
        promo_id: int,
        provider: str,
        url: str,
        note: str,
        status: str,
        found_delta: timedelta,
        source: str,
        **kw: Any,
    ) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": promo_id,
            "provider": provider,
            "url": url,
            "note": note,
            "found_at": iso(t - found_delta),
            "status": status,
            "account_key": kw.get("account_key"),
            "source": source,
        }
        if "expires_delta" in kw:
            d["expires_at"] = iso(t + kw["expires_delta"])
        if "updates_count" in kw:
            d["updates_count"] = kw["updates_count"]
            d["updated_at"] = iso(t - kw.get("updated_delta", timedelta(hours=2)))
        if "base_url" in kw:
            d["base_url"] = kw["base_url"]
        if "api_key_env" in kw:
            d["api_key_env"] = kw["api_key_env"]
        if "rejected_reason" in kw:
            d["rejected_reason"] = kw["rejected_reason"]
            d["rejected_at"] = iso(t - kw.get("rejected_delta", timedelta(days=1)))
        return d

    return [
        row(
            1,
            "explabs",
            "https://experientiallabs.ai/blog/free-tier-bump",
            "hourly out_tokens 30k -> 50k, unconfirmed",
            "new",
            timedelta(days=3),
            "pepper.pl",
        ),
        row(
            2,
            "groq",
            "https://console.groq.com/promo/free-daily",
            "daily request cap raised for new keys",
            "new",
            timedelta(days=2),
            "manual",
        ),
        row(
            3,
            "flexon",
            "https://api.flexon.dev/promo/launch-week",
            "new aggregator, not in the catalog yet",
            "new",
            timedelta(hours=20),
            "scout",
            base_url="https://api.flexon.dev/v1",
            api_key_env="FLEXON_API_KEY",
        ),
        row(
            4,
            "dashscope",
            "https://dashscope-intl.aliyuncs.com/promo/qwen-newyear",
            "1M free tokens per model, needs intl account",
            "known",
            timedelta(days=11),
            "pepper.pl",
            expires_delta=timedelta(days=5),
        ),
        row(
            5,
            "zai",
            "https://z.ai/pricing",
            "glm-4.6-flash may go free",
            "known",
            timedelta(days=1),
            "manual",
        ),
        row(
            6,
            "novaai",
            "https://novaai.dev/promo/launch",
            "launch week, 500k free tokens",
            "used",
            timedelta(days=6),
            "scout",
            account_key="novaai/novaai-main",
        ),
        row(
            7,
            "groq",
            "https://console.groq.com/promo/summer",
            "summer bump, already rolled back",
            "expired",
            timedelta(days=30),
            "manual",
            expires_delta=-timedelta(days=14),
        ),
        row(
            8,
            "mistral",
            "https://mistral.ai/news/free-tier",
            "codestral free tier widened",
            "new",
            timedelta(hours=5),
            "twitter",
        ),
        row(
            9,
            "cohere",
            "https://cohere.com/pricing",
            "trial key rate limit raised",
            "known",
            timedelta(days=4),
            "discord",
            expires_delta=timedelta(days=2),
        ),
        row(
            10,
            "together",
            "https://together.ai/promo/launch-week",
            "launch week free credits",
            "used",
            timedelta(days=1),
            "vendor docs, promo-hunt 2026-09-08",
            account_key="together/together-main",
            updates_count=3,
            updated_delta=timedelta(hours=3),
        ),
        row(
            11,
            "fireworks",
            "https://fireworks.ai/pricing",
            "free tier, needs waitlist approval",
            "expired",
            timedelta(days=40),
            "vendor docs, promo-hunt 2026-08-01",
            expires_delta=-timedelta(days=20),
        ),
        row(
            12,
            "openrouter",
            "https://openrouter.ai/models?free=1",
            "new free model added to catalog",
            "new",
            timedelta(hours=8),
            "reddit",
        ),
        row(
            13,
            "huggingface",
            "https://huggingface.co/inference-api",
            "serverless inference free quota",
            "known",
            timedelta(days=9),
            "scout",
        ),
        row(
            14,
            "replicate",
            "https://replicate.com/pricing",
            "free trial credits on signup",
            "used",
            timedelta(days=15),
            "manual",
            account_key="replicate/replicate-main",
        ),
        row(
            15,
            "anyscale",
            "https://anyscale.com/endpoints",
            "endpoints free tier, discontinued",
            "expired",
            timedelta(days=60),
            "pepper.pl",
            expires_delta=-timedelta(days=30),
        ),
        row(
            16,
            "perplexity",
            "https://perplexity.ai/api-platform",
            "sonar free tier for new accounts",
            "new",
            timedelta(hours=2),
            "scout",
        ),
        row(
            17,
            "deepinfra",
            "https://deepinfra.com/pricing",
            "free credits for open models",
            "known",
            timedelta(days=6),
            "aggregator-x",
            expires_delta=timedelta(days=10),
        ),
        row(
            18,
            "novita",
            "https://novita.ai/pricing",
            "free tier for image + text models",
            "used",
            timedelta(days=3),
            "discord",
            account_key="novita/novita-main",
        ),
        row(
            19,
            "siliconflow",
            "https://siliconflow.cn/pricing",
            "qwen models free for new accounts",
            "new",
            timedelta(minutes=30),
            "manual",
        ),
        row(
            20,
            "moonshot",
            "https://moonshot.cn/pricing",
            "kimi free tier, quota tightened since",
            "expired",
            timedelta(days=90),
            "twitter",
            expires_delta=-timedelta(days=60),
        ),
        row(
            21,
            "baidu",
            "https://qianfan.baidubce.com/pricing",
            "ernie free tier, needs cn phone verify",
            "known",
            timedelta(days=7),
            "vendor docs, promo-hunt 2026-09-08",
        ),
        row(
            22,
            "minimax",
            "https://minimax.chat/pricing",
            "abab free tier bumped twice this week",
            "new",
            timedelta(hours=1),
            "scout",
            updates_count=3,
            updated_delta=timedelta(minutes=20),
        ),
        row(
            23,
            "stepfun",
            "https://platform.stepfun.com/pricing",
            "free trial credits on signup",
            "used",
            timedelta(days=12),
            "manual",
            account_key="stepfun/stepfun-main",
        ),
        row(
            24,
            "sensetime",
            "https://platform.sensenova.cn/pricing",
            "free tier for sensechat models",
            "known",
            timedelta(days=4),
            "reddit",
            expires_delta=timedelta(days=1),
        ),
        row(
            25,
            "01ai",
            "https://platform.lingyiwanwu.com/pricing",
            "yi models free tier, quota reduced",
            "expired",
            timedelta(days=45),
            "pepper.pl",
            expires_delta=-timedelta(days=10),
        ),
        row(
            26,
            "windsurf",
            "https://windsurf.com/pricing",
            "pro trial, 2 weeks of frontier models in the IDE",
            "rejected",
            timedelta(days=8),
            "pepper.pl",
            rejected_reason="IDE-only, no API endpoint the hub can call",
            rejected_delta=timedelta(days=6),
        ),
        row(
            27,
            "lambdalabs",
            "https://lambdalabs.com/inference",
            "$10 inference credits for new accounts",
            "rejected",
            timedelta(days=14),
            "scout",
            rejected_reason="needs a card on file before the credits are issued",
            rejected_delta=timedelta(days=12),
            updates_count=2,
            updated_delta=timedelta(days=2),
        ),
        row(
            28,
            "aggregator-x",
            "https://aggregator-x.dev/free",
            "one key for groq, cerebras and openrouter free models",
            "rejected",
            timedelta(days=20),
            "reddit",
            rejected_reason="aggregator of models the hub already routes directly, adds a hop and a rate limit",
            rejected_delta=timedelta(days=18),
        ),
    ]


def _promo_rejections() -> list[dict[str, Any]]:
    """The history the scout reads: one row per rejection event, newest first."""
    rows = [row for row in _promos() if row.get("status") == "rejected"]
    rows.sort(key=lambda row: str(row.get("rejected_at") or ""), reverse=True)
    return [
        {
            "id": index + 1,
            "identity": row["provider"],
            "provider": row["provider"],
            "reason": row["rejected_reason"],
            "url": row["url"],
            "note_snippet": row["note"][:200] or None,
            "rejected_at": row["rejected_at"],
        }
        for index, row in enumerate(rows)
    ]


APPS = ["my-app", "batch-ocr", "hub-cli", "dashboard"]
LIVE_STATES = ("queued", "waiting_quota", "running")
MOCK_WORKERS = 6

KNOWN_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "explabs",
        "kind": "openai",
        "base_url": "https://api.experientiallabs.ai/v1",
        "api_key_env": "EXPLABS_API_KEY",
        "docs_url": "https://docs.experientiallabs.ai/api",
        "models": [
            {
                "id": "gpt-6-astra",
                "caps": ["text", "vision", "tools", "json", "reasoning"],
                "context": 1050000,
                "reset_tz": "UTC",
                "free": {
                    "hourly": {"out_tokens": 30000},
                    "daily": {"in_tokens": 375000, "out_tokens": 75000},
                },
            },
            {
                "id": "claude-fable-5.1",
                "caps": ["text", "vision", "tools", "json", "reasoning"],
                "free": {
                    "hourly": {"out_tokens": 30000},
                    "daily": {"in_tokens": 375000, "out_tokens": 75000},
                },
            },
        ],
    },
    {
        "id": "zai",
        "kind": "openai",
        "base_url": "https://api.z.ai/api/paas/v4",
        "api_key_env": "ZAI_API_KEY",
        "docs_url": "https://docs.z.ai/guides/overview",
        "models": [
            {
                "id": "glm-4.5-flash",
                "caps": ["text", "tools"],
                "free": {},
                "extra_body": {"thinking": {"type": "disabled"}},
            },
            {"id": "glm-4.6v-flash", "caps": ["text", "vision"], "free": {}},
        ],
    },
    {
        "id": "dashscope",
        "kind": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/",
        "models": [
            {
                "id": "qwen3.7-plus",
                "caps": ["text", "tools", "json"],
                "free": {"allowance": {"total_tokens": 1000000, "expires_at": None}},
                "extra_body": {"enable_thinking": False},
            },
            {
                "id": "qwen3-vl-plus",
                "caps": ["text", "vision"],
                "free": {"allowance": {"total_tokens": 1000000, "expires_at": None}},
            },
        ],
    },
    {
        "id": "openrouter",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "docs_url": "https://openrouter.ai/docs/quickstart",
        "models": [
            {"id": "llama-4-scout:free", "caps": ["text", "vision"], "free": {}},
            {"id": "deepseek-r2:free", "caps": ["text", "reasoning"], "free": {}},
        ],
    },
    {
        "id": "gemini",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "models": [
            {
                "id": "gemini-3.0-flash",
                "caps": ["text", "vision", "tools", "json"],
                "reset_tz": "America/Los_Angeles",
                "free": {"daily": {"requests": 1500}, "hourly": {"requests": 100}},
            },
            {
                "id": "gemini-3.0-flash-lite",
                "caps": ["text", "vision"],
                "free": {"daily": {"requests": 1500}},
            },
        ],
    },
    {
        "id": "groq",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "docs_url": "https://console.groq.com/docs/openai",
        "models": [
            {"id": "llama-4-scout-17b", "caps": ["text", "tools"], "free": {"daily": {"requests": 1000}}},
        ],
    },
    {
        "id": "cerebras",
        "kind": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "docs_url": "https://inference-docs.cerebras.ai/",
        "models": [
            {
                "id": "llama3.3-70b",
                "caps": ["text", "tools"],
                "free": {"daily": {"total_tokens": 1000000}, "hourly": {"requests": 30}},
            },
        ],
    },
    {
        "id": "sambanova",
        "kind": "openai",
        "base_url": "https://api.sambanova.ai/v1",
        "api_key_env": "SAMBANOVA_API_KEY",
        "docs_url": "https://docs.sambanova.ai/cloud/docs/get-started/overview",
        "models": [
            {"id": "Meta-Llama-3.3-70B-Instruct", "caps": ["text", "tools"], "free": {}},
        ],
    },
    {
        "id": "opencode-zen",
        "kind": "openai",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_ZEN_API_KEY",
        "docs_url": "https://opencode.ai/docs/zen/",
        "models": [
            {
                "id": "grok-code",
                "caps": ["text", "tools"],
                "free": {"allowance": {"total_tokens": 2000000, "expires_at": None}},
            },
        ],
    },
    {
        "id": "cloudflare-workers-ai",
        "kind": "openai",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "docs_url": "https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/",
        "fields": [
            {
                "name": "account_id",
                "label": "Cloudflare account id",
                "required": True,
                "placeholder": "32 hex chars",
            },
        ],
        "models": [
            {
                "id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "caps": ["text"],
                "free": {"daily": {"requests": 10000}},
            },
        ],
    },
    {
        "id": "ollama",
        "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "",
        "docs_url": "https://github.com/ollama/ollama/blob/main/docs/openai.md",
        "models": [
            {
                "id": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "caps": ["text", "json"],
                "free": {},
                "concurrency": 1,
            },
            {"id": "gemma3:27b", "caps": ["text", "vision"], "free": {}, "concurrency": 1},
        ],
    },
]

UNVERIFIED_FREE = "unverified free status; registered because the owner added the key on purpose"

# templates the v0.2 form never offered because they carry no verified free model list:
# they ship models: [] and rely on discover, which is exactly the quick add path
QUICK_ONLY_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "nvidia-nim",
        "kind": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "docs_url": "https://build.nvidia.com/",
        "models": [
            {"id": "moonshotai/kimi-k3", "caps": ["text", "tools"], "free": {}, "notes": UNVERIFIED_FREE},
            {
                "id": "deepseek-ai/deepseek-v4-pro",
                "caps": ["text", "reasoning"],
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
        "models": [],
    },
    {
        "id": "cohere",
        "kind": "openai",
        "base_url": "https://api.cohere.com/compatibility/v1",
        "api_key_env": "COHERE_API_KEY",
        "docs_url": "https://docs.cohere.com/",
        "models": [],
    },
    {
        "id": "huggingface",
        "kind": "openai",
        "base_url": "https://router.huggingface.co/v1",
        "api_key_env": "HF_TOKEN",
        "docs_url": "https://huggingface.co/docs/inference-providers",
        "models": [],
    },
    {
        "id": "moonshot",
        "kind": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "docs_url": "https://platform.moonshot.ai/docs",
        "models": [],
    },
    {
        "id": "deepseek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "docs_url": "https://api-docs.deepseek.com/",
        "models": [],
    },
    {
        "id": "xai",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "docs_url": "https://docs.x.ai/",
        "models": [],
    },
    {
        "id": "fireworks",
        "kind": "openai",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "api_key_env": "FIREWORKS_API_KEY",
        "docs_url": "https://docs.fireworks.ai/",
        "models": [],
    },
    {
        "id": "together",
        "kind": "openai",
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "docs_url": "https://docs.together.ai/",
        "models": [],
    },
    {
        "id": "deepinfra",
        "kind": "openai",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_key_env": "DEEPINFRA_API_KEY",
        "docs_url": "https://deepinfra.com/docs",
        "models": [],
    },
    {
        "id": "minimax",
        "kind": "openai",
        "base_url": "https://api.minimax.io/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "docs_url": "https://www.minimax.io/platform/document",
        "models": [],
    },
]

KNOWN_PROVIDERS.extend(QUICK_ONLY_TEMPLATES)

# aliases = what a human writes or pastes, hostnames = domains that turn up in a promo url.
# Both feed the quick add resolution: source text -> catalog.
CATALOG_HINTS: dict[str, dict[str, list[str]]] = {
    "explabs": {
        "aliases": ["explabs", "experiential labs", "experientiallabs"],
        "hostnames": ["api.experientiallabs.ai", "experientiallabs.ai"],
    },
    "zai": {
        "aliases": ["zai", "z.ai", "z ai", "zhipu", "bigmodel", "glm"],
        "hostnames": ["api.z.ai", "z.ai", "open.bigmodel.cn"],
    },
    "dashscope": {
        "aliases": ["dashscope", "model studio", "alibaba", "aliyun", "bailian", "qwen"],
        "hostnames": ["dashscope-intl.aliyuncs.com", "dashscope.aliyuncs.com", "aliyuncs.com"],
    },
    "openrouter": {"aliases": ["openrouter", "open router"], "hostnames": ["openrouter.ai"]},
    "gemini": {
        "aliases": ["gemini", "google ai studio", "ai studio", "google gemini"],
        "hostnames": ["generativelanguage.googleapis.com", "aistudio.google.com"],
    },
    "groq": {
        "aliases": ["groq", "groqcloud", "groq cloud"],
        "hostnames": ["api.groq.com", "console.groq.com", "groq.com"],
    },
    "cerebras": {
        "aliases": ["cerebras", "cerebras cloud"],
        "hostnames": ["api.cerebras.ai", "cloud.cerebras.ai", "cerebras.ai"],
    },
    "sambanova": {
        "aliases": ["sambanova", "samba nova"],
        "hostnames": ["api.sambanova.ai", "cloud.sambanova.ai"],
    },
    "opencode-zen": {"aliases": ["opencode", "opencode zen", "zen"], "hostnames": ["opencode.ai"]},
    "cloudflare-workers-ai": {
        "aliases": ["cloudflare", "workers ai", "cloudflare ai"],
        "hostnames": ["api.cloudflare.com", "dash.cloudflare.com"],
    },
    "ollama": {"aliases": ["ollama"], "hostnames": ["127.0.0.1:11434", "localhost:11434"]},
    "nvidia-nim": {
        "aliases": ["nvidia", "nim", "nvidia nim"],
        "hostnames": ["integrate.api.nvidia.com", "build.nvidia.com"],
    },
    "mistral": {
        "aliases": ["mistral", "le chat", "codestral"],
        "hostnames": ["api.mistral.ai", "console.mistral.ai", "mistral.ai"],
    },
    "cohere": {
        "aliases": ["cohere", "command r"],
        "hostnames": ["api.cohere.com", "dashboard.cohere.com", "cohere.com"],
    },
    "huggingface": {
        "aliases": ["huggingface", "hugging face", "hf", "hf router"],
        "hostnames": ["router.huggingface.co", "huggingface.co"],
    },
    "moonshot": {
        "aliases": ["moonshot", "kimi"],
        "hostnames": ["api.moonshot.ai", "platform.moonshot.ai", "moonshot.cn"],
    },
    "deepseek": {"aliases": ["deepseek"], "hostnames": ["api.deepseek.com", "platform.deepseek.com"]},
    "xai": {"aliases": ["xai", "x.ai", "grok"], "hostnames": ["api.x.ai", "console.x.ai"]},
    "fireworks": {
        "aliases": ["fireworks", "fireworks ai"],
        "hostnames": ["api.fireworks.ai", "fireworks.ai"],
    },
    "together": {"aliases": ["together", "together ai"], "hostnames": ["api.together.xyz", "together.ai"]},
    "deepinfra": {"aliases": ["deepinfra", "deep infra"], "hostnames": ["deepinfra.com"]},
    "minimax": {
        "aliases": ["minimax", "mini max"],
        "hostnames": ["api.minimax.io", "minimax.io", "minimaxi.com"],
    },
}

for _tpl in KNOWN_PROVIDERS:
    _hint = CATALOG_HINTS.get(_tpl["id"], {})
    _tpl["aliases"] = _hint.get("aliases", [_tpl["id"]])
    _tpl["hostnames"] = _hint.get("hostnames", [])

KEY_PREFIXES: list[tuple[str, str]] = [
    ("sk-or-v1-", "openrouter"),
    ("AIza", "gemini"),
    ("gsk_", "groq"),
    ("csk-", "cerebras"),
    ("xpl_", "explabs"),
    ("nvapi-", "nvidia-nim"),
    ("hf_", "huggingface"),
    ("sk-ant-", "anthropic"),
]

DISCOVERED_BY_PROVIDER: dict[str, list[str]] = {
    "mistral": [
        "mistral-small-latest",
        "mistral-medium-latest",
        "pixtral-12b-2409",
        "open-mistral-nemo",
        "codestral-latest",
    ],
    "ollama": ["qwen3:30b-a3b-instruct-2507-q4_K_M", "gemma3:27b", "llama3.2:3b"],
}

# The live view: three apps busy, five calls in flight. zai/glm-4.5-flash has two callers at
# once (my-app and hub-cli), so the Models table's Apps cell has something to show two chips
# for; the dashscope job is already on its second candidate after a fallback, for the attempt
# marker; batch-ocr holds one running call with a second queued behind it, which is what the
# muted "waiting" chip and the kill switch need to show. Elapsed seconds are counted from
# process start so the chips tick. Every (account, model) pair here must exist in _models() or
# the Models table has nothing to join it against.
LIVE_BOOT = now()
LIVE_WINDOW_MIN = 15
LIVE_CALLS: list[dict[str, Any]] = [
    {
        "app": "my-app",
        "model": "zai/glm-4.5-flash",
        "account": "zai-main",
        "kind": "stream",
        "base_s": 6.0,
        "attempt": 1,
        "job_id": None,
        "call_id": 101,
        "state": "running",
    },
    {
        "app": "hub-cli",
        "model": "zai/glm-4.5-flash",
        "account": "zai-main",
        "kind": "sync",
        "base_s": 1.5,
        "attempt": 1,
        "job_id": None,
        "call_id": 102,
        "state": "running",
    },
    {
        "app": "my-app",
        "model": "dashscope/qwen3.7-plus",
        "account": "dashscope-main",
        "kind": "job",
        "base_s": 41.0,
        "attempt": 2,
        "job_id": "job_9f21c0a4b7d1",
        "call_id": 103,
        "state": "running",
    },
    {
        "app": "batch-ocr",
        "model": "explabs/claude-fable-5.1",
        "account": "explabs-main",
        "kind": "sync",
        "base_s": 2.0,
        "attempt": 1,
        "job_id": None,
        "call_id": 104,
        "state": "running",
    },
    {
        "app": "batch-ocr",
        "model": "explabs/claude-fable-5.1",
        "account": "explabs-main",
        "kind": "sync",
        "base_s": 12.0,
        "attempt": 1,
        "job_id": None,
        "call_id": 105,
        "state": "waiting",
    },
]
LIVE_RECENT: dict[str, list[dict[str, Any]]] = {
    "my-app": [
        {"model": "zai/glm-4.5-flash", "calls": 31, "out_tokens": 18420},
        {"model": "explabs/claude-fable-5.1", "calls": 12, "out_tokens": 9040},
        {"model": "explabs/gpt-6-astra", "calls": 4, "out_tokens": 2210},
    ],
    "batch-ocr": [
        {"model": "zai/glm-4.6v-flash", "calls": 7, "out_tokens": 1580},
        {"model": "dashscope/qwen3-vl-plus", "calls": 3, "out_tokens": 640},
    ],
}


def _live_apps() -> list[dict[str, Any]]:
    age = (now() - LIVE_BOOT).total_seconds()
    apps = sorted({*LIVE_RECENT, *(call["app"] for call in LIVE_CALLS)})
    rows: dict[str, dict[str, Any]] = {
        app: {"app": app, "in_flight": [], "recent": list(LIVE_RECENT.get(app, []))} for app in apps
    }
    for call in LIVE_CALLS:
        rows[call["app"]]["in_flight"].append(
            {
                "call_id": call["call_id"],
                "state": call["state"],
                "model": call["model"],
                "account": call["account"],
                "kind": call["kind"],
                "elapsed_s": round(call["base_s"] + age, 1),
                "attempt": call["attempt"],
                "job_id": call["job_id"],
            }
        )
    return sorted(
        rows.values(),
        key=lambda row: (
            -len(row["in_flight"]),
            -sum(item["calls"] for item in row["recent"]),
            row["app"],
        ),
    )


def _drop_live_calls(match: Callable[[dict[str, Any]], bool]) -> list[int]:
    """The mock's kill switch: a cancelled call simply leaves the in-flight list."""
    gone = [call["call_id"] for call in LIVE_CALLS if match(call)]
    LIVE_CALLS[:] = [call for call in LIVE_CALLS if call["call_id"] not in gone]
    return gone


def _live_model_columns() -> tuple[dict[str, list[str]], dict[str, int]]:
    """What the Models table shows per row: the apps of the window, and calls in flight."""
    apps: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for app, rows in LIVE_RECENT.items():
        for row in rows:
            apps.setdefault(row["model"], []).append(app)
    for call in LIVE_CALLS:
        names = apps.setdefault(call["model"], [])
        if call["app"] not in names:
            names.append(call["app"])
        counts[call["model"]] = counts.get(call["model"], 0) + 1
    return {key: sorted(names) for key, names in apps.items()}, counts


def _bans() -> dict[str, list[dict[str, Any]]]:
    """One app with two bans: what the Apps table's Bans column expands into."""
    return {
        "batch-ocr": [
            {
                "app": "batch-ocr",
                "model": "explabs/claude-fable-5.1",
                "reason": "paraphrases the source instead of quoting it verbatim",
                "created_at": (now() - timedelta(days=2)).isoformat(),
            },
            {
                "app": "batch-ocr",
                "model": "zai/glm-4.5-flash",
                "reason": "returns prose around the json, schema ignored on long inputs",
                "created_at": (now() - timedelta(hours=6)).isoformat(),
            },
        ]
    }


STATE: dict[str, Any] = {
    "models": _models(),
    "paused": {"batch-ocr": True},
    "bans": _bans(),
    "promos": _promos(),
    "next_promo_id": 29,
    "promo_rejections": _promo_rejections(),
    "jobs": [],
    "usage": [],
    "registry": {},
}


def _seed_usage() -> None:
    rnd = random.Random(7)
    keys = [m["key"] for m in STATE["models"]]
    start = now().replace(minute=0, second=0, microsecond=0) - timedelta(days=30)
    rows = []
    for h in range(30 * 24):
        ts = start + timedelta(hours=h)
        day_factor = 1.0 + 0.6 * ((h // 24) % 7 == 0)
        hour_factor = 0.15 if ts.hour < 7 else 1.0
        for app in APPS:
            base = {"my-app": 9, "batch-ocr": 5, "hub-cli": 2, "dashboard": 1}[app]
            reqs = int(rnd.gauss(base * day_factor * hour_factor, 1.5))
            if reqs <= 0:
                continue
            key = keys[rnd.randrange(len(keys))]
            in_tok = reqs * rnd.randint(400, 2600)
            out_tok = reqs * rnd.randint(120, 900)
            errs = 1 if rnd.random() < 0.06 else 0
            rows.append(
                {
                    "ts": ts,
                    "app": app,
                    "model": key,
                    "account": key.split("/")[0] + "-main",
                    "requests": reqs,
                    "in_tokens": in_tok,
                    "out_tokens": out_tok,
                    "errors": errs,
                }
            )
    STATE["usage"] = rows


def _seed_jobs() -> None:
    t = now()
    reset_40 = iso(t + timedelta(minutes=40))
    STATE["jobs"] = [
        {
            "id": "j-1041",
            "app": "my-app",
            "model": "explabs/gpt-6-astra",
            "state": "waiting_quota",
            "priority": 3,
            "created_at": iso(t - timedelta(minutes=48)),
            "next_window_at": reset_40,
        },
        {
            "id": "j-1042",
            "app": "my-app",
            "model": "explabs/gpt-6-astra",
            "state": "waiting_quota",
            "priority": 3,
            "created_at": iso(t - timedelta(minutes=45)),
            "next_window_at": reset_40,
        },
        {
            "id": "j-1043",
            "app": "batch-ocr",
            "model": "vision",
            "state": "waiting_quota",
            "priority": 5,
            "created_at": iso(t - timedelta(minutes=30)),
            "next_window_at": reset_40,
        },
        {
            "id": "j-1044",
            "app": "batch-ocr",
            "model": "auto",
            "state": "queued",
            "priority": 5,
            "created_at": iso(t - timedelta(minutes=12)),
            "next_window_at": None,
        },
        {
            "id": "j-1045",
            "app": "hub-cli",
            "model": "dashscope/qwen3.7-plus",
            "state": "queued",
            "priority": 7,
            "created_at": iso(t - timedelta(minutes=8)),
            "next_window_at": None,
        },
        {
            "id": "j-1046",
            "app": "my-app",
            "model": "explabs/claude-fable-5.1",
            "state": "running",
            "priority": 1,
            "created_at": iso(t - timedelta(minutes=1)),
            "next_window_at": None,
        },
        {
            "id": "j-1039",
            "app": "my-app",
            "model": "zai/glm-4.5-flash",
            "state": "done",
            "priority": 5,
            "created_at": iso(t - timedelta(hours=2)),
            "next_window_at": None,
        },
        {
            "id": "j-1038",
            "app": "batch-ocr",
            "model": "zai/glm-4.6v-flash",
            "state": "failed",
            "priority": 5,
            "created_at": iso(t - timedelta(hours=3)),
            "next_window_at": None,
        },
    ]


def _events() -> list[dict[str, Any]]:
    rnd = random.Random(11)
    kinds = ["error", "quota", "fallback", "no_candidates", "retry", "cooldown", "waiting_quota"]
    messages = {
        "quota": [
            "explabs/gpt-6-astra hourly out_tokens exhausted, next window in 40m",
            "dashscope/qwen3-vl-plus free quota has been exhausted",
        ],
        "fallback": [
            "auto: explabs/gpt-6-astra exhausted -> explabs/claude-fable-5.1",
            "vision: zai/glm-4.6v-flash down -> dashscope/qwen3-vl-plus",
            "auto: attempt 2 of 4, zai/glm-4.5-flash cooldown",
        ],
        "error": [
            "429 Too Many Requests (code 1302) from api.z.ai",
            "httpx.ConnectError: operation timed out connecting to api.z.ai:443",
            "upstream 502 bad gateway, retry in 5s",
            "refusal: model declined the request (safety)",
        ],
        "no_candidates": [
            "no candidates left for caps [vision, tools] after excluding disabled models",
            "no_candidates: every model matching the request is exhausted or down",
        ],
        "retry": [
            "retry 1 of 3 for zai/glm-4.5-flash after connect timeout",
            "retry 2 of 3 for dashscope/qwen3.7-plus after 500 response",
        ],
        "cooldown": [
            "zai/glm-4.6v-flash entering cooldown for 90s after repeated 5xx",
            "openrouter/llama-4-scout:free cooldown extended to 5m",
        ],
        "waiting_quota": [
            "job j-1045 waiting_quota, next window in 22m",
            "job j-1039 waiting_quota on dashscope/qwen3.7-plus",
        ],
    }
    out = []
    t = now()
    for i in range(320):
        kind = kinds[rnd.randrange(len(kinds))]
        out.append(
            {
                "ts": iso(t - timedelta(minutes=42 * i + rnd.randrange(20))),
                "app": APPS[rnd.randrange(len(APPS))],
                "model": STATE["models"][rnd.randrange(len(STATE["models"]))]["key"],
                "kind": kind,
                "message": messages[kind][rnd.randrange(len(messages[kind]))],
            }
        )
    return out


def _parse_since(value: str | None, default_days: int = 1) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return now() - timedelta(days=default_days)


def _require_token(request: Request) -> None:
    if not REQUIRE_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if auth != "Bearer " + REQUIRE_TOKEN:
        raise HTTPException(status_code=401, detail="token required")


def _find_model(key: str) -> dict[str, Any]:
    for m in STATE["models"]:
        if m["key"] == key:
            return m
    raise HTTPException(status_code=404, detail="unknown model " + key)


def _known(provider: str) -> dict[str, Any] | None:
    for t in KNOWN_PROVIDERS:
        if t["id"] == provider:
            return t
    return None


def _find_promo(promo_id: Any) -> dict[str, Any]:
    for row in STATE["promos"]:
        if str(row.get("id")) == str(promo_id):
            return row
    raise HTTPException(status_code=404, detail=f"unknown promo {promo_id}")


def _next_run_at(schedule: str | None) -> str | None:
    if not schedule:
        return None
    try:
        hh_s, mm_s = schedule.split(":")
        hh, mm = int(hh_s), int(mm_s)
    except ValueError:
        return None
    n = now()
    candidate = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= n:
        candidate += timedelta(days=1)
    return iso(candidate)


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in run.items() if k not in ("report_md", "decisions")}


def _apply_scout_decisions(decisions: list[dict[str, Any]]) -> None:
    for d in decisions:
        if d.get("action") == "new" and d.get("row"):
            row = dict(d["row"])
            row["id"] = STATE["next_promo_id"]
            row.setdefault("found_at", iso(now()))
            row.setdefault("status", "new")
            row.setdefault("account_key", None)
            row["source"] = "scout"
            STATE["next_promo_id"] += 1
            STATE["promos"].insert(0, row)
        elif d.get("action") == "update" and d.get("promo_id") is not None:
            try:
                target = _find_promo(d["promo_id"])
                target["note"] = (target.get("note") or "") + " (scout: confirmed)"
            except HTTPException:
                pass


def _finish_scout_run(run_id: str) -> None:
    run = STATE["scout_runs"].get(run_id)
    if run is None or run.get("status") != "running":
        return
    decisions = [
        {
            "action": "new",
            "row": {
                "provider": "novaai",
                "url": "https://novaai.dev/pricing",
                "base_url": "https://api.novaai.dev/v1",
                "api_key_env": "NOVAAI_API_KEY",
                "expires_at": None,
                "note": "free tier: 1M tokens/mo on the flash model",
            },
            "reason": "vendor docs list a new $0 tier, not yet in the catalog; evidence quote captured",
        },
        {
            "action": "update",
            "promo_id": 5,
            "reason": "zai pricing page confirms glm-4.6-flash moved to free, limit still unconfirmed",
        },
        {
            "action": "skip",
            "row": {"provider": "groq"},
            "reason": "duplicate of promo #2, same url and offer",
        },
        {
            "action": "skip",
            "row": {"provider": "unknown"},
            "reason": "page had no offers and no evidence quote",
        },
    ]
    _apply_scout_decisions(decisions)
    finished = now()
    report_md = (
        "# Scout run " + run_id + "\n\n"
        "- sources: " + str(SCOUT_SOURCES_COUNT) + ", pages fetched: 18 (3 changed)\n"
        "- offers extracted: 4 -> 1 new, 1 updated, 2 skipped\n"
        "- new: novaai (https://novaai.dev/pricing) - 1M tokens/mo free tier\n"
        "- updated: zai (#5) - glm-4.6-flash confirmed free\n"
        "- skipped: groq (duplicate), 1 page with no offers\n"
        "- models: extract=fast, curate=astra\n"
        "- tokens: extract 5400 in / 1200 out, curate 2100 in / 650 out\n"
        "- errors: 0\n"
    )
    run.update(
        {
            "status": "done",
            "finished_at": iso(finished),
            "sources": SCOUT_SOURCES_COUNT,
            "pages_fetched": 18,
            "pages_changed": 3,
            "offers": 4,
            "new": 1,
            "updated": 1,
            "skipped": 2,
            "models_used": {"extract": ["fast"], "curate": ["astra"]},
            "tokens": {"extract": {"in": 5400, "out": 1200}, "curate": {"in": 2100, "out": 650}},
            "errors": 0,
            "decisions": decisions,
            "report_md": report_md,
        }
    )
    STATE["scout"]["active_run_id"] = None
    STATE["scout"]["last_run_id"] = run_id


def _seed_scout() -> None:
    base = now().replace(hour=8, minute=0, second=0, microsecond=0)
    specs = [
        (
            3,
            "done",
            18,
            3,
            4,
            1,
            1,
            2,
            0,
            ["fast"],
            ["astra"],
            (5400, 1200),
            (2100, 650),
            "daily run - novaai promo added, zai confirmed",
        ),
        (
            2,
            "done",
            21,
            5,
            6,
            2,
            1,
            3,
            0,
            ["fast"],
            ["gemini-3.8-flash"],
            (6100, 1400),
            (2600, 700),
            "daily run - 2 new promos, dashscope note refreshed",
        ),
        (
            1,
            "failed",
            9,
            1,
            0,
            0,
            0,
            0,
            2,
            ["fast"],
            [],
            (1800, 0),
            (0, 0),
            "daily run aborted - hub answered 429 twice on fast, stopped early",
        ),
    ]
    runs: dict[str, Any] = {}
    for (
        days_ago,
        status,
        pages,
        changed,
        offers,
        new,
        upd,
        skip,
        errors,
        ex_m,
        cu_m,
        ex_t,
        cu_t,
        note,
    ) in specs:
        started = base - timedelta(days=days_ago)
        finished = started + timedelta(minutes=4, seconds=12)
        run_id = "run-" + started.strftime("%Y%m%d-%H%M")
        runs[run_id] = {
            "id": run_id,
            "started_at": iso(started),
            "finished_at": iso(finished),
            "status": status,
            "sources": SCOUT_SOURCES_COUNT,
            "pages_fetched": pages,
            "pages_changed": changed,
            "offers": offers,
            "new": new,
            "updated": upd,
            "skipped": skip,
            "models_used": {"extract": ex_m, "curate": cu_m},
            "tokens": {"extract": {"in": ex_t[0], "out": ex_t[1]}, "curate": {"in": cu_t[0], "out": cu_t[1]}},
            "errors": errors,
            "report_md": "# " + run_id + "\n\n" + note + "\n",
            "decisions": [],
        }
    STATE["scout_runs"] = runs
    ordered = sorted(runs.values(), key=lambda r: r["started_at"])
    STATE["scout"] = {
        "schedule": "08:00",
        "active_run_id": None,
        "last_run_id": ordered[-1]["id"] if ordered else None,
    }


def _catalog_match(source: str) -> str | None:
    """template id from free text: hostnames first (more specific), then id and aliases."""
    text = " " + (source or "").lower().strip() + " "
    for tpl in KNOWN_PROVIDERS:
        for host in tpl.get("hostnames") or []:
            if host.lower() in text:
                return tpl["id"]
    for tpl in KNOWN_PROVIDERS:
        for name in [tpl["id"]] + list(tpl.get("aliases") or []):
            if re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", text):
                return tpl["id"]
    return None


def _prefix_match(api_key: str) -> str | None:
    for prefix, provider in KEY_PREFIXES:
        if api_key.startswith(prefix):
            return provider
    return None


def _guess_base_url(url: str) -> str | None:
    m = re.match(r"https?://([^/]+)", url or "")
    return f"https://{m.group(1)}/v1" if m else None


def _seed_registry() -> None:
    registry: dict[str, Any] = {}
    for provider in ("explabs", "zai", "dashscope"):
        tpl = _known(provider) or {}
        registry[provider] = {
            "kind": tpl.get("kind", "openai"),
            "base_url": tpl.get("base_url"),
            "docs_url": tpl.get("docs_url"),
            "accounts": [
                {
                    "id": provider + "-main",
                    "api_key_env": tpl.get("api_key_env"),
                    "key_present": True,
                    "env_file": f"~/.llmhub/env/{provider}.env",
                }
            ],
            "models": [dict(m) for m in tpl.get("models", [])],
        }
    # a provider whose first model is paid, so the test refusal (409) path is reachable
    registry["openrouter"] = {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "docs_url": "https://openrouter.ai/docs/quickstart",
        "accounts": [
            {
                "id": "openrouter-main",
                "api_key_env": "OPENROUTER_API_KEY",
                "key_present": True,
                "env_file": "~/.llmhub/env/openrouter.env",
            }
        ],
        "models": [
            {
                "id": "gpt-6-astra-preview",
                "caps": ["text", "tools"],
                "free": None,
                "notes": "paid row, kept for manual comparison",
            },
            {"id": "llama-4-scout:free", "caps": ["text", "vision"], "free": {}},
        ],
    }
    STATE["registry"] = registry


def _registry_provider(provider: str) -> dict[str, Any]:
    prov = STATE["registry"].get(provider)
    if not prov:
        raise HTTPException(status_code=404, detail="unknown provider " + provider)
    return prov


def _registry_account(provider: str, account_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    prov = _registry_provider(provider)
    for acc in prov["accounts"]:
        if acc["id"] == account_id:
            return prov, acc
    raise HTTPException(status_code=404, detail=f"unknown account {provider}/{account_id}")


def _lan_warning(request: Request) -> str | None:
    client = request.client.host if request.client else ""
    if request.url.scheme == "http" and client not in ("127.0.0.1", "::1", "localhost", ""):
        return f"key sent over plain HTTP from {client}; prefer adding keys on the Mac itself"
    return None


def _account_rows() -> list[dict[str, Any]]:
    rows = []
    for provider, prov in STATE["registry"].items():
        for acc in prov["accounts"]:
            rows.append(
                {
                    "provider": provider,
                    "kind": prov.get("kind"),
                    "base_url": prov.get("base_url"),
                    "account": acc["id"],
                    "api_key_env": acc.get("api_key_env"),
                    "key_present": acc.get("key_present", False),
                    "env_file": acc.get("env_file"),
                    "models": [m["id"] for m in prov.get("models", [])],
                }
            )
    return rows


def _app_shares() -> dict[str, dict[str, Any]]:
    """Same shape as the real queue reports: what waits, what runs, what an app may hold."""
    counts: dict[str, dict[str, int]] = {}
    for j in STATE["jobs"]:
        if j["state"] not in LIVE_STATES:
            continue
        slot = counts.setdefault(j["app"], {"queued": 0, "running": 0})
        slot["running" if j["state"] == "running" else "queued"] += 1
    paused = {name for name in counts if STATE["paused"].get(name)}
    cap = max(1, MOCK_WORKERS // max(1, len(set(counts) - paused)))
    return {
        name: {
            "queued": slot["queued"],
            "running": slot["running"],
            "cap": 0 if name in paused else cap,
            "paused": name in paused,
        }
        for name, slot in sorted(counts.items())
    }


def create_hub_app() -> FastAPI:
    app = FastAPI(title="llmhub dev mock", docs_url=None, redoc_url=None)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        depth: dict[str, int] = {}
        by_app: dict[str, int] = {}
        queued_at: list[str] = []
        for j in STATE["jobs"]:
            depth[j["state"]] = depth.get(j["state"], 0) + 1
            if j["state"] in LIVE_STATES:
                by_app[j["app"]] = by_app.get(j["app"], 0) + 1
                if j.get("created_at"):
                    queued_at.append(str(j["created_at"]))
        apps_15m, in_flight = _live_model_columns()
        return {
            "generated_at": iso(now()),
            "models": [
                dict(m, apps_15m=apps_15m.get(m["key"], []), in_flight=in_flight.get(m["key"], 0))
                for m in STATE["models"]
            ],
            "queue": {
                "depth_by_state": depth,
                "by_app": by_app,
                "live": sum(depth.get(state, 0) for state in LIVE_STATES),
                "oldest_queued_at": min(queued_at) if queued_at else None,
                "expired_last_24h": depth.get("expired", 0),
                "workers": MOCK_WORKERS,
                "apps": _app_shares(),
            },
            "accounts": _account_rows(),
        }

    @app.get("/api/live")
    def live(window_min: int = LIVE_WINDOW_MIN) -> dict[str, Any]:
        rows = _live_apps()
        return {
            "generated_at": iso(now()),
            "window_min": window_min,
            "apps": rows,
            "in_flight_total": sum(len(row["in_flight"]) for row in rows),
        }

    @app.post("/api/live/{call_id}/cancel")
    def cancel_live_call(request: Request, call_id: int) -> dict[str, Any]:
        _require_token(request)
        if not _drop_live_calls(lambda call: call["call_id"] == call_id):
            raise HTTPException(status_code=404, detail=f"call {call_id} is not cancellable any more")
        return {"cancelled": [call_id], "count": 1, "ts": iso(now())}

    @app.post("/api/live/cancel")
    def cancel_live_calls(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        _require_token(request)
        app_name = payload.get("app")
        if not app_name and not payload.get("all"):
            raise HTTPException(status_code=422, detail="pass an app name or all: true")
        cancelled = _drop_live_calls(lambda call: not app_name or call["app"] == app_name)
        return {"cancelled": cancelled, "count": len(cancelled), "ts": iso(now())}

    @app.get("/api/usage")
    def usage(since: str | None = None, group_by: str = "app", app_name: str | None = None) -> dict[str, Any]:
        start = _parse_since(since)
        agg: dict[str, dict[str, Any]] = {}
        for r in STATE["usage"]:
            if r["ts"] < start:
                continue
            if group_by == "day":
                key = r["ts"].date().isoformat()
            elif group_by == "account":
                key = r["account"]
            elif group_by == "model":
                key = r["model"]
            else:
                key = r["app"]
            slot = agg.setdefault(
                key, {"bucket": key, "requests": 0, "in_tokens": 0, "out_tokens": 0, "errors": 0}
            )
            slot["requests"] += r["requests"]
            slot["in_tokens"] += r["in_tokens"]
            slot["out_tokens"] += r["out_tokens"]
            slot["errors"] += r["errors"]
        rows = sorted(agg.values(), key=lambda x: -(x["in_tokens"] + x["out_tokens"]))
        for row in rows:
            row["total_tokens"] = row["in_tokens"] + row["out_tokens"]
        return {"group_by": group_by, "since": iso(start), "rows": rows}

    @app.get("/api/usage/timeseries")
    def timeseries(bucket: str = "hour", since: str | None = None) -> dict[str, Any]:
        start = _parse_since(since)
        agg: dict[str, dict[str, Any]] = {}
        for r in STATE["usage"]:
            if r["ts"] < start:
                continue
            slot_ts = r["ts"].replace(minute=0, second=0, microsecond=0)
            if bucket == "day":
                slot_ts = slot_ts.replace(hour=0)
            key = iso(slot_ts)
            slot = agg.setdefault(
                key, {"bucket": key, "requests": 0, "in_tokens": 0, "out_tokens": 0, "errors": 0}
            )
            slot["requests"] += r["requests"]
            slot["in_tokens"] += r["in_tokens"]
            slot["out_tokens"] += r["out_tokens"]
            slot["errors"] += r["errors"]
        return {
            "bucket": bucket,
            "since": iso(start),
            "rows": sorted(agg.values(), key=lambda x: x["bucket"]),
        }

    @app.get("/api/events")
    def events(limit: int = 100) -> dict[str, Any]:
        return {"events": _events()[:limit]}

    @app.get("/api/jobs")
    def jobs(state: str | None = None) -> dict[str, Any]:
        rows = STATE["jobs"]
        # same contract as the hub: no state = live rows, "all" = history, else one state
        if state is None:
            rows = [j for j in rows if j["state"] in ("queued", "waiting_quota", "running")]
        elif state != "all":
            rows = [j for j in rows if j["state"] == state]
        return {"jobs": rows}

    @app.delete("/jobs/{job_id}")
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        for j in STATE["jobs"]:
            if j["id"] == job_id:
                j["state"] = "cancelled"
                j["next_window_at"] = None
                return {"ok": True, "id": job_id, "state": "cancelled"}
        raise HTTPException(status_code=404, detail="unknown job")

    @app.get("/api/apps")
    def apps_list() -> dict[str, Any]:
        totals: dict[str, dict[str, Any]] = {}
        start = now() - timedelta(days=7)
        for r in STATE["usage"]:
            if r["ts"] < start:
                continue
            slot = totals.setdefault(
                r["app"], {"app": r["app"], "requests": 0, "in_tokens": 0, "out_tokens": 0, "errors": 0}
            )
            slot["requests"] += r["requests"]
            slot["in_tokens"] += r["in_tokens"]
            slot["out_tokens"] += r["out_tokens"]
            slot["errors"] += r["errors"]
        shares = _app_shares()
        rows = []
        for name in APPS:
            row = totals.get(name, {"app": name, "requests": 0, "in_tokens": 0, "out_tokens": 0, "errors": 0})
            share = shares.get(name, {})
            row["total_tokens"] = row["in_tokens"] + row["out_tokens"]
            row["queued"] = share.get("queued", 0)
            row["running"] = share.get("running", 0)
            row["cap"] = share.get("cap")
            row["paused"] = bool(STATE["paused"].get(name))
            row["bans"] = len(STATE["bans"].get(name, []))
            rows.append(row)
        return {"apps": rows, "workers": MOCK_WORKERS}

    @app.get("/api/apps/{app_name}/bans")
    def app_bans(app_name: str) -> dict[str, Any]:
        return {"app": app_name, "bans": list(STATE["bans"].get(app_name, []))}

    @app.delete("/api/apps/{app_name}/bans/{model:path}")
    def unban_app_model(app_name: str, model: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        kept = [row for row in STATE["bans"].get(app_name, []) if row["model"] != model]
        if len(kept) == len(STATE["bans"].get(app_name, [])):
            raise HTTPException(status_code=404, detail="unknown ban")
        STATE["bans"][app_name] = kept
        return {"app": app_name, "model": model, "banned": False}

    @app.post("/api/apps/{app_name}/pause")
    def pause_app(app_name: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        STATE["paused"][app_name] = True
        return {"ok": True, "app": app_name, "paused": True}

    @app.post("/api/apps/{app_name}/resume")
    def resume_app(app_name: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        STATE["paused"][app_name] = False
        return {"ok": True, "app": app_name, "paused": False}

    @app.get("/api/accounts")
    def accounts() -> dict[str, Any]:
        out = []
        for provider, prov in STATE["registry"].items():
            accs = []
            for acc in prov["accounts"]:
                accs.append(
                    {
                        "id": acc["id"],
                        "api_key_env": acc.get("api_key_env"),
                        "key_present": acc.get("key_present", False),
                        "env_file": acc.get("env_file"),
                        "models": [dict(m) for m in prov.get("models", [])],
                    }
                )
            out.append(
                {
                    "provider": provider,
                    "kind": prov.get("kind"),
                    "base_url": prov.get("base_url"),
                    "docs_url": prov.get("docs_url"),
                    "accounts": accs,
                }
            )
        return {"accounts": out}

    @app.get("/api/promos")
    def promos() -> dict[str, Any]:
        return {"promos": STATE["promos"]}

    @app.post("/api/promos")
    def add_promo(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _require_token(request)
        row = {
            "id": STATE["next_promo_id"],
            "provider": str(payload.get("provider") or "unknown"),
            "url": str(payload.get("url") or ""),
            "note": str(payload.get("note") or ""),
            "found_at": iso(now()),
            "status": "new",
            "account_key": None,
            "source": str(payload.get("source") or "manual"),
        }
        STATE["next_promo_id"] += 1
        STATE["promos"].insert(0, row)
        return row

    @app.get("/api/promos/rejections")
    def promo_rejections(limit: int = 200) -> dict[str, Any]:
        return {"rejections": STATE["promo_rejections"][:limit]}

    def _reject(row: dict[str, Any], reason: str) -> dict[str, Any]:
        at = iso(now())
        row["status"] = "rejected"
        row["rejected_reason"] = reason
        row["rejected_at"] = at
        STATE["promo_rejections"].insert(
            0,
            {
                "id": len(STATE["promo_rejections"]) + 1,
                "identity": row.get("provider"),
                "provider": row.get("provider"),
                "reason": reason,
                "url": row.get("url"),
                "note_snippet": str(row.get("note") or "")[:200] or None,
                "rejected_at": at,
            },
        )
        return row

    @app.patch("/api/promos/{promo_id}")
    def patch_promo(
        promo_id: int, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        row = _find_promo(promo_id)
        if payload.get("status") == "rejected":
            reason = str(payload.get("rejected_reason") or "").strip()
            if len(reason) < REJECT_REASON_MIN:
                raise HTTPException(status_code=422, detail=REJECT_REASON_REQUIRED)
            return _reject(row, reason)
        if payload.get("status"):
            row["status"] = str(payload["status"])
        if "note" in payload:
            row["note"] = str(payload.get("note") or "")
        return row

    @app.post("/api/promos/{promo_id}/reject")
    def reject_promo(
        promo_id: int, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < REJECT_REASON_MIN:
            raise HTTPException(status_code=422, detail=REJECT_REASON_REQUIRED)
        return _reject(_find_promo(promo_id), reason)

    @app.post("/api/promos/{promo_id}/reopen")
    def reopen_promo(promo_id: int, request: Request) -> dict[str, Any]:
        _require_token(request)
        row = _find_promo(promo_id)
        row["status"] = "known"
        row["rejected_reason"] = None
        row["rejected_at"] = None
        return row

    @app.get("/api/scout/status")
    def scout_status() -> dict[str, Any]:
        scout = STATE["scout"]
        active = scout.get("active_run_id") is not None
        last_run = None
        last_id = scout.get("last_run_id")
        if last_id and last_id in STATE["scout_runs"]:
            last_run = _run_summary(STATE["scout_runs"][last_id])
        return {
            "active": active,
            "next_run_at": None if active else _next_run_at(scout.get("schedule")),
            "schedule": scout.get("schedule"),
            "last_run": last_run,
        }

    @app.get("/api/scout/runs")
    def scout_runs_list(limit: int = 10) -> dict[str, Any]:
        ordered = sorted(STATE["scout_runs"].values(), key=lambda r: r["started_at"], reverse=True)
        return {"runs": [_run_summary(r) for r in ordered[: max(1, limit)]]}

    @app.get("/api/scout/runs/{run_id}")
    def scout_run_detail(run_id: str) -> dict[str, Any]:
        run = STATE["scout_runs"].get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return run

    @app.post("/api/scout/run")
    def scout_run_start(request: Request) -> JSONResponse:
        _require_token(request)
        if STATE["scout"].get("active_run_id") is not None:
            raise HTTPException(status_code=409, detail="a scout run is already active")
        started = now()
        run_id = "run-" + started.strftime("%Y%m%d-%H%M%S")
        run = {
            "id": run_id,
            "started_at": iso(started),
            "finished_at": None,
            "status": "running",
            "sources": SCOUT_SOURCES_COUNT,
            "pages_fetched": 0,
            "pages_changed": 0,
            "offers": 0,
            "new": 0,
            "updated": 0,
            "skipped": 0,
            "models_used": {"extract": [], "curate": []},
            "tokens": {"extract": {"in": 0, "out": 0}, "curate": {"in": 0, "out": 0}},
            "errors": 0,
            "report_md": "",
            "decisions": [],
        }
        STATE["scout_runs"][run_id] = run
        STATE["scout"]["active_run_id"] = run_id
        timer = threading.Timer(SCOUT_RUN_SECONDS, _finish_scout_run, args=(run_id,))
        timer.daemon = True
        timer.start()
        return JSONResponse({"run_id": run_id}, status_code=202)

    @app.post("/api/models/{key:path}/forgive")
    def forgive(key: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        m = _find_model(key)
        m["status"] = "ok"
        m["last_error"] = None
        for w in m["windows"].values():
            if w:
                w["used"] = 0
        return {"ok": True, "key": key, "status": "ok"}

    @app.post("/api/models/{key:path}/disable")
    def disable(key: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        m = _find_model(key)
        m["status"] = "disabled"
        return {"ok": True, "key": key, "status": "disabled"}

    @app.post("/api/models/{key:path}/enable")
    def enable(key: str, request: Request) -> dict[str, Any]:
        _require_token(request)
        m = _find_model(key)
        m["status"] = "ok"
        return {"ok": True, "key": key, "status": "ok"}

    @app.get("/api/registry")
    def registry() -> dict[str, Any]:
        return {
            "providers": STATE["registry"],
            "aliases": {
                "auto": {
                    "prefer": [
                        "explabs/gpt-6-astra",
                        "explabs/claude-fable-5.1",
                        "dashscope/qwen3.7-plus",
                        "zai/glm-4.5-flash",
                    ]
                },
                "vision": {
                    "require": ["vision"],
                    "prefer": ["explabs/gpt-6-astra", "dashscope/qwen3-vl-plus"],
                },
            },
        }

    @app.post("/api/registry/reload")
    def registry_reload(request: Request) -> dict[str, Any]:
        _require_token(request)
        return {"ok": True, "reloaded_at": iso(now())}

    @app.get("/api/providers/known")
    def providers_known() -> dict[str, Any]:
        return {"providers": KNOWN_PROVIDERS}

    @app.post("/api/accounts")
    def add_account(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _require_token(request)
        provider = str(payload.get("provider") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        if not provider or not account_id:
            raise HTTPException(status_code=400, detail="provider and account_id are required")

        tpl = _known(provider) or {}
        prov = STATE["registry"].get(provider)
        if prov is None:
            prov = {
                "kind": payload.get("kind") or tpl.get("kind") or "openai",
                "base_url": payload.get("base_url") or tpl.get("base_url"),
                "docs_url": tpl.get("docs_url"),
                "accounts": [],
                "models": [],
            }
            STATE["registry"][provider] = prov
        else:
            if payload.get("base_url"):
                prov["base_url"] = payload["base_url"]
            if payload.get("kind"):
                prov["kind"] = payload["kind"]
        fields = payload.get("fields") or {}
        if fields:
            prov["fields"] = {**prov.get("fields", {}), **fields}
        if payload.get("docs_url"):
            prov["docs_url"] = payload["docs_url"]
        if payload.get("template"):
            prov["template"] = payload["template"]

        acc = next((a for a in prov["accounts"] if a["id"] == account_id), None)
        created = acc is None
        if acc is not None:
            # mirrors the real backend: a repeated account id is a conflict, so the dashboard
            # falls back to the per-account models path for the discover -> register step
            raise HTTPException(status_code=409, detail=f"account {provider}/{account_id} already exists")
        if acc is None:
            acc = {
                "id": account_id,
                "api_key_env": payload.get("api_key_env")
                or tpl.get("api_key_env")
                or (provider.upper().replace("-", "_") + "_API_KEY"),
                "key_present": False,
                "env_file": f"~/.llmhub/env/{provider}.env",
            }
            prov["accounts"].append(acc)
        if payload.get("api_key_env"):
            acc["api_key_env"] = payload["api_key_env"]
        if payload.get("api_key"):
            acc["key_present"] = True
        if payload.get("activated_at"):
            acc["activated_at"] = payload["activated_at"]

        added = []
        for m in payload.get("models") or []:
            m = dict(m) if isinstance(m, dict) else {"id": str(m), "free": {}}
            if not m.get("id"):
                continue
            if any(x["id"] == m["id"] for x in prov["models"]):
                continue
            prov["models"].append(m)
            added.append(m["id"])
            key = "{}/{}".format(provider, m["id"])
            if not any(x["key"] == key for x in STATE["models"]):
                STATE["models"].append(
                    {
                        "key": key,
                        "provider": provider,
                        "account": account_id,
                        "model": m["id"],
                        "caps": m.get("caps") or ["text"],
                        "status": "ok",
                        "windows": {"hourly": None, "daily": None, "monthly": None},
                        "last_error": None,
                        "last_ok_at": None,
                        "avg_latency_ms": None,
                    }
                )

        out: dict[str, Any] = {
            "ok": True,
            "created": created,
            "provider": provider,
            "account": {
                "id": account_id,
                "api_key_env": acc["api_key_env"],
                "key_present": acc["key_present"],
                "env_file": acc["env_file"],
            },
            "models_added": added,
            "reloaded": True,
        }
        warning = _lan_warning(request)
        if warning:
            out["warning"] = warning
        return out

    @app.post("/api/accounts/quick")
    def add_account_quick(request: Request, payload: dict[str, Any] = Body(default={})) -> Any:
        _require_token(request)
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key is required")
        source = str(payload.get("source") or "").strip()
        base_url = str(payload.get("base_url") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        promo_id = payload.get("promo_id")

        promo = _find_promo(promo_id) if promo_id is not None else None
        hint = source
        if promo is not None and not hint:
            hint = "{} {}".format(promo.get("provider") or "", promo.get("url") or "")

        provider = _catalog_match(hint) if hint else None
        if provider is None and hint and hint.strip() in STATE["registry"]:
            provider = hint.strip()
        if provider is None:
            provider = _prefix_match(api_key)

        if provider is None:
            # 422 bodies are flat: {needs, guess}, guess = candidate template ids
            named = (promo.get("provider") if promo else "") or ""
            if named and not base_url:
                promo_base_url = (promo or {}).get("base_url")
                return JSONResponse(
                    {
                        "needs": ["base_url"],
                        "guess": [named],
                        "guesses": [
                            {
                                "url": promo_base_url or f"https://api.{named}.com/v1",
                                "status": "exists_key_rejected",
                            },
                            {"url": f"https://{named}.com/v1", "status": "no_response"},
                            {"url": f"https://api.{named}.ai/v1", "status": "no_response"},
                        ],
                    },
                    status_code=422,
                )
            if not named and not base_url:
                return JSONResponse(
                    {
                        "needs": ["source"],
                        "guess": ["openrouter", "gemini", "groq", "cerebras", "mistral"],
                        "message": "cannot tell who this key is from - pick the provider",
                    },
                    status_code=422,
                )
            provider = named or (_guess_base_url(base_url) or "custom").split("//")[-1].split(".")[0]

        tpl = _known(provider)
        prov = STATE["registry"].get(provider)
        created_provider = False
        discovered = False
        rotated = False

        if prov is None:
            if not (tpl and tpl.get("base_url")) and not base_url:
                return JSONResponse(
                    {
                        "needs": ["base_url"],
                        "guess": [provider],
                        "guesses": [
                            {"url": f"https://api.{provider}.com/v1", "status": "exists_key_rejected"},
                            {"url": f"https://{provider}.com/v1", "status": "no_response"},
                            {"url": f"https://api.{provider}.ai/v1", "status": "no_response"},
                        ],
                    },
                    status_code=422,
                )
            required_fields = [f for f in (tpl or {}).get("fields", []) if f.get("required")]
            missing_fields = [f["name"] for f in required_fields if not fields.get(f["name"])]
            if missing_fields and not base_url:
                return JSONResponse(
                    {
                        "needs": missing_fields,
                        "guess": [provider],
                        "message": "{} needs {}".format(provider, ", ".join(missing_fields)),
                    },
                    status_code=422,
                )
            resolved_base_url = base_url or (tpl or {}).get("base_url")
            for f in required_fields:
                v = fields.get(f["name"])
                if v:
                    resolved_base_url = resolved_base_url.replace("{{{}}}".format(f["name"]), v)
            prov = {
                "kind": (tpl or {}).get("kind", "openai"),
                "base_url": resolved_base_url,
                "docs_url": (tpl or {}).get("docs_url"),
                "accounts": [],
                "models": [],
            }
            STATE["registry"][provider] = prov
            created_provider = True

        acc = None
        if account_id:
            acc = next((a for a in prov["accounts"] if a["id"] == account_id), None)
        elif prov["accounts"]:
            acc = prov["accounts"][0]
        if acc is None:
            account_id = account_id or (provider + "-main")
            acc = {
                "id": account_id,
                "api_key_env": (tpl or {}).get("api_key_env")
                or (provider.upper().replace("-", "_") + "_API_KEY"),
                "key_present": True,
                "env_file": f"~/.llmhub/env/{provider}.env",
            }
            prov["accounts"].append(acc)
        else:
            # the key replaced an existing one: the env line is rewritten in place
            account_id = acc["id"]
            acc["key_present"] = True
            acc["rotated_at"] = iso(now())
            rotated = True

        if not prov["models"]:
            models = [dict(m) for m in (tpl or {}).get("models", [])]
            if not models:
                ids = DISCOVERED_BY_PROVIDER.get(provider) or [f"{provider}-small", f"{provider}-large"]
                models = [
                    {"id": i, "caps": ["text"], "free": {}, "notes": "limits unknown, discovered"}
                    for i in ids
                ]
                discovered = True
            for m in models:
                prov["models"].append(m)
                key = "{}/{}".format(provider, m["id"])
                if not any(x["key"] == key for x in STATE["models"]):
                    STATE["models"].append(
                        {
                            "key": key,
                            "provider": provider,
                            "account": account_id,
                            "model": m["id"],
                            "caps": m.get("caps") or ["text"],
                            "status": "ok",
                            "windows": {"hourly": None, "daily": None, "monthly": None},
                            "last_error": None,
                            "last_ok_at": None,
                            "avg_latency_ms": None,
                        }
                    )

        model_ids = [m["id"] for m in prov["models"]]
        free_ids = [m["id"] for m in prov["models"] if m.get("free") is not None]
        first = free_ids[0] if free_ids else (model_ids[0] if model_ids else None)
        if first is None:
            test: dict[str, Any] = {
                "ok": False,
                "status": "error",
                "model": None,
                "latency_ms": None,
                "error": "no model to test",
            }
        elif len(api_key) < 12:
            # a truncated paste: the vendor rejects the key, the account row still exists
            test = {
                "ok": False,
                "status": "error",
                "model": f"{provider}/{first}",
                "http_status": 401,
                "latency_ms": 180,
                "error": {"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}},
            }
        elif "vl" in first or "vision" in first:
            test = {
                "ok": False,
                "status": "quota",
                "model": f"{provider}/{first}",
                "latency_ms": 300 + (abs(hash(first)) % 900),
                "error_code": "AllocationQuota.FreeTierOnly",
                "error": "free quota has been exhausted",
            }
        else:
            test = {
                "ok": True,
                "status": "ok",
                "model": f"{provider}/{first}",
                "latency_ms": 300 + (abs(hash(first)) % 1500),
            }

        out: dict[str, Any] = {
            "ok": True,
            "provider": provider,
            "account_id": account_id,
            "created_provider": created_provider,
            "models": model_ids,
            "discovered": discovered,
            "test": test,
            "reloaded": True,
        }
        if rotated:
            out["rotated"] = True
        if promo is not None:
            promo["status"] = "used"
            promo["account_key"] = f"{provider}/{account_id}"
            out["promo_id"] = promo["id"]
        warning = _lan_warning(request)
        if warning:
            out["warning"] = warning
        return out

    @app.post("/api/accounts/{provider}/{account_id}/models")
    def add_models(
        provider: str, account_id: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        prov, _acc = _registry_account(provider, account_id)
        added = []
        for m in payload.get("models") or []:
            m = dict(m) if isinstance(m, dict) else {"id": str(m), "free": {}}
            if not m.get("id") or any(x["id"] == m["id"] for x in prov["models"]):
                continue
            prov["models"].append(m)
            added.append(m["id"])
            key = "{}/{}".format(provider, m["id"])
            if not any(x["key"] == key for x in STATE["models"]):
                STATE["models"].append(
                    {
                        "key": key,
                        "provider": provider,
                        "account": account_id,
                        "model": m["id"],
                        "caps": m.get("caps") or ["text"],
                        "status": "ok",
                        "windows": {"hourly": None, "daily": None, "monthly": None},
                        "last_error": None,
                        "last_ok_at": None,
                        "avg_latency_ms": None,
                    }
                )
        out: dict[str, Any] = {
            "ok": True,
            "provider": provider,
            "account_id": account_id,
            "models_added": added,
            "models": [m["id"] for m in prov["models"]],
            "reloaded": True,
        }
        warning = _lan_warning(request)
        if warning:
            out["warning"] = warning
        return out

    @app.put("/api/accounts/{provider}/{account_id}/key")
    def rotate_key(
        provider: str, account_id: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        _prov, acc = _registry_account(provider, account_id)
        if not payload.get("api_key"):
            raise HTTPException(status_code=400, detail="api_key is required")
        acc["key_present"] = True
        acc["rotated_at"] = iso(now())
        out: dict[str, Any] = {
            "ok": True,
            "provider": provider,
            "account": account_id,
            "key_present": True,
            "env_file": acc["env_file"],
            "rotated_at": acc["rotated_at"],
            "reloaded": True,
        }
        warning = _lan_warning(request)
        if warning:
            out["warning"] = warning
        return out

    @app.delete("/api/accounts/{provider}/{account_id}")
    def delete_account(
        provider: str, account_id: str, request: Request, purge_key: int = 0
    ) -> dict[str, Any]:
        _require_token(request)
        prov, acc = _registry_account(provider, account_id)
        prov["accounts"] = [a for a in prov["accounts"] if a["id"] != account_id]
        STATE["models"] = [
            m for m in STATE["models"] if not (m["provider"] == provider and m["account"] == account_id)
        ]
        if not prov["accounts"]:
            STATE["registry"].pop(provider, None)
        return {
            "ok": True,
            "provider": provider,
            "account": account_id,
            "key_purged": bool(purge_key),
            "env_file": acc["env_file"],
            "reloaded": True,
        }

    @app.post("/api/accounts/{provider}/{account_id}/test")
    def test_account(
        provider: str, account_id: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        prov, _acc = _registry_account(provider, account_id)
        models = prov.get("models") or []
        model_id = payload.get("model") or (models[0]["id"] if models else None)
        if not model_id:
            raise HTTPException(status_code=400, detail="no model registered for this account")
        model = next((m for m in models if m["id"] == model_id), None)
        if model is None:
            raise HTTPException(status_code=404, detail="unknown model " + str(model_id))
        if model.get("free") is None and not payload.get("allow_paid"):
            raise HTTPException(
                status_code=409,
                detail=f"{provider}/{model_id} is not marked free in the registry; resend with allow_paid to test it",
            )
        latency = 300 + (abs(hash(model_id)) % 900)
        base = {
            "provider": provider,
            "account_id": account_id,
            "model": f"{provider}/{model_id}",
            "model_id": model_id,
        }
        if "vl" in model_id or "vision" in model_id:
            return {
                **base,
                "ok": False,
                "status": "quota",
                "error_code": "AllocationQuota.FreeTierOnly",
                "http_status": 403,
                "latency_ms": latency,
                "attempts": [],
                "attempt_count": 1,
                "error": {
                    "error": {
                        "code": "AllocationQuota.FreeTierOnly",
                        "message": "free quota has been exhausted",
                    }
                },
            }
        out: dict[str, Any] = {
            **base,
            "ok": True,
            "status": "ok",
            "http_status": 200,
            "latency_ms": latency,
            "attempts": [{"model": base["model"], "status": "ok"}],
            "attempt_count": 1,
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            "sample": "pong",
        }
        if payload.get("allow_paid"):
            out["warning"] = "paid model tested with allow_paid, this request may have cost money"
        return out

    @app.post("/api/providers/{provider}/discover")
    def discover(
        provider: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        _require_token(request)
        account_id = str(payload.get("account_id") or "")
        prov = STATE["registry"].get(provider)
        if prov is None:
            raise HTTPException(status_code=404, detail="unknown provider " + provider)
        if account_id and not any(a["id"] == account_id for a in prov["accounts"]):
            raise HTTPException(status_code=404, detail=f"unknown account {provider}/{account_id}")
        ids = DISCOVERED_BY_PROVIDER.get(provider)
        if ids is None:
            tpl = _known(provider)
            if tpl:
                ids = [m["id"] for m in tpl.get("models", [])]
            else:
                ids = [
                    f"{provider}-small",
                    f"{provider}-medium",
                    f"{provider}-large",
                    f"{provider}-vision-preview",
                ]
        return {
            "provider": provider,
            "account_id": account_id,
            "base_url": prov.get("base_url"),
            "ok": True,
            "http_status": 200,
            "note": "limits unknown",
            "models": list(ids),
            "count": len(ids),
            "registered": [m["id"] for m in prov.get("models", [])],
        }

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app.include_router(router)
    return app


def create_app() -> FastAPI:
    _seed_usage()
    _seed_jobs()
    _seed_registry()
    _seed_scout()
    hub = create_hub_app()
    root = FastAPI(title="llmhub dev mock root", docs_url=None, redoc_url=None)
    root.mount("/hub", hub)
    root.mount("/", hub)
    return root


def main() -> None:
    global REQUIRE_TOKEN
    parser = argparse.ArgumentParser(description="llmhub dashboard dev mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument(
        "--require-token", default=None, help="require Authorization: Bearer <token> on mutating endpoints"
    )
    args = parser.parse_args()
    REQUIRE_TOKEN = args.require_token

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
