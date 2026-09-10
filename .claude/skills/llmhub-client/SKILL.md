---
name: llmhub-client
description: Use when a script or app on this Mac needs to call an LLM/VLM and should go through the local free-only llmhub gateway instead of a direct vendor SDK. Triggers on "hub", "llmhub", "wyslij do huba", "darmowy model", "free LLM", "batch LLM job", or any code that would otherwise call OpenAI/Anthropic/etc directly for a task that can run on a free endpoint.
---

# llmhub client

When the task needs an LLM call from code on this Mac, route it through the hub instead of
calling a vendor SDK directly.

## 0. Verify the hub is up

```bash
curl -sf http://127.0.0.1:8800/healthz || echo "hub down: run launchd/install.sh from the llmhub repo root"
```

If it prints the "hub down" message, run that install script (or ask the owner) before doing
anything else - do not fall back to a direct vendor call silently.

## 1. What it is

Base URL on this Mac: `http://127.0.0.1:8800/v1`. From the LAN: `http://mac.local/hub/v1`,
which needs `Authorization: Bearer $LLMHUB_TOKEN` (loopback needs no token, LAN always does).
Wire format is OpenAI Chat Completions (`POST /v1/chat/completions`). Free vendor endpoints
only - the hub itself decides which vendor/model serves the call; do not pick one yourself
unless you have a specific reason to pin it.

## 2. Make the sync call

```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-App: myapp' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say OK."}]}'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8800/v1",
    api_key="hub",  # placeholder, loopback needs no real key
    default_headers={"X-Hub-App": "myapp"},
)
resp = client.chat.completions.create(
    model="auto",  # or "vision", or an explicit "provider/model_id"
    messages=[{"role": "user", "content": "Say OK."}],
)
```

Set `model` to `"auto"` (text) or `"vision"` (needs an image) unless a specific
`provider/model_id` is required. Always set `X-Hub-App: <project-name>` - it is required
(400 without it) and drives per-app usage/pause on the dashboard. Add `X-Hub-Require:
vision,json` to demand capabilities instead of hardcoding a model id. `X-Hub-Prefer:
provider/model` nudges the router without excluding other candidates. Never send
`X-Hub-Allow-Paid: 1` unless the owner explicitly told you to for this call - default is
free-only.

For long inputs: `X-Hub-Min-Context: 24000` is a requirement - only models whose declared
context is known and at least that wide, so size it as worst-case input plus the output you
reserve (a model with no declared context is rejected too, since it cannot promise the room).
`X-Hub-Avoid: reasoning` and `X-Hub-Max-Latency-Ms: 30000` only reorder the pool: an avoided
or slow model still answers when nothing else is left. An alias can carry the same three
(`extract` does, for long inputs and strict JSON); alias and header combine to the stricter
side, and a 429 echoes the result under `error.constraints`.

Read these response headers back (log at least `X-Hub-Model`): `X-Hub-Model` (what served
the call), `X-Hub-Account`, `X-Hub-Attempts` (comma list of `model:status` tried in order),
`X-Hub-Remaining-Out` (out tokens left for that account+model window after the call; absent
on streams and on models with no declared cap).

`X-Hub-Model` names the model, so when your own quality check fails in a way that is the
model's fault - paraphrased instead of quoting, ignored the schema again - `POST
/api/apps/{app}/bans {"model": "...", "reason": "..."}` and the hub never routes your app
there again. The reason is mandatory (422 without one) and should say what the model did.
Never ban for a transient error, a timeout or a quota hit - the router handles those, and a
ban holds until `DELETE /api/apps/{app}/bans/{model}`.

Quota windows are per (account, model), so two models never share a budget - a batch can run
them in parallel by sending explicit `provider/model_id` per worker, or by splitting workers
with `X-Hub-Prefer: provider/model`.

Streaming: set `"stream": true` as usual, the hub passes SSE straight through. Vision images
go in as an OpenAI-format `data:` URL inside an `image_url` content part - no hub-specific
encoding.

## 3. Handle errors correctly

- `400`: bad JSON body, missing `X-Hub-App`, an unknown `model`/alias string, or every
  vendor the hub tried rejected the request itself (the last vendor body is passed
  through). One vendor saying no is not a 400 - the hub tries the next candidate.
- `401`: LAN call without a valid `Authorization: Bearer $LLMHUB_TOKEN`.
- `404`: unknown job id (`GET /jobs/{id}`) or unknown model key on the
  `forgive|disable|enable` management endpoints.
- `429`: no free candidate has quota right now. Body has `error.constraints` (what the alias
  and your headers asked for together - read it first), `error.rejected` (why each candidate
  was skipped: also `context_unknown`/`context_small` under `X-Hub-Min-Context` and
  `app_banned` with the ban reason as `detail`; a `quota` row carries `detail` = binding
  window, `remaining_out`, `remaining_in`, `resets_at`, `requested_out`,
  `requested_in`), `error.next_window_at` (UTC
  timestamp of the next free window) and `error.remaining_out` / `error.remaining_in` = the
  max room across quota-rejected candidates, mirrored in headers `X-Hub-Remaining-Out` /
  `X-Hub-Remaining-In` (plus `X-Hub-Next-Window`, `Retry-After`).
  **Do not retry this in a loop.** If `error.remaining_out` (or header `X-Hub-Remaining-Out`)
  is > 0, retry once with `max_tokens` <= that value minus 10%. If it is 0 or absent, submit
  the request as an async job instead (step 4), or sleep until `next_window_at`.
- `502`: every candidate actually failed (not a quota issue). Body has `error.attempts` with
  per-candidate status and error codes, `error.last` with the final vendor body, and
  `error.budget_exhausted: true` when the hub stopped at its 90 s budget with candidates
  still untried. Retry once after `Retry-After: 30`, then go async.
- `503`: the calling app is paused on the hub.

## 4. Batch / overnight / quota-bound work: use jobs, not a retry loop

```bash
curl -X POST http://127.0.0.1:8800/jobs \
  -H 'Content-Type: application/json' \
  -d '{"app":"myapp","model":"auto","priority":5,
       "request":{"messages":[{"role":"user","content":"..."}]}}'
```

Body fields: `app` (required), `model` (alias or `provider/model_id`, default `auto`),
`require` (capability list), `priority` (1 high .. 9 low, default 5), `ttl_s` (how long it may
wait, default 6 h, cap 36 h), `request` (full OpenAI chat body, required), `callback_url`
(optional, hub POSTs `{"id","state","result"}` there on completion). A job in `waiting_quota`
retries on its own once a window resets - it is never failed for lack of quota, but it does
end `expired` at its deadline. Terminal states: `done`, `failed`, `expired`, `cancelled`.

Treat `expired` like `failed` and read `error` (JSON) before reposting: `reason`
`window_too_small` means `max_tokens` is over the widest window that model ever opens - lower
it instead of retrying; `no_window` means quota never freed up in time; `no_worker` means the
queue never reached it (safe to repost). `POST /jobs` answers 422 `window_too_small` up front
for a request that could never run, and 429 `queue_full` when your app has 200 live jobs.

Poll-until-done:

```bash
while :; do
  JOB=$(curl -s "http://127.0.0.1:8800/jobs/$JOB_ID")
  STATE=$(echo "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')
  case "$STATE" in
    done) echo "$JOB" | python3 -c 'import sys,json
j=json.load(sys.stdin); r=json.loads(j["result"])
print(r["choices"][0]["message"]["content"])'; break ;;
    failed|expired|cancelled) echo "job $STATE: $JOB"; break ;;
  esac
  sleep 5
done
```

Other job routes: `GET /jobs?app=&state=` (live rows by default, `state=all` for history),
`DELETE /jobs/{id}` (cancel if not terminal yet),
`POST /api/apps/{app}/pause|resume` (stop/restart job pickup for one app).

## 5. Check quota before a big batch

`GET /v1/models` lists eligible free models with `hub.status`, `hub.windows`
(used/limit/resets_at) and `hub.remaining_out` - the number to size `max_tokens`
against, since asking for more than is left in the binding window is an immediate 429. `GET /api/status` adds queue depth. `GET /api/usage?group_by=app`
shows what has already burned tokens. Check status before queuing a large batch of jobs - if
the models you need are already `exhausted` for hours, size the batch down or accept an
overnight drain instead of flooding the queue.

## 6. Rules

- Always set `X-Hub-App` to the real project name, never a placeholder.
- Prefer `auto` / `vision`; use `X-Hub-Require` for capabilities instead of hardcoding a
  model id.
- Never put secrets or private/personal data into a prompt that can route through `explabs` -
  its free tier is not ZDR (public data only).
- Never set `X-Hub-Allow-Paid: 1` without the owner explicitly asking for it in-session.
- On 429, retry once with a smaller `max_tokens` when `error.remaining_out` > 0, otherwise go
  async (step 4) - never loop-retry.
- Log `X-Hub-Model` (or a job's `served_by`) so the owner can see what served the call.
- Ban a model for your app only when the model itself produced an unusable answer, with a
  reason naming what it did. Never for a transient error, a timeout or a quota hit.

## 7. Dashboard

`http://llmhub.localhost/` (this Mac), `http://mac.local/hub/` (LAN). Forgive an exhausted
model, disable/enable a model, pause/resume an app - all there, same actions as
`POST /api/models/{key}/forgive|disable|enable` and `POST /api/apps/{app}/pause|resume`.

## Opt-in-only providers
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
