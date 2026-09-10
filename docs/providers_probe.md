# Provider probe notes (2026-09-07)

Read-only recon for the llmhub registry (`~/.llmhub/providers.yaml`, schema in `DESIGN.md`).
All requests used `max_tokens: 5`, one request per model. No key values are printed anywhere
below.

---

## 1. Experiential Labs (`explabs`)

- Auth env var: `EXPLABS_API_KEY` (Bearer)
- Two hosts, two different APIs:
  - `api.experientiallabs.ai` — OpenAI-compatible inference gateway, keyed with the API key.
  - `platform.experientiallabs.ai` — dashboard/management API. `Bearer <API key>` gets 401
    (`{"error":"Authentication required."}`) on every `/api/*` route tried
    (`/api/models` list, `/api/gateway/usage/daily`, `/api/orgs`, `/api/me`, `/api/billing`).
    This host wants a browser session token, not the inference API key — the docs' claim that
    `GET /api/models` is a public keyless catalog read did **not** hold for the list route, but
    single-slug reads (`GET /api/models/{slug}`) **are** keyless and returned 200 with full
    pricing/capability data. `GET /api/models/{slug}/waterfall` etc. were not tried (out of
    scope, would need the same session auth).

### Models endpoint

`GET https://api.experientiallabs.ai/v1/models` → 200, bare OpenAI list: 311 models, each only
`{id, object, created, owned_by}` — **no pricing field on this endpoint**, contrary to what
`/docs/reference` claims ("Lists available model slugs with pricing data"). Both target models
are present: `gpt-6-astra`, `claude-fable-5.1`.

Pricing/caps only show up per-slug on the platform host:
`GET https://platform.experientiallabs.ai/api/models/{slug}` (keyless, 200):

| slug | context | max_out | modalities in/out | list price in/cached/out ($/M) | pricing_source | requires_payment (default route) |
|---|---|---|---|---|---|---|
| gpt-6-astra | 1,050,000 | 128,000 | text+image / text | 10 / 1 / 50 | provider-docs | false (openai direct) |
| claude-fable-5.1 | 1,000,000 | 128,000 | text+image / text | 10 / 0.25 / 50 | provider-docs | false (anthropic direct, waterfall position 0) |

Both are normally-paid models (real list price, not $0) that Experiential Labs is currently
subsidizing as a promotional free daily tier. `claude-fable-5.1` also has an `openrouter` route
(`requires_payment: true`) and a disabled `bedrock` route — the free tier only applies to the
default (position-0) route, which for `claude-fable-5.1` is direct Anthropic.

No systematic scan of all 311 models for `price==0` was done (would be 311 individual GETs).
Not needed: `/docs/billing` states explicitly, as of today, only these two models carry the
promo: *"Some platform-funded models carry a promotional free daily tier (today `gpt-6-astra`
and `claude-fable-5.1`)."*

### Chat completions + headers

`POST /v1/chat/completions`, `gpt-6-astra` → **HTTP 429**, already exhausted at probe time:
```
{"error":{"message":"You've hit the free limit for GPT-6 Astra free daily tier (375,000 input
/ 75,000 output tokens per day). It resets at 00:00 UTC. Credit overflow -- keeping going on
your credits past the free limit, for all free models in your organization -- unlocks once
your organization has a card on file and the settled $1 verification; any purchase or Pro
subscription unlocks it too and turns it on automatically. Requests using your own provider
keys (BYOK) are never rate-limited.","type":"insufficient_quota","param":null,
"code":"free_limit_reached"}}
```
Headers on this response: only `date`, `content-type`, `x-correlation-id`,
`strict-transport-security`. **No `x-ratelimit-*`, no `retry-after`.**

`POST /v1/chat/completions`, `claude-fable-5.1` → **HTTP 200**, `usage.cost: 0.0` (confirms
free). Headers: `x-request-id`, `x-gateway-alias`, `x-gateway-alias-revision`,
`x-gateway-canonical-model`, `x-gateway-provider: anthropic`, `x-gateway-deployment`,
`x-gateway-route-depth: 0`, `x-gateway-route-reason: direct`, `x-correlation-id`. These are
routing/observability headers, not quota headers — still **no `x-ratelimit-*`, no
remaining/reset field anywhere**.

### Usage/org/billing probes

| endpoint | host | result |
|---|---|---|
| `GET /v1/usage` | api | 404 `{"error":{"code":"not_found","message":"Unsupported serving path: /v1/usage"}}` |
| `GET /api/gateway/usage/daily` | platform | 401 (needs session auth) |
| `GET /api/orgs` | platform | 401 |
| `GET /api/me` | platform | 401 |
| `GET /api/billing` | platform | 401 |

None answered 200 with the API key. Per `/docs/reference` and `/docs/telemetry`, the intended
usage/billing surface on the platform host is: `GET /api/gateway/usage/daily` (rollup by
day/model/member, spend in micro-USD), `GET /api/gateway/usage/events` (per-request stream),
`GET /api/gateway/keys/{api_key_id}/limits` (**this looks like the actual rate-limit/spend-cap
endpoint** — not reachable without a key id + session auth), `GET /api/orgs/{org_id}/telemetry/traces`.
All gated behind the platform session, not the inference API key.

### Docs quotes

`/docs/billing`: *"Each tier has per-org daily and hourly token allowances (the model page
names the exact numbers)."* — confirms hourly allowances exist per model, but the numeric
hourly figure was **not** surfaced anywhere I could reach (not in the 429 body, not in the
keyless model-detail JSON). Only the daily figure (375k in / 75k out) came through, via the
error message text.

*"the request answers `429 insufficient_quota` with a `free_limit_reached` message and does
not spend credits."* — matches observed behavior exactly.

*"requests past the free limit bill the overage to your credits at list price instead of
throttling"* (credit overflow) — requires *"a saved card and one settled $1 charge on the
organization"*. This is the verification charge: $1, one-time, unlocks credit overflow for all
free models org-wide, or auto-unlocks via Pro subscription / any purchase.

Resets: daily 00:00 UTC, hourly top-of-hour (`/docs/billing`).

`/docs/models`: confirms two pricing lanes — BYOK (pass-through, provider bills directly) vs
platform-funded (billed through Experiential Labs credits). No free-tier list beyond what
`/docs/billing` already named.

### Gotchas
- `/v1/models` has no pricing; `/api/models/{slug}` (platform host, keyless) does. Two
  different hosts needed to build a full picture.
- `/api/models` (list, no slug) needs platform session auth even though single-slug reads
  don't — inconsistent with the "public catalog" description in `/docs/reference`.
- No rate-limit response headers on any call, success or 429. All quota-state comes from
  parsing the 429 error body text, not headers.
- Free-tier hourly cap number is not exposed via API at all (docs only, and only as "the model
  page names the exact numbers" — the model page contains no such number for gpt-6-astra as
  probed today).

---

## 2. z.ai (`zai`)

- Auth env var: `ZAI_API_KEY` (Bearer)
- Base: `https://api.z.ai/api/paas/v4`

### Models endpoint

`GET /v4/models` → 200, 10 entries: `glm-4.5`, `glm-4.5-air`, `glm-4.6`, `glm-4.7`, `glm-5`,
`glm-5-turbo`, `glm-5.1`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash`. **Neither `glm-4.5-flash` nor
`glm-4.6v-flash` appear in this list**, even though both are callable via chat completions —
the models list is curated/incomplete, not a full catalog.

### Chat completions + headers

`glm-4.5-flash` with `"thinking":{"type":"disabled"}` → **HTTP 200**.
```
{"choices":[{"finish_reason":"length","index":0,
"message":{"content":"Hello! 👋 How","role":"assistant"}}], ...,
"usage":{"completion_tokens":5,"prompt_tokens":10,
"prompt_tokens_details":{"cached_tokens":4},"total_tokens":15}}
```
Headers: `date`, `content-type`, `content-length`, `alt-svc`, `set-cookie: acw_tc=...`
(load-balancer affinity cookie, not a quota token), `ga-traceid`, `x-log-id`, `vary` (×3),
`strict-transport-security`. **No `x-ratelimit-*` anywhere.**

`glm-4.6v-flash` → **HTTP 429** both attempts (tried twice since the error looked transient):
```
{"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}}
```
Same header shape as above, no rate-limit fields. Can't tell from this alone whether
`glm-4.6v-flash` is actually overloaded or simply not a valid/routable model id for this
account — it's absent from `/v4/models` too, same as `glm-4.5-flash` which does work, so
absence from the list isn't diagnostic either way.

`glm-4.6` (paid, one probe to confirm the error shape) → **HTTP 429**:
```
{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}
```
Matches the registry's known "quota gone" string exactly (`DESIGN.md` line: *"z.ai code
1113"*).

### Docs quotes

`docs.z.ai/api-reference/api-code.md`:
- **1113** (HTTP 429): "Insufficient balance or no resource package. Please recharge." — no
  balance/resource package for this (account, model).
- **1305** (HTTP 429): "The service may be temporarily overloaded, please try again later." —
  documented as transient, not quota.

`docs.z.ai/guides/overview/pricing`: lists **GLM-4.5-Flash** and **GLM-4.6V-Flash** with
pricing marked **"Free"** for input, cached input, and output — confirms both are meant to be
free-tier models per the public pricing page (so the 1305 on `glm-4.6v-flash` is more likely
transient/overload or a temporary account-side block than "not actually free").

No RPM or concurrency numbers found in public docs. `docs.z.ai/api-reference/rate-limit.md`
307-redirects to `https://z.ai/manage-apikey/rate-limits`, which is a logged-in console page
(SPA shell only, no static rate-limit table reachable without a browser session).
`docs.z.ai/guides/develop/rate-limits` (guessed path) → 404.

### Gotchas
- `/v4/models` list is not the full catalog — don't use it to decide what's callable.
- No rate-limit headers on any response (success or error).
- Rate-limit numbers (RPM/concurrency) are only in the logged-in dashboard, not in public docs
  or API responses — not confirmable read-only.

---

## 3. DashScope Intl (`dashscope`)

- Auth env var: `DASHSCOPE_API_KEY` (Bearer)
- Base: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`

### Models endpoint

`GET /v1/models` → 200, 165 entries. Both target models present: `qwen3.7-plus`,
`qwen3-vl-plus`. List is `{id, object, created, owned_by}` only, no pricing/quota fields.

### Chat completions + headers

`qwen3.7-plus` with `enable_thinking:false` → **HTTP 403**:
```
{"error":{"message":"The free quota has been exhausted. To continue accessing the model on a
paid basis, please complete your payment information （or disable the \"use free tier only\"
mode in the management console if already completed).",
"type":"AllocationQuota.FreeTierOnly","param":null,"code":"AllocationQuota.FreeTierOnly"}, ...}
```
**Confirms the memory note is still current**: the free-quota-exhausted 403 that first fired
2026-09-06 for `qwen3.7-plus` is still firing today (2026-09-07), same error code
`AllocationQuota.FreeTierOnly`.

`qwen3-vl-plus` → **HTTP 200**, free quota not exhausted for this model:
```
{"choices":[{"message":{"content":"Hi there! 😊","reasoning_content":"","role":"assistant"}, ...}],
"usage":{"prompt_tokens":9,"completion_tokens":5,"total_tokens":14, ...}}
```
Headers on both: `x-request-id`, `x-dashscope-timeout` (a per-request timeout config value —
600s on the 403, 3600s on the 200 — **not a rate limit**, just the call's allotted server-side
timeout), `x-dashscope-call-gateway`/`x-dashscope-finished` (only on success),
`req-cost-time`/`req-arrive-time`/`resp-start-time`/`x-envoy-upstream-service-time` (latency
instrumentation), `set-cookie: acw_tc=...` (LB affinity). **No `x-ratelimit-*`, no
remaining/reset counters.**

### Docs quotes

Alibaba Cloud Model Studio, free-quota policy page:
*"Each model (such as qwen-plus, qwen3.6-plus, or qwen3.6-plus-2026-04-02) has its own
independent free quota (typically 1,000,000 tokens)."*
*"The free quota is valid for 90 days, starting from the date you activate Alibaba Cloud Model
Studio, the model is released, or your model request is approved (whichever is later)."*
*"some models do not participate in the new user free quota program"* — must check per-model
in the console; not all 165 listed models are eligible.

This is **not a monthly refill** — it's a one-time 1M-token allowance per model with a 90-day
window from whichever trigger date applies. Doesn't match the `monthly: {total_tokens:
1000000}` framing used in `DESIGN.md`'s example registry — that's a reasonable proxy shape
inside the schema but the underlying vendor policy is "one bucket, 90-day validity," not a
recurring monthly grant.

Vision models docs page lists `qwen3-vl-plus`, `qwen3-vl-flash`, `qwen3-vl-235b-a22b-thinking`,
plus legacy `qwen-vl-plus`/`qwen-vl-max`, but does **not** give a separate free-quota table for
vision vs text models — same generic "1M tokens/model, 90 days, check console for
eligibility" policy applies, unconfirmed per-model for the vision line specifically.

### Gotchas
- `x-dashscope-timeout` looks like a rate-limit header at a glance; it's a request timeout
  budget, not a quota counter. Don't mistake it for one when scraping headers generically.
- Free-quota exhaustion is per (account, model) and persists across days once the one-time 1M
  bucket for that model is used up — it will NOT reset daily/hourly like the schema's window
  model assumes. `qwen3.7-plus` will likely stay exhausted until the 90-day window rolls or the
  bucket is topped up by Alibaba, not until "next window reset."

---

## 4. Ollama (local)

`GET http://127.0.0.1:11434/v1/models` → 200, OpenAI-compatible, no auth needed (loopback):
```
qwen3:30b-a3b-instruct-2507-q4_K_M
gemma3:27b
qwen3:30b-a3b
qwen3:0.6b
```
`ollama list` sizes on disk: `qwen3:30b-a3b-instruct-2507-q4_K_M` 18 GB, `gemma3:27b` 17 GB,
`qwen3:30b-a3b` 18 GB, `qwen3:0.6b` 522 MB.

`ollama ps` at probe time: empty — nothing currently loaded into VRAM/resident. No live
VRAM-usage number to report; sizes above are on-disk model sizes, used as the practical proxy
for VRAM footprint at `concurrency: 1`.

Registry note: `gemma3:27b` is listed under `vision` caps in `DESIGN.md`'s example — not
verified here (no vision-capable chat request sent to Ollama; out of scope for this probe,
which targeted the three cloud accounts).

---

## 5. Anthropic

Per instruction: **no API call made** (no credits, owner's decision). `ant auth status`
output (credential values not printed, token shown only pre-truncated by the tool itself and
is likewise not reproduced here):

- Active profile: `default` (`user_oauth` credential type — Claude Code / Max-subscription
  login, not a raw API key).
- Logged in as `<owner account>`, workspace `<owner account>` ("Claude Code").
- Base URL: `https://api.anthropic.com` (default, unchanged).
- **Credential status: expired** — `expires: 2026-09-05T01:51:25+02:00 (expired 57h32m17s
  ago)` at probe time. Whether `ant`/`claude` auto-refreshes silently on next use wasn't
  tested (would require an actual call, out of scope here).

Gotcha for the `kind: claude-code` agent lane (phase 2 in `DESIGN.md`): if this OAuth
credential sits expired between sessions, the first headless `claude -p` job after a gap may
need an interactive refresh before it can run — worth a startup check in that lane rather than
assuming the stored credential is always live.

---

## Registry recommendations

Confirmed-only YAML, in `DESIGN.md` schema shape. Fields not independently confirmed are
commented out with the source of the claim.

```yaml
providers:
  explabs:
    kind: openai
    base_url: https://api.experientiallabs.ai/v1
    accounts:
      - id: explabs-main
        api_key_env: EXPLABS_API_KEY
    models:
      - id: gpt-6-astra
        caps: [text, vision, tools, json, reasoning]
        context: 1050000
        free:
          daily: {in_tokens: 375000, out_tokens: 75000}   # confirmed: 429 error body text
          # hourly: {out_tokens: 30000}                    # NOT confirmed independently; docs say
          #                                                 a per-model hourly cap exists but the
          #                                                 number wasn't surfaced anywhere reachable
        reset_tz: UTC
        notes: "promo row, list price $10/$1(cached)/$50 per M; not ZDR, public data only"
      - id: claude-fable-5.1
        caps: [text, vision, tools, json, reasoning]
        context: 1000000
        free:
          daily: {}   # exhaustion not observed this probe; assume same shape as gpt-6-astra,
                      # UNCONFIRMED numerically for this specific model
        notes: "default route = anthropic direct (position 0); openrouter route is paid"
  zai:
    kind: openai
    base_url: https://api.z.ai/api/paas/v4
    accounts: [{id: zai-main, api_key_env: ZAI_API_KEY}]
    models:
      - id: glm-4.5-flash
        caps: [text, tools]
        free: {}          # confirmed free via docs pricing page; no numeric RPM/concurrency found
        extra_body: {thinking: {type: disabled}}
      - id: glm-4.6v-flash
        caps: [text, vision]
        free: {}          # docs list it as free; live probe returned 1305 twice (overload or
                          # not-routable for this account) -- re-test before trusting
  dashscope:
    kind: openai
    base_url: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    accounts: [{id: dashscope-main, api_key_env: DASHSCOPE_API_KEY}]
    models:
      - id: qwen3.7-plus
        caps: [text, tools, json]
        free: {one_time: {total_tokens: 1000000, validity_days: 90}}  # NOT a monthly refill,
                                                                        # schema's "monthly" key
                                                                        # is the wrong shape here
        extra_body: {enable_thinking: false}
        status_now: "AllocationQuota.FreeTierOnly (403) as of 2026-09-07, exhausted since at
          least 2026-09-06 per memory"
      - id: qwen3-vl-plus
        caps: [text, vision]
        free: {one_time: {total_tokens: 1000000, validity_days: 90}}  # policy confirmed generic,
                                                                        # NOT confirmed specifically
                                                                        # for this model id
        status_now: "200 OK, quota not exhausted as of 2026-09-07"
  ollama:
    kind: openai
    base_url: http://127.0.0.1:11434/v1
    accounts: [{id: local, api_key_env: null}]
    models:
      - {id: "qwen3:30b-a3b-instruct-2507-q4_K_M", caps: [text, json], free: {}, concurrency: 1}
      - {id: "gemma3:27b", caps: [text, vision], free: {}, concurrency: 1}   # vision cap assumed
                                                                              # from model family,
                                                                              # NOT probed here
```

### Could not confirm (read-only limits)
- Experiential Labs: hourly free-tier allowance number per model (docs say it exists, not
  surfaced via API or model-detail JSON for `gpt-6-astra`); `claude-fable-5.1`'s own daily/
  hourly numbers (its quota wasn't hit during this probe); anything behind `/api/*` on the
  platform host (needs a browser session, not the API key) — including
  `/api/gateway/keys/{id}/limits`, which is probably the real source of truth for rate limits.
- z.ai: RPM/concurrency numbers for `glm-4.5-flash` / `glm-4.6v-flash` (only reachable in a
  logged-in dashboard); whether `glm-4.6v-flash`'s 1305 is transient overload or an
  account/model routing problem.
- DashScope: whether `qwen3-vl-plus` specifically participates in the standard 1M/90-day free
  quota program (generic policy confirmed, not this model by name); any RPM/concurrency caps
  (not found in fetched docs).
- Ollama: `gemma3:27b` vision capability (assumed from model family, not exercised); no live
  VRAM figure since nothing was loaded at probe time (only on-disk sizes available).
- Anthropic: nothing probed by design (no API call, no credits).
