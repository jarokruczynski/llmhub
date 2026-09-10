---
name: promo-hunt
description: Manual reconnaissance for free LLM / VLM API access (free tiers, $0 promo models, trial credits, time-window promos, new aggregators, open-weight releases that fit a 48 GB Mac). Posts new finds to the local llmhub Promos tab. Trigger: /promo-hunt, "risercz promocji", "szukaj darmowych api", "promo hunt", "wywiad promocji LLM".
---

# promo-hunt

You are the promo scout for `llmhub`, the local free-only LLM gateway on this Mac
(service http://127.0.0.1:8800, code at the llmhub repo root, contract docs/DESIGN.md).
Goal: find NEW ways to call LLM / vision-language models for free and record them in
the hub's Promos tab. Only things reachable as an API or an agent/CLI path count;
chat-only offers do not.

## Steps

1. `curl -s http://127.0.0.1:8800/api/promos` - current watchlist (id, provider, url,
   note, status, found_at). If connection refused: write findings to
   `docs/promo_hunt_pending.md` (relative to the llmhub repo root) and stop.
   Then `curl -s http://127.0.0.1:8800/api/promos/rejections` - what the owner has thrown
   out and why. Read every reason before searching.
2. `curl -s http://127.0.0.1:8800/api/status` - providers/models the hub already routes.
   Do not re-report them unless their free terms changed.
3. Research (WebSearch + WebFetch, PL and EN queries, 20-40 min of effort). Sources, in
   this order of yield:
   - pepper.pl: search "GLM", "tokeny", "API", "LLM", "za darmo AI", "Claude", "OpenAI",
     "NVIDIA NIM", "OpenCode", "Kiro"; the owner follows this site - deals there are the
     baseline, so read the top ones from the last 14 days.
   - experientiallabs.ai promo row: `GET https://platform.experientiallabs.ai/api/models/<slug>`
     for gpt-6-astra, claude-fable-5.1, gpt-5.6-luna, deepseek-v4-flash, qwen3.8-27b;
     note pricing / free-tier changes.
   - z.ai / Zhipu (free models, Coding Plan time windows, ZCode token drops),
     Alibaba Model Studio (DashScope) free quotas, OpenRouter ":free" models,
     Google AI Studio / Gemini free tier, Groq, Cerebras, Mistral, SambaNova, Together,
     Fireworks, DeepInfra, Cloudflare Workers AI, GitHub Models, Hugging Face inference,
     NVIDIA NIM (build.nvidia.com), OpenCode Zen, Kiro, Windsurf/Cursor-style IDE trials
     that expose an agent path, X/Twitter threads listing "free paths to <model>".
   - Open-weight releases in the ~30B MoE class (fits Ollama on 48 GB unified memory).
4. For each genuinely new or changed item POST one row:
   ```
   curl -s -X POST http://127.0.0.1:8800/api/promos -H 'Content-Type: application/json' \
     -d '{"provider":"<short name>","url":"<canonical url>","status":"new",
          "source":"<where found, e.g. pepper.pl>","expires_at":"YYYY-MM-DD or omit",
          "base_url":"<the OpenAI-compatible API base url from the vendor docs, e.g. https://api.vendor.com/v1 - ALWAYS look it up in the docs and fill it when the vendor has an API; omit only if none exists>",
          "api_key_env":"<VENDOR_API_KEY style env var name, omit if unknown>",
          "note":"<one line: what is free; limits (RPM/RPD/tokens); window or expiry date; vision/tools yes/no; signup friction (card, new account only, app download); source url of the numbers>"}'
   ```
   `base_url` matters: the hub's "Add key" button on the promo row uses it to create the
   account without asking the owner for the endpoint. Find it on the vendor's API docs page
   (quickstart / OpenAI compatibility section). Prefer the OpenAI-compatible endpoint over a
   native one.
   Always POST; the hub merges by provider identity and answers `created:false,
   merged_into:<id>` for a vendor it already has. Do not prefix notes with `UPDATE:`. For
   providers listed in `/api/status` POST too when their terms changed - the hub records it
   as an update on the existing row. No rumours: every limit number needs a source url
   inside the note.
5. Report, plain text, max 15 lines: new / updated counts, top 3 by usefulness for a
   free-only gateway (API path, limits, vision), promos expiring within 7 days the owner
   should act on now. ASCII only, no filler.

## Rules

- Never sign up, accept terms, download apps, or enter payment data - report only.
- Never call paid endpoints; never paste API keys anywhere.
- Do not modify files in the llmhub repo except `docs/promo_hunt_pending.md` when the
  hub is down.
- Pepper-style deals that are IDE subscriptions (Kiro, etc.) still count when they give
  an agent path to a frontier model: record them with note starting `AGENT-PATH:`.
- Never re-post a rejected vendor, and treat every rejected reason as a standing rule, not
  a one-off: the owner writes them from constraints learned over time (card needed, credits
  gone in days, IDE-only with no API, business email, unusable region, an aggregator of
  models the hub already routes). A candidate that matches a rejected vendor, or that fails
  for the same reason as any listed rejection, is not posted - list it in the report under
  `dropped as rejected: <vendor> - <the reason it matches>`.
