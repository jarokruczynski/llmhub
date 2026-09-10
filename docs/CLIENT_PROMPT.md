# llmhub client prompt

Paste this block into any Claude Code session (or any coding agent) on this Mac so it knows
how to call the local LLM gateway instead of guessing at an API.

---

## 1. What this is

`llmhub` is a local, free-only LLM gateway running on this Mac. Base URL on this machine:
`http://127.0.0.1:8800/v1`. From another device on the LAN: `http://mac.local/hub/v1` (Caddy
strips the `/hub` prefix); LAN calls need `Authorization: Bearer $LLMHUB_TOKEN` because only
loopback is open, everything else needs the token. The wire format is OpenAI Chat Completions
(`POST /v1/chat/completions`, same body and response shape as `openai` SDK expects). Only free
vendor endpoints are ever used to serve a request unless you explicitly opt into paid (see
below, and do not do that without the owner's say-so). You do not pick a vendor or model
directly in normal use: you send `model: "auto"` (or `"vision"`) and the hub picks a free
candidate that has quota left, rotating across providers and accounts on failure or exhaustion.

## 2. Sync call

curl:

```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-App: myapp' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say OK."}]}'
```

Python (`openai` SDK):

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8800/v1",
    api_key="hub",  # placeholder value; loopback needs no real key
    default_headers={"X-Hub-App": "myapp"},
)

resp = client.chat.completions.create(
    model="auto",  # or "vision", or an explicit "provider/model_id"
    messages=[{"role": "user", "content": "Say OK."}],
)
print(resp.choices[0].message.content)
```

`model` is one of:
- `"auto"` - text-first alias, tries free candidates in registry preference order.
- `"vision"` - alias that requires vision capability.
- `"fast"`, `"strong"`, `"local"` - aliases for latency, quality and on-machine only.
- `"provider/model_id"` explicit key (e.g. `"zai/glm-4.5-flash"`) - only when you have a
  specific reason to pin a vendor; prefer the aliases otherwise.

Opt-in-only providers: some entries are metered or run on someone's licence, so they are
removed from every alias pool. `copilot` is one - it runs on a Copilot seat that is not the
owner's to spend freely, hence opt-in only: the seat holder sees usage, and each call costs
about a third of a premium request however short the prompt. Nothing routes there by
accident: `auto`, `vision`, `fast`, `strong` and `local` never return a copilot candidate,
and `X-Hub-Prefer` does not open it either. To use it on purpose, name it in `model`:
- `"copilot"` - its own alias, tries copilot models first and then falls back to free ones,
  so a busy or signed-out licence silently gives you a free model instead.
- `"copilot/claude-opus-5"` (or another id) - exactly that model, no fallback to any other
  provider. Use this when the licence model is the point of the call.
Do not send private-project or personal data through `copilot`. Use it for work that
belongs to the licence holder.

Request headers:
- `X-Hub-App: <name>` - required on every `/v1/chat/completions` call. Missing it is a 400.
  Use your project name (`my-app`, `batch-ocr`, ...), not a generic value.
- `X-Hub-Require: vision,json` - capability filter on top of the alias/model (comma list).
  Use this instead of hardcoding a vision-capable model id.
- `X-Hub-Prefer: provider/model` - soft preference; moves that candidate first in the queue,
  does not exclude others.
- `X-Hub-Min-Context: 24000` - requirement: only models whose declared context is known and
  at least this wide. Size it as your worst-case input plus the output you reserve
  (`max_tokens`). A model whose context nobody wrote down is rejected too, on purpose: it
  cannot give the guarantee you asked for.
- `X-Hub-Avoid: reasoning` - comma list of caps to sort last. A preference, not a filter: an
  avoided model is still tried when nothing else is left.
- `X-Hub-Max-Latency-Ms: 30000` - the same kind of preference, against the model's measured
  average latency. A model with no measurement yet is never pushed down for it.
- `X-Hub-Allow-Paid: 1` - the only way a non-free model becomes eligible. Never set this
  unless the owner explicitly says to.

An alias can carry `min_context`, `avoid` and `max_latency_ms` of its own - `extract` is the
one for long inputs and strict JSON. Alias and header combine to the stricter side (context
floor: the higher; latency ceiling: the lower; avoid: the union), and the 429 body echoes the
result under `error.constraints`.

Response headers to read (useful for logging / debugging which vendor actually answered):
- `X-Hub-Model` - the `provider/model_id` that actually served the call.
- `X-Hub-Account` - which account (of possibly several per provider) served it.
- `X-Hub-Attempts` - comma list of `model:status` for every candidate tried, in order
  (e.g. `explabs/gpt-6-astra:quota,explabs/claude-fable-5.1:ok`).
- `X-Hub-Remaining-Out` - out tokens left for the serving (account, model) in its binding
  window after this call. Best effort: absent when the model declares no out/total cap, and
  on streamed responses (usage is only known after the headers are sent).

Banning a model for your app: `X-Hub-Model` names what served the call, so when your own
quality check fails in a way that is the model's fault - it paraphrased instead of quoting,
it ignored the schema again - `POST /api/apps/{app}/bans {"model": "...", "reason": "..."}`
and the hub never routes your app there again. The reason is mandatory (422 without one) and
should say what the model did, not that it was bad. Do not ban for a transient error, a
timeout or a quota hit: the router already handles those, and a ban is permanent until
someone calls `DELETE /api/apps/{app}/bans/{model}`.

Streaming: set `"stream": true` in the body as usual; the hub passes SSE through unchanged.
Token usage on a stream is read from the final `usage` chunk when the vendor sends one
(request `stream_options: {"include_usage": true}` if you build the request by hand; the
`openai` SDK's streaming helpers already do this).

Vision: send the image exactly as OpenAI's wire format, a `data:` URL inside an
`image_url` content part:

```json
{
  "model": "vision",
  "messages": [
    {"role": "user", "content": [
      {"type": "text", "text": "What is in this image?"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<...>"}}
    ]}
  ]
}
```

## 3. Error contract

- `400` - malformed JSON body, body not a JSON object, missing `X-Hub-App` header, or an
  unknown `model`/alias string on `/v1/chat/completions` or `POST /jobs`. Also: every vendor
  the router tried rejected the request itself, and the last one's body is passed through
  unchanged. A single vendor rejecting it is never a 400 - the hub tries the next candidate.
  A vendor's own 404 or 402 never reaches you: that pair is parked and the run continues.
- `401` - non-loopback call without a valid `Authorization: Bearer $LLMHUB_TOKEN` (loopback
  calls never need a token).
- `404` - unknown job id on `GET /jobs/{id}`, or an unknown model key on the
  `/api/models/{key}/forgive|disable|enable` management endpoints. (Chat completions with an
  unknown model string is a 400, not a 404 - see above.)
- `429` - no free candidate currently has quota. Body:
  ```json
  {"error": {"message": "no eligible model with quota", "type": "no_candidates",
             "rejected": [{"model": "...", "account": "...", "reason": "quota",
                           "detail": "hourly", "remaining_out": 500, "remaining_in": 10000,
                           "resets_at": "2026-09-07T11:00:00+00:00",
                           "requested_out": 900, "requested_in": 50}],
             "next_window_at": "2026-09-07T10:00:00+00:00",
             "remaining_out": 500, "remaining_in": 10000,
             "constraints": {"min_context": 24000, "avoid": ["reasoning"],
                             "max_latency_ms": 30000}}}
  ```
  `rejected` lists every candidate the router looked at and why it was skipped (`disabled`,
  `capability`, `paid`, `no_key`, `exhausted`, `cooldown`, `quota`, plus `context_unknown` /
  `context_small` under `X-Hub-Min-Context` and `app_banned` for a model your app banned, with
  the ban reason as `detail`). `error.constraints` is what the alias and your headers asked
  for together - read it first when the pool came back empty. A `quota` row also carries
  `detail` (the binding window), `remaining_out`/`remaining_in` (tokens left in that window),
  `resets_at`, `remaining_requests` (calls left, on tiers metered in requests rather than
  tokens - `null` when the vendor caps only tokens), and `requested_out`/`requested_in` (what
  your call asked for). Top-level
  `error.remaining_out` / `error.remaining_in` are the best case: the max across quota-rejected
  candidates. `next_window_at` is the earliest UTC timestamp when a free window in the pool
  resets (or `null` if none apply). Headers: `Retry-After: 60`, `X-Hub-Next-Window`, and
  `X-Hub-Remaining-Out` / `X-Hub-Remaining-In` / `X-Hub-Remaining-Requests` (mirroring the
  JSON; omitted when nothing was rejected for quota or the model declares no cap for that
  metric).
- `502` - every candidate the router tried actually failed (not quota). Body:
  ```json
  {"error": {"message": "all candidates failed", "type": "upstream_failure",
             "attempts": [{"model": "...", "account": "...", "status": "...",
                           "error_code": "...", "latency_ms": 0, "attempt": 1}],
             "last": {"error": {"message": "..."}}, "budget_exhausted": true}}
  ```
  `last` is the final vendor's own body. `budget_exhausted` is present when the hub stopped at
  its wall-clock budget (90 s) with candidates left untried, so the pool is not necessarily
  exhausted. Retry once after `Retry-After: 30`; if that fails too, submit it as an async job
  instead of looping.
- `422` - `POST /jobs` only: the request can never run. Body:
  ```json
  {"error": {"code": "window_too_small", "message": "max_tokens 6000 exceeds ...",
             "window_limit": 300, "requested_out": 6000, "model": "auto"}}
  ```
  `window_limit` is the largest a window for that model ever opens with, not what is left in
  it right now. Resubmit with `max_tokens` below it (or split the work); waiting changes
  nothing.
- `429` on `POST /jobs` with `error.code = "queue_full"` - your app already has the maximum
  number of live jobs (200 by default). Poll and finish what is queued before posting more.
- `503` - the target app is paused (`POST /api/apps/{app}/pause` was called for it), or the
  hub owner cancelled this call from the dashboard:
  ```json
  {"error": {"message": "the hub owner cancelled this call from the dashboard",
             "type": "cancelled_by_owner"}}
  ```
  `X-Hub-Attempts` says how far the run got. A cancelled call is not a hub failure and not a
  retry signal: ask the owner before sending it again.

Correct client reaction to a 429: do not loop-retry the same call.
- `error.remaining_out` (or header `X-Hub-Remaining-Out`) > 0: retry once with `max_tokens`
  <= that value minus 10%. The room is real, your first call just asked for more than it.
- 0 or absent: submit the same request as an async job (section 4) so it waits for quota on
  its own, or sleep until `next_window_at`/`Retry-After` and try once more.

Looping synchronous retries against a 429 just burns wall-clock time for nothing - the hub
already tried every free candidate it has.

Quota windows are per (account, model), not global. Two different models never share a
budget, so a batch can run them in parallel: send explicit `provider/model_id` per worker, or
keep `auto` and split them with `X-Hub-Prefer: provider/model`.

**When you stop waiting, so does the hub.** Close the connection, time out client-side, or
hit ctrl-c and the hub notices within a second: it cancels the vendor call (an http request
closed, a CLI agent's process group killed), frees the concurrency slot, and books the attempt
as usage status `abandoned` - which counts as neither a success nor an error for your app.
Nothing keeps running in the background, so there is no answer to collect later. A client-side
timeout is therefore not a retry signal either: the call it abandoned is gone, and sending the
same request again while the first was still going is what put dozens of dead calls in a
vendor's queue. Work that needs longer than your own timeout belongs in a job (section 4): a
job holds no socket, waits for a slot as long as it takes, and is the only shape of request the
hub is willing to keep alive without someone reading it.

## 4. Async jobs (batch / overnight / quota-bound work)

Use `/jobs` for anything that does not need an answer in the same request-response cycle, or
for work you expect to exceed current quota (the job then waits and drains automatically).

`POST /jobs` body:

```json
{
  "app": "myapp",
  "model": "auto",
  "require": ["json"],
  "priority": 5,
  "ttl_s": 21600,
  "request": {"messages": [{"role": "user", "content": "..."}], "max_tokens": 200},
  "callback_url": "http://127.0.0.1:9000/hub-callback"
}
```

Fields: `app` (required, same as `X-Hub-App`), `model` (alias or `provider/model_id`, default
`auto`; `alias` is also accepted as a synonym), `require` (capability list, default `[]`),
`priority` (1 high .. 9 low, default 5), `ttl_s` (how long the job may wait before the hub
gives up on it; default 6 h, capped at 36 h), `request` (the full OpenAI chat body,
required), `callback_url` (optional; the hub POSTs `{"id","state","result"}` there on
completion; best effort, failures are only logged - and only on `done`).

`GET /jobs/{id}` returns the job row. States: `queued`, `running`, `waiting_quota` (no free
candidate right now, `next_window_at` set, job is retried automatically - a job is never
failed for lack of quota), `done`, `failed`, `expired`, `cancelled`. On `done`, `result` is
the full OpenAI chat completion response object (JSON, as a string field - parse it);
`served_by` is `provider/model_id#account_id`. On `failed`, `error` has the reason.

`expired` means the job left the queue at its deadline without ever running. Treat it like
`failed`: a terminal state you do not retry blindly. `error` is JSON -
`{"code": "expired", "reason": ..., "message": ...}` - and `reason` says what to do:
- `window_too_small` - the request asks for more output tokens than the model's window ever
  opens with. Lower `max_tokens` or split the work; reposting as-is expires again.
- `no_window` - every candidate was out of quota for the whole ttl. Repost later, or with a
  longer `ttl_s`, or check `GET /api/status` for when a window resets.
- `provider_down` - no usable candidate (no key, disabled, unknown model). Owner's problem,
  not something a retry fixes.
- `no_worker` - a model had room but the queue never reached this job. Safe to repost.
A `failed` job can also carry JSON with `code: "lease_lost"`: the worker running it died
three times over. Safe to repost once.

- `GET /jobs?app=myapp` - live jobs (`queued`, `waiting_quota`, `running`), newest first.
  Add `state=done|failed|expired|cancelled` for one state, or `state=all` for the history.
- `DELETE /jobs/{id}` - cancel a job that has not reached a terminal state yet.
- `POST /api/apps/{app}/pause` / `POST /api/apps/{app}/resume` - stop/restart the worker from
  picking up jobs for one app (existing running job finishes; queued/waiting ones just wait).

Poll-until-done bash loop:

```bash
JOB_ID=$(curl -s -X POST http://127.0.0.1:8800/jobs \
  -H 'Content-Type: application/json' \
  -d '{"app":"myapp","model":"auto","request":{"messages":[{"role":"user","content":"Say OK."}]}}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

while :; do
  JOB=$(curl -s "http://127.0.0.1:8800/jobs/$JOB_ID")
  STATE=$(echo "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')
  case "$STATE" in
    done)
      echo "$JOB" | python3 -c 'import sys,json
j=json.load(sys.stdin)
r=json.loads(j["result"])
print(r["choices"][0]["message"]["content"])'
      break ;;
    failed|expired|cancelled)
      echo "job $STATE: $JOB"; break ;;
  esac
  sleep 5
done
```

## 5. Introspection

- `GET /v1/models` - eligible free models right now (add `X-Hub-Allow-Paid: 1` to also list
  paid ones), each with a `hub` block: `caps`, `context`, `free`, `status`
  (`ok|exhausted|cooldown|down`), `windows` (per-window `used`/`limit`/`resets_at`),
  `remaining_out`, `remaining_in`, `window_limit`, `resets_at`, `last_error`,
  `avg_latency_ms`. Aliases (`auto`, `vision`) are listed too, with `hub.alias: true`.
  Size `max_tokens` against `hub.remaining_out`: asking for more than what is left in the
  binding window is a 429 on the spot, and asking for more than `hub.window_limit` can never
  be served by that model at all. `null` means the model declares no cap on that axis.
- `GET /api/status` - same model rows plus queue depth (`queue.live` = jobs still moving,
  `queue.depth_by_state`, `queue.by_app`, `queue.oldest_queued_at`,
  `queue.expired_last_24h`, and `queue.apps` with each app's `queued`/`running`/`cap`),
  alias definitions, and account key-presence.
- `GET /api/usage?group_by=app` (also `model`, `account`, `day`, `provider`; add
  `since=<ISO>` and/or `app=<name>` to filter) - token/request/error counts.
- `GET /api/apps/{app}/bans` - models this app has banned for itself, with the reason and
  when. `POST` the same path with `{"model", "reason"}` to add one (201; 404 for a model key
  the registry does not have, 422 without a reason of at least 8 characters), `DELETE
  /api/apps/{app}/bans/{model}` to lift it.

Check `GET /api/status` (or `/v1/models`) before kicking off a large batch of jobs: if the
models you need are already `exhausted` with a `resets_at` hours away, submitting hundreds of
jobs just fills the `waiting_quota` queue instead of doing anything - better to size the batch
to remaining `windows` headroom, or accept it will drain overnight. Jobs that cannot run in
their ttl end `expired` rather than waiting forever, so a batch sized past the day's quota
comes back as verdicts, not as silence.

Workers are shared fairly between apps: one app can use the whole pool while it is the only
one queueing, and gives slots back as other apps arrive (nothing running is interrupted).
`queue.apps.<app>.cap` says how many jobs of yours may run right now.

## 6. Rules for agents

- Always set `X-Hub-App` to the calling project's name (`my-app`, `batch-ocr`, ...), never a
  placeholder like `test` or `agent`.
- Prefer `model: "auto"` (or `"vision"` for images). Do not hardcode a `provider/model_id`
  unless there is a specific reason to pin one vendor.
- Need a capability (vision, json, tools, reasoning)? Use `X-Hub-Require: vision`, do not
  hardcode a model id to get it.
- Never send secrets or private/personal data through the `explabs` provider - its free tier
  is "not ZDR" (not zero-data-retention, per the registry notes), public data only.
- Never set `X-Hub-Allow-Paid: 1` unless the owner explicitly asked for it in that session.
- On a 429, do not retry in a loop - retry once with a smaller `max_tokens` if
  `error.remaining_out` > 0, otherwise submit the request as a job (section 4) or wait for
  `next_window_at`.
- On an `expired` job, read `error.reason` before doing anything. Never repost the same
  request in a loop: `window_too_small` will expire every time.
- Log `X-Hub-Model` from the response (or `served_by` from a job) so the owner can see which
  vendor actually served each call.
- Ban a model for your app only when the model itself produced an unusable answer, with a
  reason that names what it did. Never for a transient error, a timeout or a quota hit.
- Do not re-send a request your own client just timed out on. The hub cancelled it when you
  stopped listening; if the work is slower than your timeout, queue it as a job instead.

## 7. Dashboard

`http://llmhub.localhost/` on this Mac, `http://mac.local/hub/` from the LAN. Shows models
(status, remaining quota per window, latency), usage, queue, accounts, and a promo watchlist.
Forgive an exhausted model, disable/enable a model, and pause/resume an app are all available
there (same actions as the `POST /api/models/{key}/forgive|disable|enable` and
`POST /api/apps/{app}/pause|resume` endpoints).
