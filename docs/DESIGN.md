This is a chronological design log, v0 through v0.12, written in the order the hub was
built. Sections are not kept in sync with each other: a later section, and any "As built"
note under an earlier one, overrides text above it when the two disagree. Treat this file
as a record of how decisions were reached, not as the current reference - start at
[README.md](../README.md) for that.

# llmhub - design contract (v0, 2026-09-07)

Local LLM gateway for private projects (an OCR batch app, a transcript-extraction app,
future apps). One process on the Mac, SQLite, OpenAI-compatible API, dashboard. Policy:
FREE ENDPOINTS ONLY. A paid model is never selected automatically because a free one ran
dry.

## Location, runtime, ports
- Repo: `~/llmhub` (NOT under ~/Documents: launchd cannot read there - TCC).
- Python 3.12+, `uv`, FastAPI + uvicorn, SQLite at `~/.llmhub/hub.db`, httpx for upstream.
- Listens 127.0.0.1:8800. Caddy exposes `http://llmhub.localhost` (Mac) and
  `http://mac.local/hub/` (LAN, path prefix stripped by Caddy `handle_path`) -> dashboard
  must use RELATIVE urls only (no leading "/"), so it works under a prefix.
- LaunchAgent `com.llmhub.gateway` (launchd/install.sh), logs `~/Library/Logs/llmhub/`.
- Keys: env vars only, loaded from `~/.app-a/*.env` (KEY=value, chmod 600). The hub reads
  the variable NAMES from the registry; never store key values in repo, db or logs.
- CLI: `python -m llmhub check` prints the registry/quota state table (account, model, key
  present, status, windows used/limit, exhausted-until) and exits - no network calls, no
  server. `python -m llmhub [--host] [--port] [--log-level]` runs the server (default).

## Registry: `~/.llmhub/providers.yaml` (repo ships `providers.example.yaml`)
```yaml
providers:
  explabs:
    kind: openai            # openai | anthropic | ollama | claude-code
    base_url: https://api.experientiallabs.ai/v1
    accounts:
      - id: explabs-jaro
        api_key_env: EXPLABS_API_KEY
    models:
      - id: gpt-6-astra
        caps: [text, vision, tools, json, reasoning]
        context: 1050000
        free:                    # hourly | daily | monthly | allowance; missing = unlimited
          hourly:  {out_tokens: 30000}
          daily:   {in_tokens: 375000, out_tokens: 75000}
        reset_tz: UTC            # daily resets 00:00 in this tz, hourly at top of hour
        notes: "promo row; not ZDR - public data only"
      - id: claude-fable-5.1
        caps: [text, vision, tools, json, reasoning]
        free: {hourly: {out_tokens: 30000}, daily: {in_tokens: 375000, out_tokens: 75000}}
  zai:
    kind: openai
    base_url: https://api.z.ai/api/paas/v4
    accounts: [{id: zai-jaro, api_key_env: ZAI_API_KEY}]
    models:
      - {id: glm-4.5-flash,  caps: [text, tools],  free: {}, extra_body: {thinking: {type: disabled}}}
      - {id: glm-4.6v-flash, caps: [text, vision], free: {}}
  dashscope:
    kind: openai
    base_url: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    accounts: [{id: dashscope-jaro, api_key_env: DASHSCOPE_API_KEY}]
    models:
      - id: qwen3.7-plus
        caps: [text, tools, json]
        activated_at: 2026-09-07   # anchors the allowance window; account-level if omitted here
        free:
          allowance: {total_tokens: 1000000, expires_at: null}   # one-time bucket, no reset
        extra_body: {enable_thinking: false}
      - {id: qwen3-vl-plus, caps: [text, vision], free: {allowance: {total_tokens: 1000000, expires_at: null}}}
  ollama:
    kind: openai
    base_url: http://127.0.0.1:11434/v1
    accounts: [{id: local, api_key_env: null}]
    models:
      - {id: "qwen3:30b-a3b-instruct-2507-q4_K_M", caps: [text, json], free: {}, concurrency: 1}
      - {id: "gemma3:27b", caps: [text, vision], free: {}, concurrency: 1}
aliases:
  auto: {spread: 4, prefer: [explabs/gpt-6-astra, explabs/claude-fable-5.1, dashscope/qwen3.7-plus, zai/glm-4.5-flash, ollama/qwen3:30b-a3b-instruct-2507-q4_K_M]}
  vision: {spread: 4, require: [vision], prefer: [explabs/gpt-6-astra, explabs/claude-fable-5.1, dashscope/qwen3-vl-plus, zai/glm-4.6v-flash, ollama/gemma3:27b]}
  extract:                        # long inputs, strict JSON, verbatim quoting
    spread: 3
    require: [text, json]         # requirement: a candidate without these caps is out
    min_context: 24000            # requirement: context must be known and at least this wide
    avoid: [reasoning]            # preference: sorts these last, never removes them
    max_latency_ms: 30000         # preference: same, for a model measured slower than this
    prefer: [gemini/gemini-3.5-flash-lite, explabs/gpt-6-astra, zai/glm-4.5-flash]
```
Model key everywhere = `provider/model_id`. Multiple accounts per provider = rotation: quota
windows are tracked PER (account, model). `activated_at` (date or datetime, set on the model
or the account - model wins) anchors `allowance` windows; if unset it falls back to the first
usage timestamp seen for that (account, model).

### Alias spread
Free tiers are per model (Gemini counts requests per day per model, explabs per model), so a
strict walk down `prefer` parks every request on the first entry until it 429s and leaves the
rest of the pool idle. `spread: N` on an alias says how wide to fan out:

- Selection builds the eligible list as before (caps, free/paid, key present, not disabled,
  not expired/exhausted/cooling, quota room), ordered by `X-Hub-Prefer`, then `prefer`, then
  registry order.
- The first `N` of that list are the **active set**. Inside it the pick is least recently used
  first, with anything holding a busy `concurrency` semaphore pushed to the back. The LRU
  clock lives in memory per (account, model) and is seeded from the `usage` table at startup,
  so a restart or a registry reload does not send every alias back to entry one.
- Candidates past the active set keep their order and stay as fallbacks; `router.run` walks
  the reordered list top to bottom.
- `spread: 1` = strict prefer order, the pre-spread behaviour. Default when the yaml says
  nothing: 4 for `auto`, `vision`, `fast`, 1 for every other alias (`local` included) and for
  a request that names a model key directly.
- `X-Hub-Prefer` still wins outright: its model moves to the front and is the first attempt,
  ahead of the LRU rotation.

Window kinds: `hourly`, `daily`, `monthly` reset on a schedule in `reset_tz` (top of hour,
midnight, first of month). `allowance` is a one-time bucket counted since `activated_at` with
no periodic reset - it only ends at `expires_at` (status becomes `expired`) or a manual
`forgive`.

Vendor error -> quota classification (`vendor_errors.py`):
| provider  | signal                                                          | outcome |
|-----------|------------------------------------------------------------------|---------|
| openai    | code/text `insufficient_quota`                                    | quota |
| explabs   | code/text `insufficient_quota`, `free_limit_reached`               | quota |
| dashscope | text "free quota has been exhausted", code/text `AllocationQuota.FreeTierOnly` (served as HTTP 403) | quota |
| z.ai      | code `1113`, "insufficient balance"                                | quota |
| z.ai      | code `1305`                                                        | retry (transient overload) |
| z.ai      | code `1210`                                                        | error (client error, not retried) |
| anthropic | text "credit balance is too low"                                   | quota |

A `quota` classification marks the (account, model) exhausted until the window reset, or for
`allowance` until `forgive` or `expires_at`. Plain 429/5xx = retry 5/15/45 s then next
candidate. A bare `error` classification (e.g. z.ai 1210) is raised straight to the caller,
no fallback to the next candidate.

The window a quota error refers to comes from words in the body ("per day", "hourly",
"insufficient balance"), then from the matched rule. When neither says anything, the catalog
template's `quota_scope_default` decides. Gemini's free tier answers "You exceeded your
current quota" with no window word at all and its per-minute and per-day caps both come back
quickly, so `gemini -> hourly`: the model is parked for the rest of the hour, not the day.

### Observed caps
Most free endpoints publish no numbers, so their registry rows carry `free: {}` and would be
treated as infinite forever. A quota error with a `hourly`/`daily`/`monthly` scope therefore
records what the vendor actually served before saying no: a row
(account, model, window, metric `out_tokens`, value = out tokens used in that window at that
moment, observed_at). `allowance` hits record nothing - the bucket is already a declared total.

- The observed value becomes the effective limit in `has_room`/`room` whenever it is lower
  than the declared one, or no limit is declared. An observed window that is not in the yaml
  at all is added to the model's windows, which also pins the next exhaustion to that window's
  reset instead of the default hour.
- Nothing is recorded when the declared cap is already at or below what was served, or when
  the window shows zero out tokens (a zero cap would park the model for good).
- A later observation replaces the earlier one: a free tier that got wider is worth believing.
- `forgive` clears the exhaustion marker and keeps the observed caps - they are measurements,
  not punishment. `DELETE api/models/{key}/observed` drops them (all accounts of that key).

### Unsupported request params (v0.5.1)
Some model routes pin a sampling param to one fixed value and answer 400 to anything else
(e.g. an explabs route that only accepts `temperature: 1.0`) - this is not a quota hit and not
a generic bad request, it is a fact about that (account, model) worth learning once.
`vendor_errors.classify` adds a kind `unsupported_param` for a 400 whose body names a
parameter: an OpenAI-shaped `"param"` field, or text matching `value ... for '<param>' is not
supported`, `Unsupported parameter: <param>`, or a bare `'<param>' is not supported`. Detail
carries the param name.

On that classification, `gateway.call_nonstream` strips the named param from the outbound
body and retries the *same* candidate once immediately, no backoff. On success it records
`(account, model, param)` in the `unsupported_params` table and every later request to that
(account, model) has the param stripped before it is ever sent
(`strip_learned_unsupported_params`). `GET api/status` lists the learned set per model as
`unsupported_params: [...]`. A retry that fails again falls through to the router's normal
fallback (cooldown, next candidate) - it is not treated as a hard `error`, since a different
candidate may not share the same restriction. A 400 that names no parameter (e.g. explabs
"Failed to generate JSON") is unaffected and stays a plain `error`.

## Gateway API (OpenAI wire)
- `POST /v1/chat/completions` - body as OpenAI. `model` = alias (`auto`, `vision`) or
  `provider/model_id`. Streaming pass-through (SSE). `extra_body` from registry merged in.
- Headers: `X-Hub-App: my-app` (attribution, required), `X-Hub-Require: vision,json`
  (capability filter on top of alias), `X-Hub-Prefer: provider/model` (soft preference),
  `X-Hub-Allow-Paid: 1` (ONLY then paid models are eligible; default never).
- Missing `X-Hub-App` -> 400, OpenAI-shaped body naming the header:
  `{error:{message, type:"missing_header", param:"X-Hub-App", code:"missing_header",
  header:"X-Hub-App", example:"X-Hub-App: my-app"}}`.
- Unknown model/alias (chat completions or job creation) -> 400. 404 is reserved for an
  unknown job id, or an unknown model key on `forgive`/`disable`/`enable`, or an unknown
  promo id on `PATCH api/promos/{id}`.
- No eligible candidate (all rejected or exhausted) -> 429, body
  `{error:{message, type:"no_candidates", rejected:[...], next_window_at, remaining_out,
  remaining_in}}`, headers `Retry-After: 60`, `X-Hub-Next-Window`, and
  `X-Hub-Remaining-Out`/`X-Hub-Remaining-In` = max remaining across the quota-rejected free
  candidates (header omitted when no candidate was rejected for quota, or the metric is
  uncapped). A `rejected` row with `reason: "quota"` carries `detail` (binding window),
  `remaining_out`, `remaining_in`, `resets_at`, `requested_out`, `requested_in` - enough for
  the client to retry once with a smaller `max_tokens` instead of guessing.
- Response headers: `X-Hub-Model` (what actually served), `X-Hub-Account`,
  `X-Hub-Attempts`, `X-Hub-Attempt-Count`, and on success `X-Hub-Remaining-Out` = out tokens
  left for the serving (account, model) in its binding window after this call. Best effort:
  omitted when the model declares no out/total cap, and on streams (usage is known only after
  the headers are on the wire).
- Concurrency: an `asyncio.Semaphore` per (account, model) when the model sets `concurrency`
  (used for local ollama); a request over the limit waits on that semaphore rather than
  falling over to the next candidate.
- Auth: loopback = open. Non-loopback = `Authorization: Bearer <HUB_TOKEN>` (env
  `LLMHUB_TOKEN`), else 401. Dashboard read endpoints are open on LAN, mutating ones need token.
- `GET /v1/models` - eligible models with `hub` extension: caps, remaining per window, status.
- Every upstream call -> `usage` row: ts, app, provider, account, model, in/out/cached tokens,
  latency_ms, status (ok|quota|error|refusal), error_code, attempt no. Written before parsing.

## Queue (deferred jobs)
- `POST /jobs` {app, model|alias, require, priority (1 high..9 low), request: <chat body>,
  callback_url?}; `GET /jobs/{id}`; `GET /jobs?app=&status=`; `DELETE /jobs/{id}`.
- Worker: `LLMHUB_JOB_WORKERS` (default 6) jobs run concurrently in total; how many of them
  one app may hold is the fair share computed per pass, not a fixed number (see v0.11).
  Claims runnable jobs ordered `priority ASC, created_at ASC` (oldest highest-priority
  first), so an app's own jobs still start FIFO when several of them run at once. If no
  eligible model has quota right now, job waits (state `waiting_quota`, with
  `next_window_at`; the `error` column then holds a JSON note `{state, next_window_at,
  next_attempt_at, remaining_out, remaining_in}` so the dashboard can show how much room is
  left). Never fails a job for lack of quota - but it does expire one at its deadline.
- Pause/resume per app: `POST /api/apps/{app}/pause|resume`.

## Agent lane (phase 2, design only now)
- `kind: claude-code` = run `claude -p` headless on the Max subscription (private account).
  Budget knob = manual "% of weekly limit allowed" set on the dashboard; concurrency 1.
  Jobs only (no sync path). Interface: same job body, plus `workdir`, `allowed_tools`.

## Dashboard read API (JSON, used by the UI; all paths relative under the app root)
- `GET /healthz` -> {status, version} - liveness only, no auth, no hub state.
- `GET api/status` -> {generated_at, models:[{key, provider, account, model, caps, status:
  ok|exhausted|cooldown|expired|down, expires_at, windows:{hourly|daily|monthly|allowance:
  {used, limit, metric, resets_at, started_at, limit_source, details}}, observed, usage_today,
  last_error, last_ok_at, avg_latency_ms}], queue:{depth_by_state, by_app, live,
  oldest_queued_at, expired_last_24h, workers, apps:{<app>:{queued, running, cap, paused}}},
  aliases:[{alias, require, prefer, spread}], accounts:[...]}. `resets_at` is null for
  `allowance` windows and for any window carrying no limit; `metric` is whichever of
  out_tokens/total_tokens/in_tokens the window's limit is keyed on; `details` breaks used/limit
  down per token kind; the model-level `expires_at` is the earliest `allowance.expires_at`
  (null if none).
  - `limit_source` per window: `"declared"` (from the yaml) or `"observed"` (measured off a
    429, see Observed caps) - it says where the `limit` number on that window came from.
  - `observed`: `{"<window>": {"out_tokens": N, "observed_at": "<iso>"}}` for that
    (account, model), `{}` when nothing has been measured.
  - `usage_today`: `{requests, in_tokens, out_tokens, errors, last_used_at}` over the UTC day,
    per (account, model); zeros and a null `last_used_at` for a pair that served nothing
    today. One grouped query for the whole page, not one per row.
  - `apps_15m`: the apps that called this (account, model) in the last 15 minutes, sorted;
    `in_flight`: how many calls on that pair are running right now (router memory, not a query).
- `GET api/usage?since=ISO&group_by=app|model|account|day&app=` -> rows with tokens/requests/
  errors; `GET api/usage/timeseries?bucket=hour|day&since=` for charts.
- `GET api/events?limit=` -> recent errors/quota hits/fallbacks (ts, app, model, kind, message).
- `GET api/live?window_min=15` -> {generated_at, window_min, in_flight_total, apps:[{app,
  in_flight:[{call_id, state, model, account, kind, elapsed_s, attempt, job_id}],
  recent:[{model, calls, out_tokens}]}]} - calls running right now plus what each app used over
  the window; `state` is `waiting` (queued for a concurrency slot) or `running`. In-flight
  comes from the router's memory, `recent` from one grouped query; cheap enough for a 2 s poll.
- `GET api/jobs?state=` (live states by default, `all` for history) ; `GET api/apps`
  (per-app totals, paused flag, and `queued`/`running`/`cap` - the app's current fair share).
- `GET api/registry` -> provider/model/alias config as loaded from the yaml, with
  `key_present` per account - for inspecting the active registry without reading the file.
- `GET api/promos` -> watchlist rows (provider, url, note, source, found_at, status,
  expires_at) - table only now, the hunter that fills it is phase 2.
- Mutations (token on LAN): `POST api/models/{key}/forgive`, `POST api/models/{key}/disable`,
  `POST api/models/{key}/enable`, `DELETE api/models/{key}/observed` (drop the measured caps,
  -> {key, cleared, ts}), `POST api/apps/{app}/pause|resume`, `POST api/promos`,
  `PATCH api/promos/{id}` {status: new|known|used|expired, note},
  `POST api/live/{call_id}/cancel` and `POST api/live/cancel` {app} | {all: true}
  -> {cancelled:[call_id], count, ts}.

## Dashboard UI (server-rendered HTML + vanilla JS, polls api/status every 10 s)
Pages: Models (table: provider, model, caps, status pill, remaining per window with reset
countdown, latency, last error), Usage (what burns tokens: by app, by model, by day; bars),
Queue (depth, per-app, per-job rows with cancel), Accounts (per provider: accounts, status),
Promos (watchlist). Dark/light by prefers-color-scheme. English UI text. No build step.

## Out of scope v0
Promo hunter automation, claude-code lane implementation, the transcript-extraction and
OCR-batch apps' client migration
(separate sessions in those repos: add preset `hub` -> base_url http://127.0.0.1:8800/v1).

## Accounts and keys from the dashboard (v0.2)
Adding a provider account must not require editing files by hand. Storage stays the
owner's convention: key value in `~/.app-a/<provider>.env` as `<ENV_NAME>=<value>`
(chmod 600, file created or the line replaced in place), registry entry in
`~/.llmhub/providers.yaml`. The hub reloads both in-process after a write; no restart.
The key value is write-only: never returned, logged, or stored in the db.

- `GET api/providers/known` - built-in catalog of provider templates the form offers:
  id, kind, base_url, default env var name, docs url, list of known free models with caps
  and windows (explabs, zai, dashscope, openrouter, gemini (OpenAI-compatible endpoint
  https://generativelanguage.googleapis.com/v1beta/openai/), groq, cerebras, sambanova,
  opencode-zen, cloudflare-workers-ai, ollama, custom).
- `POST api/accounts` {provider, account_id, api_key, api_key_env?, base_url?, kind?,
  models?: [{id, caps, free, extra_body}], activated_at?} -> creates provider if new
  (from template or custom fields), appends the account, writes the env file, hot-reloads,
  returns the registry row (`key_present: true`, never the key). Token required off-loopback;
  the response carries a `warning` when the request arrived over plain HTTP from the LAN.
- `POST api/accounts/{provider}/{account_id}/models` {models: [{id, caps, free, extra_body}]}
  - append models to that provider (models are per provider, the account_id only has to
  exist on it, else 404); a known id is updated in place (caps/free/extra_body/notes merged,
  request wins), a new one is appended; hot-reloads and returns {added, updated, models}.
  `POST api/accounts` for an account that already exists answers 409 with a `hint` pointing
  here when the body carried a `models` list.
- `PUT api/accounts/{provider}/{account_id}/key` {api_key} - rotate: rewrite the env line.
- `DELETE api/accounts/{provider}/{account_id}` - remove from registry; the env line is
  left in place unless `?purge_key=1`.
- `POST api/accounts/{provider}/{account_id}/test` {model?} - one request with
  `max_tokens: 1` to the given or first registered model; returns status, latency,
  X-Hub-style attempt info, and the vendor error body on failure. Refused with 409 when
  the model is not marked free unless `{"allow_paid": true}` is sent.
- `POST api/providers/{provider}/discover` - `GET {base_url}/models` with the account key;
  returns the vendor's model ids so the form can tick which ones to register (all as
  `free: {}` unknown-limits unless the template knows better; note says "limits unknown").
- `POST api/registry/reload` - re-read yaml + env files.
UI: Accounts tab gets "Add account" (template picker fills base_url/env name/models;
key field is type=password, cleared after submit), per-account actions rotate / test /
remove, per-provider "discover models". Adding from the LAN shows the plain-HTTP warning.

## Quick add (v0.3) - "key + who it is from", the hub does the rest
The full Add account form stays behind an "Advanced" toggle. The default path needs two
inputs: the key and a hint of where it comes from. From the Promos tab the hint is the
row itself.

- `POST api/accounts/quick` {api_key, source?: "<free text: provider name, alias, url,
  or pasted sentence>", promo_id?: int, base_url?: str, account_id?: str}
  Resolution order: promo row (provider + url) -> `source` text matched against the
  catalog (template id, aliases, hostnames found in any url) -> key prefix
  (sk-or-v1- openrouter, AIza gemini, gsk_ groq, csk- cerebras, xpl_ explabs, nvapi- nvidia-nim,
  hf_ huggingface, sk-ant- anthropic). No template match leaves three ways to an endpoint, in
  order: `base_url` on the request -> `base_url` carried by the promo row (with the promo's
  `api_key_env`, else `<PROVIDER>_API_KEY` off the provider name) -> guess and verify ->
  422 `{"needs": ["source"|"base_url"], "guess": [...], "guesses": [...]}`.
  Then: create provider from template if missing (account_id default `<provider>-jaro`,
  env var from template, all known free models), write env line, hot-reload; if the
  template has no models -> discover and register everything the vendor lists as
  `free: {}` with notes "limits unknown, discovered"; run one test call (max_tokens 1)
  on the first free model; if `promo_id` given -> PATCH that promo `status: used` and
  store `account_key: "<provider>/<account_id>"` on it.
  Response: {provider, account_id, created_provider, models:[ids], discovered:bool,
  test:{ok, status, model, latency_ms, error?}, promo_id?, warning?}.
- Guess and verify: every hostname in the source text and the promo url/note loses its link
  label (`www. blog. docs. platform. console. dash. app. api.`) and the bare domain is
  expanded into `https://api.<domain>/v1`, `https://api.<domain>/openai/v1`,
  `https://<domain>/v1`, `https://<domain>/api/v1`, `https://api.<domain>/v1beta/openai`,
  plus a `KNOWN_ENDPOINTS` entry for the domains no pattern reaches (z.ai, ai21.com,
  chutes.ai, cohere.com, deepinfra.com, fireworks.ai, glhf.chat, nebius.com). At most 6
  candidates, probed in parallel with `GET {candidate}/models` and the submitted key, 5 s
  each. First 200 carrying a parseable model list wins: the provider is registered
  custom-style (kind openai) on that base_url, models come from the usual discover filter,
  then the one test call. Nothing wins -> 422 with `guesses: [{url, status}]`, status one of
  `ok | exists_key_rejected | not_found | no_models | no_response | http_<code>`, sorted with
  `exists_key_rejected` first so the form prefills an endpoint that is known to exist and
  shows what was tried. Two templates matching equally skips the probing: that is a naming
  question, not a missing endpoint.
- Catalog gains `aliases` and `hostnames` per template, plus templates: nvidia-nim
  (https://integrate.api.nvidia.com/v1, NVIDIA_API_KEY, moonshotai/kimi-k3, deepseek-ai/deepseek-v4-pro),
  mistral (https://api.mistral.ai/v1, MISTRAL_API_KEY), cohere
  (https://api.cohere.com/compatibility/v1, COHERE_API_KEY), huggingface
  (https://router.huggingface.co/v1, HF_TOKEN), moonshot (https://api.moonshot.ai/v1,
  MOONSHOT_API_KEY), deepseek (https://api.deepseek.com/v1, DEEPSEEK_API_KEY), xai
  (https://api.x.ai/v1, XAI_API_KEY), fireworks (https://api.fireworks.ai/inference/v1,
  FIREWORKS_API_KEY), together (https://api.together.xyz/v1, TOGETHER_API_KEY), deepinfra
  (https://api.deepinfra.com/v1/openai, DEEPINFRA_API_KEY), minimax (https://api.minimax.io/v1,
  MINIMAX_API_KEY). Templates without a verified free model list ship `models: []` and
  rely on discover; the hub still treats every registered model as `free: {}` only because
  the owner added the key on purpose - `notes` say "unverified free status".
- Promos rows: `account_key` field; `GET api/promos` returns it; UI shows "key added".
- Promos rows also carry `base_url` and `api_key_env` (accepted on POST and PATCH, returned
  on GET): a promo for a vendor off the catalog can name its own endpoint, and quick add
  takes it over guessing.
- Catalog additions (aliases + hostnames, `models: []` so discover fills them): inception
  (https://api.inceptionlabs.ai/v1, INCEPTION_API_KEY), ai21 (https://api.ai21.com/studio/v1,
  AI21_API_KEY), upstage (https://api.upstage.ai/v1, UPSTAGE_API_KEY), siliconflow
  (https://api.siliconflow.com/v1, SILICONFLOW_API_KEY), novita
  (https://api.novita.ai/openai/v1, NOVITA_API_KEY), hyperbolic
  (https://api.hyperbolic.xyz/v1, HYPERBOLIC_API_KEY), nebius
  (https://api.studio.nebius.com/v1, NEBIUS_API_KEY), chutes (https://llm.chutes.ai/v1,
  CHUTES_API_KEY).
UI: Promos row action "Add key" -> one password field + Add; result toast with provider,
model count, test result. Accounts tab: Quick add (source + key) is the default view.

## Scout (v0.5) - promo hunting on the hub's own free models
The manual Claude Code skill `promo-hunt` stays for deep dives. The hub itself runs a
cheaper, scheduled version: fetch sources without an LLM, extract candidates on cheap
free models, curate on the best model available, post to Promos, keep a run report.

Pipeline (module `llmhub/scout/`):
1. **Collect** (no LLM): a source list in `~/.llmhub/scout_sources.yaml` (repo ships
   `scout_sources.example.yaml`): pepper.pl search pages for a keyword list, vendor
   pricing/docs pages = every `docs_url` in the catalog, OpenRouter `/api/v1/models`
   filtered to $0 prompt+completion, a few RSS/Atom feeds, and optional web search when
   `BRAVE_SEARCH_API_KEY` (Brave, free tier) or `LLMHUB_SEARXNG_URL` is set. Fetch with
   httpx (10 s, 3 concurrent, polite UA), strip to text (trafilatura if installed, else a
   small html->text fallback), cache by url+day in the db (`scout_pages`), skip unchanged
   pages (content hash).
2. **Extract** on alias `fast` (through the hub's own `/v1/chat/completions`, header
   `X-Hub-App: scout`): per page, a strict JSON schema
   `{offers:[{provider,url,base_url?,what_is_free,limits,expires_at?,vision,tools,friction,
   evidence_quote}]}`; `response_format` json when the model supports it, else parse the
   first JSON object; one retry on parse failure; a page with no offers costs one call.
3. **Curate** on alias `auto` with `X-Hub-Prefer` to the strongest model that currently has
   quota (explabs astra/fable, then gemini 3.8-flash): input = extracted offers + current
   promos (`api/promos`) + catalog template ids; output = decisions
   `[{action:new|update|skip, promo_id?, row:{provider,url,base_url,api_key_env,expires_at,
   source,note}, reason}]`. Rules in the prompt: dedupe by url and provider, prefer
   official docs urls, limits need an evidence quote, no rumours, `base_url` must be an API
   endpoint (the curator may ask for one extra fetch of a docs page per offer through a
   `needs_pages:[url]` field; the pipeline fetches and re-asks once).
4. **Apply**: POST/PATCH `api/promos` with `source: "scout"`; unknown template + base_url
   -> nothing else (the owner still adds the key from the row). Write `scout_runs` row:
   started_at, finished_at, sources, pages_fetched, pages_changed, offers, new, updated,
   skipped, models_used (per stage), tokens (per stage), errors, report_md (10-15 lines).
5. **Schedule**: hub-internal daily at `LLMHUB_SCOUT_AT` (default `08:00` local, empty =
   off), plus `POST api/scout/run` (token on LAN, returns run id, runs in background),
   `GET api/scout/runs?limit=`, `GET api/scout/runs/{id}` (full report + decisions), and
   CLI `python -m llmhub scout [--dry-run]` (dry-run prints decisions, posts nothing).
   Concurrency 1 (a second run while one is active -> 409).
Budget guard: the scout stops when the hub answers 429 twice in a row on `fast` and
finishes with what it has; it never sets `X-Hub-Allow-Paid`.
UI: Promos tab gets a "Scout" box at the top: last run (when, models, counts), "Run now",
link to the report; each promo row shows `source` (pepper.pl / scout / manual).

As built, deviating from the sketch above:
- The hub endpoint the scout calls is `LLMHUB_BASE_URL` (default
  `http://127.0.0.1:8800/v1`), so the CLI run works as a separate process and tests can
  point it at a mock. The source list path is `LLMHUB_SCOUT_SOURCES`.
- `scout_pages` is keyed by url with a content hash, not by url+day: the run re-fetches
  every source and the hash is what decides whether a page reaches the extractor.
- `scout_runs` stores the counts as `new_count`/`updated_count`/`skipped_count` (plain
  `new` reads as a keyword in enough SQL dialects to be worth avoiding); the API serves
  them as `new`/`updated`/`skipped`. It also carries `decisions`, `status` and `dry_run`.
- A search hit is turned into one page of titles/urls/snippets rather than fetching every
  result: the curator asks for the pages it actually needs through `needs_pages`.
- `GET api/scout/runs/{id}` also returns `error_details` (the messages behind the `errors`
  count), and `POST api/scout/run` takes `?dry_run=1`.
- `apply` never rewrites a promo whose status is `used`, and dedupes candidates against
  existing rows by url before deciding new vs update.

As built (v0.5.1), fixing two scheduled runs that both produced zero promos:
- **Root cause 1 - hardcoded `temperature: 0.0`.** `HubLLM.chat` sent it on every call; some
  routes pin temperature to a fixed value and answer 400 to anything else. `chat` now omits
  `temperature` unless a caller passes one explicitly.
- **Root cause 2 - one bad curator answer discarded the whole run.** The curator sometimes
  answers with prose plus a fenced JSON *array* instead of `{"decisions": [...]}`, or spells
  an action `add` instead of `new`, or sends `expires_at` with a time component. `curate.py`
  now recovers the decisions list from a fenced block or a bare array, coerces action
  synonyms and `expires_at`, and tolerates a missing `row` on `skip`. The run row also
  carries `invalid_reasons` (the pydantic error plus a text snippet) so a bad answer is
  visible in the report instead of just a count.
- **Root cause 3 - dead sources every run.** Three catalog `docs_url` entries 404'd or
  429'd on every fetch (novita, ollama, inception). novita and ollama got working URLs;
  inception kept its url but is marked `scout_polite` - fetched at most once every 7 days,
  via the `Source.polite` field honoured off the `scout_pages` cache timestamp. Pepper search
  pages get a 20s fetch timeout (`Source.timeout`) instead of the collector's default 10s.
- `runner.curate_prefer()` reads the `strong` alias's own prefer order when the registry
  defines one, instead of a hardcoded copy of it that drifts out of sync.
- See "Unsupported request params" under Gateway (v0.4) for the hub-level fix that stops a
  pinned-parameter 400 from failing every call to that model outright.

## CLI providers (v0.6) - agent CLIs as hub backends
Some free paths are not HTTP APIs but agent CLIs with a headless mode. First one: Google
Antigravity `agy` (`agy -p PROMPT --output-format json --model M [--effort low|medium|high]
[--json-schema FILE] [--sandbox] --print-timeout 4m`). Output:
`{conversation_id, status: "SUCCESS"|..., response, duration_seconds, num_turns,
usage:{input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}}`.
Observed overhead ~7k input tokens per call (agent system prompt); free, so irrelevant.

Registry: `kind: cli` provider with `command: agy`, optional `extra_args`, `concurrency`
(default 2), `timeout_s` (default 240), `workdir` (default `~/.llmhub/cli-work/<provider>`,
created empty; the process cwd, so the agent sees no real files), `env_passthrough`
(default HOME, PATH, LANG). Accounts carry no key (`api_key_env: null`); login state lives
in the CLI's own config. Models = ids from `agy models`, caps `[text, json, reasoning]`
(no vision, no tools: the agent's own tools are disabled by `--sandbox` + empty cwd).

Gateway mapping (`llmhub/cli_backend.py`): OpenAI chat request -> one prompt text: system
messages first, then `User:`/`Assistant:` turns, final line `Assistant:`; image parts ->
400 "cli providers take text only"; `response_format` json_schema -> temp file +
`--json-schema`; `json_object` -> a one-line instruction appended to the prompt;
`max_tokens` ignored (no flag); `temperature` ignored. Run with asyncio subprocess, never
`--dangerously-skip-permissions`, always `--sandbox`. Response -> `chat.completion` with
`usage` mapped (`prompt_tokens=input_tokens`, `completion_tokens=output_tokens`,
`completion_tokens_details.reasoning_tokens=thinking_tokens`). Streaming requests get a
single content chunk + `[DONE]` (no real streaming). Failure classification: exit code
!= 0 or `status != SUCCESS` -> parse stdout+stderr with the vendor error table (new rules
for antigravity: "quota", "rate limit", "limit reached", "try again later" -> quota, scope
default daily; "sign in", "auth", "login" -> auth error, no retry); other -> error +
cooldown. Usage rows and windows work as for HTTP models; limits unknown -> learned.

Catalog template `antigravity` (kind cli, command agy, aliases antigravity, agy, google
antigravity; docs https://antigravity.google/pricing; models = the 14 ids above; notes:
free plan, quota unpublished, data may be used by Google, public data only). Quick add
for a cli template needs no key: `POST api/accounts/quick {source: "antigravity"}` creates
the account and probes `agy -p "Say OK."`; `api/accounts/{p}/{a}/test` runs the same probe.
Aliases: `auto.prefer` gets antigravity/claude-opus-4-6-thinking after explabs fable and
before gemini; new alias `strong` (require text): antigravity/claude-opus-4-6-thinking,
antigravity/claude-sonnet-4-6, antigravity/gemini-3.1-pro-high, explabs/gpt-6-astra,
explabs/claude-fable-5.1, spread 3. `python -m llmhub check` shows cli accounts with
`KEY = cli` instead of yes/no.

As built, deviating from the sketch above:
- No `--effort` flag: the model ids already carry the effort (`-high`/`-medium`/`-low`), so
  the registry id is the whole selection. The argv is
  `<command> [extra_args] --output-format json --sandbox --print-timeout <timeout_s>s
  --model <id> [--json-schema <file>] -p <prompt>`, run through `create_subprocess_exec`
  (no shell) with `start_new_session=True` so a timeout kills the whole process group.
- `timeout_s` is both the CLI's own `--print-timeout` and the asyncio timeout around it.
- The `--json-schema` file is written into the workdir and removed after the run, not into
  the system temp dir: the sandboxed process can read its own cwd for certain.
- Cooldowns: the router raises an `error` classification straight to the caller, so the cli
  backend sets the cooldown itself before raising. An `auth` classification keeps the router
  default (cooldown, event, next candidate); the event names the command to run.
- `quota_scope_default: daily` decides the scope, but a model with `free: {}` has no daily
  window to park in yet, so the first exhaustion lands on the next hour (`earliest_reset`)
  until an observed daily cap adds the window. Unchanged behaviour, worth knowing.
- Provider block fields: `kind: cli` makes `base_url` optional and `command` required
  (config validation). `concurrency` is provider-level and defaults to 2 for cli; the router
  semaphore now reads `entry.concurrency` (model knob first, then provider, then the cli
  default). Quick add copies `extra_args`/`concurrency`/`timeout_s`/`workdir`/
  `env_passthrough` from the template when it sets them - the antigravity template sets none,
  so the written block is command-only and the defaults apply.
- `POST api/accounts/quick` takes an optional `api_key`. A non-cli provider without one is a
  422 `needs: ["api_key"]`; a cli account that already exists is left alone (nothing to
  rotate). `POST api/providers/{p}/discover` on a cli provider answers with `exit_code` and
  `command` instead of `http_status`.
- `api/status` accounts and `api/registry` carry `command` next to `base_url`.
- Live probe 2026-09-08 with the real `agy`, `--sandbox` and no permission prompt:
  gemini-3.8-flash-low 6.0 s / 15929 in / 2 out, claude-opus-4-6-thinking 5.4 s / 18325 in /
  15 out. The agent system prompt costs ~16-18k input tokens per call, more than the ~7k
  first measured; free, so still irrelevant to routing.

## Copilot CLI (v0.7) - an employer-licensed backend, opt-in only
GitHub Copilot CLI (`copilot`, npm `@github/copilot`) has a headless mode, so it fits the
`kind: cli` mechanism. It differs from antigravity in four ways that the backend must handle.

1. `--output-format json` emits **JSONL** (one object per line), not a single object: parse
   every line, keep the last object that carries a final assistant message, and sum usage.
2. Non-interactive mode **requires** `--allow-all-tools`. That would let a coding agent edit
   files and run shell commands, so every call is fenced: cwd = an empty per-provider workdir,
   no `--add-dir`, plus `--no-ask-user --disable-builtin-mcps --no-remote --no-remote-export
   --no-custom-instructions --no-auto-update --no-color --log-level none` and `--deny-tool`
   for the write/shell/fetch tool names. Never `--allow-all`.
3. Budget: usage is measured in AI credits (legacy accounts: premium requests) and the CLI
   exposes them only in its TUI. `--max-ai-credits N` is a per-session soft cap (minimum 30);
   the registry carries `max_ai_credits` per model and the backend passes it, so one runaway
   call cannot drain a monthly allowance.
4. Auth lives in the CLI's own store (`~/.copilot`) after `copilot login`. `env_passthrough`
   must NOT include `GITHUB_TOKEN` / `GH_TOKEN`: this machine's `gh` is signed in to a personal
   account, and an inherited token would silently pick the wrong identity.

Because the licence belongs to an employer, this provider is **opt-in per call**: template
`copilot` is registered outside `auto`, `vision`, `fast` and `strong`, and reachable only as
an explicit `copilot/<model>` or through its own alias `copilot` (spread 1). The registry
block carries `notes: "employer licence; org admins see usage and audit logs; do not route
private-project traffic here"`, and the dashboard shows that note on the account row.

As built, deviating from the sketch above:
- The per-CLI parts live in a `CliDialect` picked by `dialect_for(entry.template_id)`:
  `AntigravityDialect` (unchanged behaviour, and the fallback for any template without one)
  and `CopilotDialect`. A dialect owns four things: the argv, the stdout parser, what counts
  as success, and the wording of the auth event. Everything else in `call_cli` is shared.
- Copilot argv as built: `copilot -p <prompt> --output-format json --model <id>` then the
  fences `--allow-all-tools --no-ask-user --disable-builtin-mcps --no-remote
  --no-remote-export --no-custom-instructions --no-auto-update --no-color --log-level none`,
  then `--deny-tool shell --deny-tool write --deny-tool edit --deny-tool fetch` (repeated
  pairs: the flag is variadic and would otherwise swallow the next flag), then
  `--max-ai-credits <n>` when the model sets it, then `extra_args`. No `--print-timeout`
  (the CLI has no such flag; the asyncio timeout is the only one) and no `--sandbox`.
  `extra_args` goes last here, not first as for `agy`, so the documented prefix holds.
- `--max-ai-credits` is clamped up to the CLI's own minimum of 30: a smaller number in the
  registry would fail every call instead of capping it.
- Verified live that the fence works: with `--deny-tool shell` the model asked for the `bash`
  tool and the CLI refused it ("the shell tool is disabled by policy"). `shell` and `write`
  are the documented permission categories; `edit` and `fetch` are not documented and may be
  no-ops - they are kept because an unknown name costs nothing and the CLI does not reject it.
- The real 1.0.83 output is **not** any of the four shapes first assumed. Types are dotted
  event names and the payload sits under `data`: the answer is the last
  `{"type":"assistant.message","data":{"content":"...","phase":"final_answer"}}`; the prompt
  comes back as `{"type":"user.message","data":{"content": <the whole prompt>}}`; streaming
  arrives as `assistant.message_delta` with `data.deltaContent`; an intermediate
  `assistant.message` carries `content: ""` plus `toolRequests`. So the parser accepts text
  only from an object whose type's first segment is `assistant` (or from one with no type at
  all, which is what keeps the four assumed shapes working) - reading any object with a
  `content` key would have handed the caller its own prompt back.
- Usage is not in a `usage` block. `{"type":"result",...}` carries only
  `usage.premiumRequests` and durations; the token counts sit in
  `session.usage_checkpoint.data.promptCacheBreakState[].models.<id>.prompt_tokens` /
  `.cache_read`, and the cost in `data.totalNanoAiu` (nano-AI-units, /1e9 = credits) and
  `data.totalPremiumRequests`. All of these are running session totals, so the parser takes
  the highest value seen rather than summing - summing would multiply one call's cost by the
  number of checkpoints printed. There is no output-token count anywhere, so
  `completion_tokens` is always 0 for this vendor. `ai_credits` and `premium_requests` are
  handed back in `hub_cli`. Measured 2026-09-08 on one "Say OK." turn, direct: 13535 input
  tokens (1280 cached), 0.223542 credits, 1 premium request, 5.6 s; through the hub:
  15138 input tokens, 0.342427 credits, 1 premium request, 5.3 s. So ~14k tokens of agent
  system prompt per call, and a plain one-word answer already costs a premium request -
  unlike the free CLI, every call here is billable.
- Generic `usage`-block summing is kept alongside for the shapes above and for future
  versions; if a later CLI reports cumulative token totals there, that path will need the
  same max-not-sum treatment.
- Exit 0 with no answer in the stream is treated as a failure (`cli_exit_0`, cooldown), not
  as an empty completion: it means the stream shape moved, and a loud error is easier to
  diagnose than a blank answer.
- `response_format: json_schema` has no flag, so the schema is prepended to the prompt as one
  instruction line - the same mechanism as `json_object`, with the schema JSON on its own
  line. Nothing is written to the workdir for a copilot call.
- `env_deny` (new optional provider field, applied in `cli_env`) wins over `env_passthrough`
  and lists four names, not two: the CLI's own error text advertises `COPILOT_GITHUB_TOKEN`,
  `GH_TOKEN` and `GITHUB_TOKEN` as ways to authenticate, so all three are denied, plus
  `COPILOT_ALLOW_ALL` (the env form of `--allow-all`).
- `POST api/providers/copilot/discover` is a 400 from `discover_cli_models`, which refuses
  before resolving the binary: the dialect's `lists_models` is False, so the answer is about
  the CLI's feature set rather than about this machine's install.
- The model ids first sketched (`claude-sonnet-4-6`, `claude-opus-4-6`, `gpt-5.6`) do not
  exist. `copilot help config` lists the accepted ids under its `model` setting and they use
  dots: `claude-sonnet-4.6`, `claude-opus-5`, `gpt-5.6-sol`/`-terra`/`-luna`, and 20 more.
  The template registers that list plus `auto`, and `COPILOT_MODEL_IDS` names the help topic
  to re-read after a CLI update. A wrong id is cheap: exit 1 with
  `Error: Model "x" from --model flag is not available.` before any model call, so no credits
  are spent; the hub classifies it as a plain error with a cooldown. The `copilot` alias
  prefers auto, then claude-opus-5, then claude-sonnet-4.6.
- The auth event says "run `copilot login` with the licensed account", naming the configured
  command; a `copilot-not-entitled` classification gets its own wording about the licence
  instead. Real signed-out output (captured with an empty HOME): exit 1, empty stdout,
  stderr `Error: No authentication information found.` plus the three ways to authenticate.
  The `copilot-signed-out` rule matches it on "authentication".
- Alias isolation is by omission only. `resolve_pool` puts the whole registry in an alias's
  candidate list and `prefer` merely ranks it, so a copilot model remains the last fallback
  of `auto` if every model ahead of it is exhausted in the same request. Verified on the live
  registry that `auto` picks a preferred model and that copilot sits at the tail. A hard gate
  would need a provider-level opt-in flag in `resolve_pool`; not built.
- The provider block's `notes` is written to the registry but nothing renders it: `api/status`
  account rows carry no `notes` field (the dashboard was out of scope). The per-model
  `notes` - "a Copilot seat that is not the owner's to spend freely; the seat holder sees
  usage and audit logs" - is what the dashboard already shows on each model row.

## Opt-in-only entries (v0.8)
An alias pool is the whole registry and `prefer` only ranks it, so an entry nobody preferred
is still tried once everything ahead of it is exhausted. For metered or licensed capacity that
is a silent leak: measured on the live registry, `auto` had 44 candidates of which 27 were
`copilot/*`, the first at position 17.

`opt_in_only: true` on a provider block (or on a single model, which wins) removes the entry
from every alias pool. It stays reachable two ways: an explicit `provider/model_id` request,
or an alias whose own `prefer` list names it. A rejected entry appears in `selection.rejected`
with reason `opt_in_only` and the alias name as detail, so the 429 body says why. `X-Hub-Prefer`
does not grant eligibility - only the alias definition or an explicit model does.

The copilot provider carries it, and the `copilot` alias names six of its models in `prefer`.
Verified after the change: auto/vision/fast/strong/local return zero `copilot/*` candidates,
the `copilot` alias returns its six, and `copilot/claude-haiku-4.5` still routes.

## Request size awareness (v0.9)
Measured 2026-09-08: 255 of 257 registered models declare no `context`, so the router had no
way to skip a candidate that physically cannot take the request. Result: `groq/openai/gpt-oss-120b`
(free tier, 8000 tokens per minute) was picked for an ~11k-token transcript and returned 413
eight times, and the caller saw the vendor error instead of a different model. A client cannot
express this either - `X-Hub-Require` filters capabilities, not size.

**Declared ceilings.** `ModelDef` gains `max_request_tokens` and `max_output_tokens`. The
effective input ceiling for a candidate is the smallest of: `max_request_tokens`, `context`
minus the requested output, and any observed ceiling (below). Absent everywhere = unknown,
and an unknown ceiling never rejects: this must not turn into a static fallback that hides
capacity. Seed the catalog from what is already documented per provider: groq 8000 (free tier
TPM), cerebras 30000 (uncached TPM), openrouter free rows and the rest stay unset until a
vendor states a number.

**Observed ceilings.** A 413 or a "request too large" / "maximum context length" / "tokens per
minute (TPM): Limit N, Requested M" body records `max_request_tokens = N` for that
(account, model) in `observed_request_caps`, the same way quota errors record observed windows.
The next selection skips that candidate for a request of that size instead of spending another
round trip. `DELETE api/models/{key}/observed` clears these too.

**Rejection.** A candidate too small for the request is rejected with reason `too_large`,
detail = the ceiling, plus `requested_in`. When no candidate fits, the hub answers 413 (not
429: waiting will not help) with `error.max_request_tokens` = the largest ceiling any free
candidate had, so the caller knows what size would go through, and header `X-Hub-Max-Request`.

**Truncated structured output.** A response with `finish_reason: length` is a normal outcome
for a prose request, but a broken one when the caller asked for `response_format` json: the
JSON never closes. In that case the attempt counts as failed, the router moves to the next
candidate, and an event `truncated` is recorded with the model. Nine such calls were logged
against reasoning models that spend the output budget on thinking. Repeat offenders are visible
in Events; demoting them stays a manual registry edit for now.

## Promo identity (v0.10)
Measured 2026-09-09: 81 promo rows held 15 duplicate groups - `OpenRouter :free` three times
(same url), `Google Gemini API` three times, `Cloudflare Workers AI` three times - and 16 rows
whose note started with `UPDATE:` had been filed as new rows. Cause: dedupe lived in the
promo-hunt skill, which matched on url only, and `POST api/promos` accepted whatever it was
sent. Dedupe that lives in a writer is dedupe only that writer does.

**Identity.** `promo_identity.identify(provider, url, base_url, store, registry)` answers with
a key, the stage that produced it, and why. Stages, in order: a learned alias; the provider
catalog via `accounts.resolve_source` (the same resolver quick add uses); a provider already
in `providers.yaml`, by id or by the host its `base_url` calls; the bare host of the row; the
provider name slugified. Hosts that carry many vendors (github.com, huggingface.co,
ollama.com, pepper.pl) and loopback addresses never name a vendor - they would file every
project hosted there under one key.

**Learning.** Every answer but the last writes `name:<provider>` and `host:<domain>` into
`promo_aliases`, first writer wins. The next post under either spelling short-circuits at
stage one. Quick add teaches the same table when it registers a key: that is the one moment
the hub knows for certain who a vendor is. The fallback stage never teaches - it would freeze
a guess.

**Merging.** `Store.upsert_promo` writes one sighting. A second sighting of an identity
appends `[<date> <source>] <note>` (the `UPDATE:` prefix stripped), fills in fields the row
lacked, bumps `updates_count`/`updated_at`, and keeps `found_at`, the url and the furthest
status the row ever reached (`used` > `expired` > `known` > `new`). A `used` row is frozen
apart from its note. `POST api/promos` answers 201 for a new vendor and 200 with
`merged_into` for a known one, and marks the row `used` when the registry already holds an
account for that identity.

**Migration.** `python -m llmhub promos-dedupe [--dry-run]` backs the table up to
`~/.llmhub/backups/`, walks the rows oldest first, and folds each group onto its oldest row
with the notes stamped with the day they were found. Run 2026-09-09: 81 -> 56 rows, 15 groups
merged, 123 aliases learned, 0 duplicate groups left.

## v0.11 queue hygiene and fair share
Measured 2026-09-09 on the live hub: 133 jobs of one app sat in `queued`/`waiting_quota` for
36-48 h on gemini flash models, each asking for ~6000 output tokens while the hourly windows
on that account open with 74-346; 3 jobs had been `running` for 4.5 h with no `served_by`
after the client that posted them died; jobs on openrouter free rows, which report no window
at all, never got retried; and the unfiltered `GET /jobs` returned the oldest 200 rows, so
the live work was invisible while the counter said "queue 246". Nothing in the hub ended any
of it - the queue had no way to give up.

**Deadline.** `POST /jobs` takes `ttl_s` (default `LLMHUB_JOB_TTL_S` = 6 h, clamped to
`LLMHUB_JOB_TTL_MAX_S` = 36 h) and stores `expires_at`. Past it, a job in `queued` or
`waiting_quota` becomes `expired` - a terminal state - with `error` = JSON
`{code: "expired", reason, message}`. The reason is what the router says at that moment:
`no_window` (candidates exist but all are out of quota), `window_too_small`, `provider_down`
(no key, disabled, unknown model), or `no_worker` (a candidate had room; the pool never got
to this job). Rows written before this version carry no `expires_at`; their deadline is read
as `created_at + ttl_s`, so no data migration is needed.

**Feasibility.** `QuotaTracker.window_limit(entry)` is the widest a fresh window ever opens
for that (account, model): the tightest declared-or-observed out/total limit across its
windows. Not the remaining amount - a request larger than the limit can never run, whatever
the clock does. `POST /jobs` with an explicit `max_tokens` over the best candidate's limit is
refused 422 `window_too_small` with `error.window_limit`; the same check runs on every
housekeeping pass, so a job already queued when a 429 reveals a smaller window leaves as
`expired` instead of waiting out its ttl. A request without `max_tokens` is never rejected:
an assumed default must not refuse work a vendor might have served, and an unknown limit
(any candidate with no window data at all) never rejects either.

**Lease.** A job enters `running` with `lease_until` = request read timeout + 60 s
(`LLMHUB_JOB_LEASE_MARGIN_S`), renewed by the housekeeping pass for as long as this process
holds the task. A `running` row whose lease has passed with no live task behind it goes back
to `queued` with `lease_attempts + 1`, and fails with `error.code = "lease_lost"` on the
third loss. Restart still requeues orphans outright: a new process holds no lease on them.

**Backoff for windowless models.** A model that declares no window and has none observed
never reports a reset, so parking the job on `next_window_at` waits for an event that is not
coming. Such a job gets `next_attempt_at = now + 1, 5, 15, 30 min, then 30 min`, and
`claimable_jobs` skips it until then. It still expires at its ttl.

**Queue cap and retention.** More than `LLMHUB_QUEUE_MAX_PER_APP` (default 200) live jobs for
one app answers 429 `queue_full` with `Retry-After`, per app so one dead client cannot fill
the hub. Terminal rows are deleted after `LLMHUB_JOB_RETENTION_DAYS` (default 7).

**Listing.** `GET /jobs` and `GET api/jobs` default to the live states newest first,
`?state=all` for history, `?state=<one>` for one state. `api/status.queue` adds `live`,
`oldest_queued_at`, `expired_last_24h`, `workers` and `apps`; the dashboard counter reads
`live`, so it and the default listing are the same number.

**Fair share.** The static per-app cap is gone. Each pass counts the apps with runnable work
(queued and claimable now, or running; paused apps excluded) and gives each
`cap = max(LLMHUB_JOBS_PER_APP_MIN, workers // active_apps)`, floored at 1 so more apps than
workers still take turns. `LLMHUB_JOBS_PER_APP` stays as an optional hard ceiling (default 0
= none, was 3), which is the only thing that stops one app from using an idle pool. Nothing
is preempted: a freed worker goes to the app holding the fewest running jobs, ties to the app
whose last dispatch was earliest, and FIFO within that app. An event `queue` records the
share whenever the active set changes - once per change, not once per dispatch.

**One pass.** Housekeeping and dispatch live in `JobQueue.tick`: dispatch every poll (2 s), so
a free worker never waits, housekeeping on the minute and the purge every ten, because
rereading every waiting job at poll rate costs more than it finds.

## v0.12 promo rejection
The watchlist had no way to say no. A lead that is useless for a free-only gateway - a trial
that needs a card, credits that expire in days, IDE-only access with no API, a signup that
demands a business email, a region the owner cannot use, an aggregator of models the hub
already routes - could only be left as `new` forever or mislabelled `expired`, and nothing
stopped the scout or the promo-hunt skill from posting the same kind of offer the next week.
The rejection itself was worthless; the reason behind it was the part worth keeping.

**Verdict.** `promos.status` gains `rejected`, with `rejected_reason` and `rejected_at` on the
row. In `PROMO_STATUS_RANK` it sits above `new`, `known` and `expired` and below `used`: a
re-post of a rejected offer merges into the row as another update (note appended,
`updates_count` bumped) and stays rejected, so the owner can see the same thing keeps coming
back, while a vendor that later gets a key becomes `used` like any other.

**Memory.** `promo_rejections` holds one row per rejection event (identity, provider, reason,
url, note snippet, timestamp). A row can be rejected, reopened and rejected again;
`Store.reopen_promo` clears the verdict on the row back to `known` and keeps every event. The
table, not the promo row, is what the writers read - a rejection outlives the lead it came from.

**Reason required.** `POST api/promos/{id}/reject {reason}` and `PATCH` with
`status: rejected` both refuse a reason under 8 characters with 422, and the message says why:
the reason becomes a standing rule for the scout and the promo-hunt skill. `POST
api/promos/{id}/reopen` is the other direction. `GET api/promos/rejections` serves the history
newest first.

**Reaching the writers.** The curate prompt carries the rejections as
`- <provider>: <reason>` (30 most recent) under a block that names them as the owner's rules,
and the system prompt spells out the rule: an offer from a rejected vendor, or one that fails
for the same reason as a listed rejection, is `skip_rejected` - a skip counted apart in the
run report (`- skipped as rejected: N`) so a curator that keeps hitting the same wall is
visible. The promo-hunt skill fetches the same endpoint before searching and reports what it
dropped instead of posting it.

## v0.13 live view
The usage table answers "which model did this app use lately"; nothing answered "which app is on
which model right now". The router's semaphores knew a pair was busy, not who was waiting on it.

**Registry, not a table.** `Router` keeps `dict[call_id, InFlight]` in memory: app, model key,
account, what was requested, kind (`sync` | `stream` | `job`), job id, attempt, monotonic start
plus a wall stamp, input estimate. Written inside the semaphore guard right before the vendor
call, moved (never duplicated) when a fallback takes the next candidate, dropped in a `finally`.
`started_at` survives retries, so elapsed is the age of the caller's request. Nothing persists: a
restart has nothing in flight by definition.

**Streams outlive the run.** `router.run` returns while an SSE body is still being served - the
same reason the semaphore is released early - so a stream's entry is held past the return and
released by `stream_body`, where the stream's usage row is closed. A CLI stream is already
complete and is released when the run returns. A generator dropped without being closed would
leak an entry, so a read prunes anything older than an hour.

**Reads.** `Router.in_flight()` (elapsed computed at read time) and `in_flight_count(model_key)`.
`status.live_rows(hub, window_min=15)` joins them with one grouped query over `usage`
(`idx_usage_ts` covers it) into a row per app: `in_flight` and `recent` (per model, accounts
summed). `model_rows` carries the same window as `apps_15m` plus `in_flight` per pair.

**Strip above the tabs.** Not inside the Models tab: "who is calling what" belongs to no single
tab and stays visible while the owner works in Usage, Queue or Events. In-flight chips are
highlighted and tick client-side from the elapsed the server measured; recent-only chips are
muted with a call count. Poll: 2 s while the document is visible, 10 s once nothing has been in
flight for a minute, and the strip hides when the window is empty.

## v0.14 router failure handling
Measured 2026-09-10 over 36 h on one app: 9899 vendor attempts, 2283 ok. `classify()` returned
`error` for any 4xx nothing else matched, and `Router.run` re-raised on `error`, so the vendor's
status and body went straight to the client and the remaining candidates were never tried -
1027 groq 404s reached callers as HTTP 404 on the hub's own wire. `RETRY_DELAYS = (5, 15, 45)`
applied to every `retry`, daily quotas and dead 5xx routes included: 65 s of sleep per candidate
and nothing capping the run.

**New kinds.** `not_found`: 404, codes `model_not_found` / `InvalidEndpointOrModel.NotFound` /
`unavailable_route`, "does not exist or you do not have access" and friends, plus 402 /
`payment_required` - a paid route on a free key is equally unusable. `unavailable`: a 4xx whose
body says the provider's own upstream failed (`error.type == "server_error"`, "model is
unavailable", "upstream request failed", "overloaded"). A 5xx stays `retry`. Both are checked
after the provider code rules, so a vendor with its own word for "overloaded" keeps it.

**Parking.** Table `unavailable(account, model, kind, code, reason, until_ts, ts)`, same shape
and expiry-on-read semantics as `exhausted`. `not_found` -> 7 days (`LLMHUB_NOT_FOUND_TTL_S`),
`unavailable` -> 10 minutes (`LLMHUB_UNAVAILABLE_TTL_S`). `select` rejects with reason
`unavailable`; `entry_status` reports `down` with `"<kind>: <code> until <iso>"`, so `/v1/models`
hides the pair like any other `down` row. A reload does not clear it; a successful call on a
pair this process parked does, and `POST api/models/{key}/forgive` clears it on demand.

**Retry policy.** `RETRY_DELAYS = (2.0,)`: one retry per candidate. The pool is wide, so a
second vendor answers sooner than a third wait on the first. `Classification.retry_after_s` is
parsed from the body ("try again in 27m44.063s") and from `Retry-After`; a wait under
`RETRY_WAIT_MAX_S` (8 s) is slept, a longer one becomes a cooldown of exactly that length and
the run moves on. A quota hit uses it as an upper bound on the park: a rolling day is not the
calendar day the hub tracks.

**Rate limits with a window.** "Rate limit reached ... on tokens per day (TPD)" is a quota
window the hub keeps books on, so it exhausts `daily` instead of retrying four times; a
per-minute (TPM/RPM/OTPM) limit names no such window and stays `retry`. "Request too large ...
on output tokens per minute (OTPM): Limit N" is `too_large` on the output axis and records
`max_out_tokens` - the caller fixes that one with `max_tokens`, not with a shorter prompt.

**Budget.** `run(..., budget_s)`: before starting the next candidate or sleeping a retry, a run
past its budget stops with `AllCandidatesFailed(budget_exhausted=True)`. Gateway passes
`LLMHUB_RUN_BUDGET_S` (90 s, against a 120 s client read timeout) for sync and stream; jobs pass
None and walk the whole pool. The first candidate always runs.

**400 vs 502.** Every attempt `error` and the last one carrying a real vendor HTTP status ->
400 with that vendor body: every vendor examined the request and rejected it, and that is the
client's to fix. Anything else -> 502 `upstream_failure` with `attempts`, the last vendor body
under `last`, `Retry-After: 30` and `budget_exhausted` when set. A local CLI backend that
crashed before reaching a vendor also lands as `error`, but nobody looked at the request there,
hence the status-code condition. Three candidates from three distinct providers all answering
`error` stop the run early: burning the rest of the pool changes nothing.

**Why `error` no longer aborts.** The vendor's opinion of the request is not the truth for the
next vendor - routes differ in accepted params, roles, schema strictness and model ids. One
`error` is one data point; the pool decides.

## v0.15 models table
The Models table had one five-mode sort `<select>` and a single "only non-ok" checkbox; the
owner wanted to answer "what is busy, what is broken, what is left" without reasoning about a
dropdown. Rebuilt on the Promos tab's own patterns instead of a new one.

**Sort.** Every header is `data-sort-key` + `data-sort-active`/`data-sort-dir`/`aria-sort`, same
markup as Promos, but a third click returns to registry order (Promos only toggles asc/desc) -
`{key, dir} | null` in `llmhub.modelsSort`, an old mode string migrates to `null` silently.
Count-like columns (Today, Out tokens, Latency, Apps' in-flight count) start at desc since a
bigger number is the interesting one; everything else starts at asc. Hourly/Daily/Monthly sort
by remaining fraction so a 90%-full hourly window ranks like a 90%-full monthly one; a model
with no window, no latency measurement or no error ever always sorts last, in either direction.

**Filters.** Status and caps chips, a provider select and free text are all derived from
`state.status.models` - a new status or cap value shows up as a filter with no code change.
Client side over the same array the table renders, `llmhub.modelsFilters` in localStorage.

**The live join is per cell, not a re-render.** `api/live` (2 s poll) is joined against
`state.status.models` (10 s poll) into `state.liveModelMap`, keyed by `account + "|" + key` -
`key` because a live call names the full `provider/model` id, not the bare model name in
`m.model`. Re-rendering the whole table on the fast poll would fight the slow one over row
identity for no reason: only the Apps cell and the row's `row-live` class are touched per tick,
by walking `state.modelRowNodes` built on the last full render. The "in use now" filter and the
`apps` sort key read the same map, so they are only as fresh as the last full render too.

## v0.16 selection constraints and app bans
A client with long inputs (transcript extraction, p99 17878 in tokens plus a 4000-token output
reservation) sent requirements the registry could not express: the model must hold the whole
call, no thinking models, nothing slower than about 30 s, and it must quote the source
verbatim. The quota half already worked - `select` checks `has_room(est_in, est_out)` with
`est_out = max_tokens`. Three things were missing.

**Requirement vs preference.** A requirement filters the pool, a preference only orders it.
`min_context` is a requirement: an entry whose `context` is below it is rejected `context_small`
(detail = the known context). `avoid` (caps) and `max_latency_ms` are preferences and can never
empty the pool - a slow or thinking model stays as a fallback behind everything else, because
an answer from the wrong shape of model beats no answer at all.

**Unknown context is rejected, not assumed.** `min_context` with `entry.model.context is None`
rejects `context_unknown`. The caller asked for a guarantee and an undeclared context is not
one; assuming "probably big enough" is the silent-capacity-cutoff failure the request size
work already ruled out in the other direction. The rejection is also the measurement: the
count of `context_unknown` rows in the 429 body tells the owner how much of the registry has
no context written down, which is the data model discovery fills in.

**Sort key.** `(busy, header_rank, penalty, prefer_rank, order)`. `penalty` is 1 for an entry
carrying an avoided cap, +1 for one whose measured latency (`avg_latency_ms` over the recent
usage tail, one grouped query per selection rather than one per candidate) is above
`max_latency_ms`; an unmeasured entry is never penalised. `X-Hub-Prefer` sits above the
penalty - an explicit pick is explicit - and the alias `prefer` list sits below it, so an
avoided model the alias names still sorts behind every candidate that avoids nothing.
`apply_spread` is unchanged: it takes the first `spread` of the sorted list.

**Constraints combine to the stricter side.** The alias is the owner's standing description of
the job, the headers are one caller's; neither loosens the other, so `min_context` is the max
of the two, `max_latency_ms` the min, and `avoid` the union. `Selection.constraints` carries
the result and the 429 body echoes it under `error.constraints`, so a client can see what it
actually asked for next to the rows that answer.

**Bans are per app, with a mandatory reason.** Whether an answer quoted its input verbatim or
held to a schema is a quality the hub cannot measure and the app can. `app_model_bans`
(app, model, reason, created_at) lets the app record its own verdict: `select(app=...)` rejects
the pair `app_banned` with that reason as the detail, for sync calls, streams and jobs alike.
The reason is required (8 chars, same rule as a promo rejection) because it is the only record
of why capacity is off the table - a ban with no reason is indistinguishable from a mistake six
months later. It is scoped to the app, not global: one app's quality bar is not another's, and
`POST api/models/{key}/disable` already exists for the owner's global verdict.

## v0.17 context discovery

`model.context` sat unset on almost every registered model: `Entry.input_ceiling` and
`min_context` (v0.16) both read it, but discovery threw the number away even where the
vendor's own listing named it. Most `/models` responses carry it under one of a handful of
field names, so this teaches discovery to keep it instead of inventing anything.

**`parse_model_rows`** replaces the id-only parsing at the source: one row is
`{"id": ..., "context": int | None}`, matched against `context_length` (OpenRouter, Cohere
compat, DeepInfra), `context_window` (groq), `inputTokenLimit` (gemini), `max_context_length`
(Mistral), `context_size` (Novita), `max_model_len` (vLLM-style), `max_input_tokens`, a
Cloudflare `properties[]` row (`property_id: "context_window"`), OpenRouter's nested
`top_provider.context_length`, and `limits.context`/`limits.input` - first match wins, 0/None
is treated as "no context", never a guess. `parse_model_ids` is now a thin wrapper over it, so
every existing caller is unaffected.

**A new function, not a wider `discover_models`.** `discover_models` is called from two places
in `api.py` that only ever wanted ids; widening its return to a 3-tuple would have broken both
for no benefit to either. `discover_model_rows` shares the same request-building code
(`_fetch_listing`) and applies the same id normalization and non-chat filtering, but returns
rows with context attached - it is the one to call anywhere the context window matters.
`model_specs_from_rows` turns those rows into specs `RegistryWriter.add_account`/`add_models`
can take, setting `context` only when the row had one. Neither api.py registration path (quick
add, discover-on-account) is wired to it yet - both still build specs from bare ids and would
need to switch to `discover_model_rows` first.

**The owner's number always wins.** `merge_model` now skips the `context` field entirely when
the registered model already has one, so a `refresh-context` run (or a rediscovery through
`add_models`) can never clobber a value the owner set or corrected by hand - only a model with
no context yet ever gets one written.

**`python -m llmhub refresh-context [--dry-run] [--provider NAME]`.** One discovery pass per
provider that is not `kind: cli` and has at least one account with its key present (or that
needs none): fetches the listing once, matches rows to the provider's registered models by id,
and reports provider / models / filled / already_set / unknown / status as one plain-text line
each - `status` carries the vendor's own failure (`http_401`, `no_models`, `no_account_key`, a
timeout) rather than aborting the whole run, and the process exits 0 either way so a few dead
providers do not hide the ones that worked. `--dry-run` only prints the table. A real run backs
`providers.yaml` up to `~/.llmhub/backups/providers-<ts>.yaml` (same directory `promos-dedupe`
uses), writes only the fields that were actually missing, and prints the reload endpoint
(`POST api/registry/reload`) as the last line.

## v0.18 quota learning - metric- and window-aware observed caps

`record_observed` knew one metric (`out_tokens`) and one source (what this account spent
before the 429). On a tier metered in requests that arithmetic is nonsense: eight gemini
models ended up with hourly "caps" of 97 to 1595 out tokens - the tokens spent when a
**requests per day** quota ran out - and every later request reserving 4000 out tokens was
rejected before it left the hub. groq's `on tokens per day (TPD): Limit 200000, Used 195998`
was stored as a daily cap of 32711 for the same reason: the vendor's own number was read
past.

**A cap now has three sources, in this order.** 1) The vendor states its ceiling - groq's
`Limit N`, google's `quotaValue` - and that number is recorded as it stands, for the metric
and window it was stated in. 2) The body names `requests` and no number: the count of calls
served in the window is the cap, because the vendor counted the same calls we did. 3) The
body names no metric but talks about tokens: the old used-so-far heuristic, on `out_tokens`
only. A body that names neither teaches nothing - that is the branch the bogus rows came
from. `Classification.quota_detail` (`metric`, `window`, `limit`, `used`) carries this out of
`vendor_errors`, parsed from groq's one-line shape, google's `details[].violations[]`,
google's prose, and finally the window markers with a bare `Limit N`.

**Per-minute is pacing, not exhaustion.** A quota body whose window is a minute comes back as
`retry` with `retry_after_s` (google's `retryDelay` counts as such a delay now), so the router
sleeps or cools the pair down for the stated seconds. Only hourly, daily and monthly park a
pair. The scope of a quota hit is resolved from `quota_detail.window` first, then the prose
markers, then the rule, then the provider template default - so a gemini `quotaId` naming
`PerDay` overrides the template's `hourly` guess instead of losing to it.

**Windows can cap requests.** `QuotaWindow.requests`, `window_usage.requests` (rows whose
status is not `quota`: a call the vendor refused for quota was never charged to that quota),
`has_room` plans one more request, and `room()` reports `remaining_requests` on its own axis -
a request cap says how many calls fit, never how big one may be, so it never lands in
`remaining_out`. The 429 body, the selection rejection row and `X-Hub-Remaining-Requests`
carry it; the binding window named in a rejection is now the axis that actually blocked.

## v0.19 orphaned calls
Measured on the live hub: 61 calls in flight for one client that had been stopped for hours,
ages 83 s to 3516 s, 41 of them queued on a model that answers `500` in under three seconds.
Every antigravity call that day ended in `cli_timeout` after exactly 240 s, with nine or ten
`agy` children running at once.

**The semaphore queue was unbounded.** `Router.run` checked the budget at the top of the
candidate loop, before `async with guard`, and the acquire had no timeout: dozens of runs whose
clients had timed out long ago queued for two CLI slots (61 x 240 s / 2 is about two hours),
each eventually burning a 240 s call nobody would read. The in-flight entry was written after
the acquire, so a queued run was displayed under the model of its previous attempt.

**The budget now bounds waits and attempts, not just candidates.** The acquire runs under
`asyncio.wait_for` with half of what is left of the budget (`SLOT_WAIT_SHARE`) - never all of
it, because the rest of the pool is worth more than a longer wait on a pair that is already
busy. A timeout notes the attempt `busy` / `slot_wait` and moves on. A pair with
`concurrency * 2` runs already queued (`SLOT_QUEUE_FACTOR`) is skipped without waiting at all
(`slot_queue_full`). After the acquire the budget is re-checked, so a slot that came free at
the deadline is handed on rather than spent. Each attempt runs under
`min(remaining, vendor bound) + ATTEMPT_GRACE_S` (15 s): the grace is what keeps the hub's
cancel from racing the backend's own timeout, which classifies its failure better than we can.
A timed-out attempt is `retry` / `attempt_timeout`, cools the candidate down and ends the run -
there is no budget left for another. Jobs pass `budget_s=None` and keep unbounded waits: a job
holds no socket, nobody is timing it out, and waiting for a free slot is exactly what it should
do. Its vendor calls stay bounded by the vendor timeouts as before.

**Cancellation reaches the vendor.** uvicorn does not cancel a handler when the client hangs
up, so `chat_completions` runs `execute_chat` as a task and polls `request.is_disconnected()`
every second alongside it; a gone client cancels the run task. For HTTP the cancelled `await`
closes the httpx request; `run_cli` catches `CancelledError`, kills the process group and
re-raises, so no orphaned agent keeps burning a licence. Inside `Router.run` the cancellation
releases the slot and the registry entry, notes the attempt `abandoned` with no cooldown and no
fallback event - the candidate did nothing wrong - and writes one usage row with status
`abandoned` plus an event (`client_gone`, or `cancel` when the owner did it). `abandoned` is
excluded from every error count in `store.py`: a model must not look broken because a client
left. The handler answers 499 `client_gone` to a socket that is not there, or 503
`cancelled_by_owner` with `X-Hub-Attempts` to a caller who is.

**The registry says which half of a call it is.** The entry is created on the first line of the
run, before any acquire, with `state: "waiting"` and the target model, and flips to `"running"`
inside the guard right before the vendor call. `in_flight_count` counts running only (the
`busy()` sort key still asks the semaphore about slots, which is the right question there);
`waiting_for(entry)` backs the bounded queue. `api/live` rows carry `call_id` and `state`, the
strip and the Models Apps cell mute a waiting chip and label it. The 3600 s prune stays as a
safety net but now logs a warning: with bounded waits and the disconnect watcher it should
never fire again.

**Kill switch.** `POST api/live/{call_id}/cancel`, and `POST api/live/cancel` with
`{"app": ...}` or `{"all": true}`. The router keeps `call_id -> asyncio.Task` (a handler's run
task, or a job worker's) and cancels it; everything else is the cancellation path above. A job
cancelled this way, or through `DELETE /jobs/{id}` while running, ends `cancelled` with error
code `cancelled_by_owner` - the queue tells an owner's cancellation from a worker that died,
because the second one is a lost lease and gets requeued. Every live chip has an x; a row with
more than one call in flight also has a "kill N" button behind one confirm. A stream past its
own run is the one thing the switch cannot end: `router.run` has returned, its task is finished
and the body belongs to Starlette, so that chip's x answers 404 and the strip says so.

**Dead backends park themselves.** Three consecutive `cli_timeout` or `attempt_timeout` on one
pair (`TIMEOUT_STRIKES`) park it as `unavailable` with code `timeouts` for the unavailable TTL
and an event of that kind; any answer resets the count. A backend that never answers looks
healthy to everything else in the pool, so it keeps being handed callers - three deadlines is
enough evidence.
