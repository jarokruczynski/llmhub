# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

The published history starts at 0.4.0. Entries for 0.1.0 through 0.3.0 are kept
for the record but have no tags: those commits are not part of this repository.

## [Unreleased]

### Fixed

- A transient failure no longer parks a model until the next top of the hour. A timeout, a
  5xx after the retry, a CLI crash or unparseable output, an overloaded upstream, and an agy
  quota refusal that names no reset now cool the pair down on a backoff ladder (30 s, 60 s,
  120 s, 240 s, then 300 s), reset by the next answer. Three timeouts in a row no longer park
  the pair as `unavailable` for ten minutes, and `unavailable` parks follow the same ladder.
  A named reset ("Resets in ...") and the `agy -p /quota` report still park to their instant.
- The 429 `next_window_at` counts cooldowns and `unavailable` parks, and a model without
  declared windows (agy, `free: {}`) no longer reports the next top of the hour: it reports
  when its current hold ends, or nothing. Readers stopped a whole sheet on "next window
  11:00Z" while the pool had room and the model was on a 60 s cooldown.

### Changed

- Antigravity (`agy`) quota parks cover the whole model group on both logins: a refusal on
  any Gemini id parks every Gemini id, a refusal on a Claude or GPT id parks that group.
  Parks from an older build spread at startup. The hub reads `agy -p /quota` at startup and
  after each refusal and parks every group the report shows at 0% until its reset.
  `/api/status` rows carry `exhausted_reason`, and the dashboard card shows it.

### Removed

- The fallback to the key directory used before `~/.llmhub/env` was the default. Provider
  keys are read from `LLMHUB_ENV_DIR`, or `~/.llmhub/env` when it is unset; an install that
  still keeps its `*.env` files elsewhere moves them there or sets `LLMHUB_ENV_DIR`.

## [0.6.0] - 2026-09-24

### Added

- Prompt recorder: a console tab showing what each app sent and what came back, off unless it is
  turned on. `POST api/recorder/start` arms it for a window (20 minutes by default, 120 maximum)
  and clears whatever the previous run caught; it stops on its own when the window ends.
  Deliberately in memory rather than the database - at the observed rate, storing prompt text
  would be gigabytes a day - so it is capped at 200 attempts, 6000 characters a message and
  24000 a prompt, and does not survive a restart. `GET api/recorder` is the only read in the
  console that requires the token, because it is the only one that returns the text itself.
  Refused attempts are recorded too, with what the vendor said instead of an answer: a failing
  attempt raises out of the backend, so recording only the success path left a recorder armed
  during a quota storm reporting that nothing had gone through the hub.
- The recorder reads as chats. Each app is a session with its own pane; one session fills the
  screen, two split it, up to four share it, and a bar at the top turns each session on or off.
  An attempt is opened when it is sent, so the prompt shows while the model is still thinking,
  and closed when the answer, the refusal or the end of the stream arrives. Attempts of one
  request share a `request_id` and read as one prompt with several answers. Each answer carries
  when it came, how long it took (and the first byte, for a stream), the model, the account and
  the tokens the vendor billed, or an estimate marked as one. Streamed answers are captured as
  they pass through. History an app resends on every call is folded away. The console polls
  with `since=<rev>` and draws only what changed, so reading an older message is no longer
  thrown back to the top every two seconds.
- A request shows in the recorder as it arrives, while it waits for a free slot, and the first
  attempt takes it over. Footers lead with how long the app waited, from send to answer across
  the queue and refused attempts ("answered in 1 min 05 s"), then the split ("queued 40 s +
  model 25 s"). A request that ends before any vendor is asked shows as `not sent` with the
  reason.
- Images for the agy and gemini-cli backends: `data:` image parts are written to the provider
  workdir, referenced from the prompt (agy via view_file, gemini-cli via `@name`) and removed
  after the run. Remote image URLs stay a 400; copilot stays text only. Vision cap on the
  antigravity Gemini and Claude ids and the gemini-cli ids.
- An agy provider with its own HOME keeps its token in a file instead of a keychain that does
  not exist there, and the auth hint prints the login line with the provider env.

### Changed

- The Playground settings say what they do: three steps (who answers, what you ask, how it
  answers), a sentence per field, a live description of the chosen routing profile (models tried
  in order, fallback, spread, required capabilities) or of a single model with no fallback,
  temperature as a labelled slider, and the length limit converted to words.

### Fixed

- A verification probe is bounded by `Settings.probe_timeout_s` (60 s, env
  `LLMHUB_PROBE_TIMEOUT_S`) and reports `timeout` instead of waiting out the client read timeout,
  which is ten minutes. Quick add ran into exactly that against a vendor that accepts the
  connection and never answers: the dialog looked dead and repeated clicks stacked six
  ten-minute probes. The dialog also disables its button while it works, says what it is waiting
  for, and no longer swallows a rejected request.
- A model card no longer shows a red quota banner under a green "ok" badge. The hub stops
  holding a refusal against a pair after six hours; the card kept showing it until a success
  came after it, which for a pair nothing had retried meant forever. A refusal the hub no longer
  counts is shown as muted history ("last refusal 3d ago") instead of a warning.
- Jobs are no longer failed by a pool-wide transient refusal. A job whose attempts all ended in
  statuses that time can fix (quota, retry, unavailable, abandoned) is parked until its deadline
  instead of failed; too_large, unsupported_param, not_found, auth and error still fail it. A
  dead key next to a transient refusal parks too, while a pool that only answered `auth` fails.
  An auth refusal now parks the pair as unavailable and writes an `unavailable` event, so a
  broken key shows on the pair instead of killing jobs.
- Every park carries a backoff. A park with a known next window left `next_attempt_at` empty,
  so the job was claimed again on every 2 s poll; one job reached 10204 attempts in six hours.
  Forgive clears the backoff of parked jobs and reports how many it woke.
- An agy quota refusal ("Resets in 11h1m43s") parks the pair until the named reset instead of
  the template's daily default.

### Security

- `UPDATE` statements whose columns come from keyword arguments (jobs, promos, scout runs) check
  every name against the table's columns and refuse the rest. Values were always bound as
  parameters and every caller passes fixed names, so nothing was reachable; the check keeps it
  that way when a caller one day forwards a request body.
- CI actions are pinned to commit SHAs, and Dependabot waits seven days before proposing a new
  release of a dependency or an action.
- Background tasks that ended with an error on shutdown (health sweep, job dispatcher, scout)
  log it instead of discarding it silently.

## [0.5.0] - 2026-09-17

### Removed

- The classic dashboard (`index.html`, `app.js`, `app.css`, ~5k lines). The Studio console is
  now the only one and is served at the mount root; `/v2` answers 308 to a relative `../`, so a
  bookmark still lands on it both here and behind the `/hub` prefix on the LAN. Its requests are
  resolved against the document base rather than climbing a level, which is what the old
  two-level address needed.

### Added

- `GET api/baselines` and `llmhub/baselines.py`: the list prices the savings estimate compares
  against, per million tokens, with the vendor's own pricing page linked and the date they were
  read (`AS_OF`). The dashboard fetches them instead of carrying its own copy - the prices there
  were hardcoded with no date or source and had fallen a model generation behind. Cached input
  is priced at its own rate, tier matching keeps a small model from being priced as a flagship,
  and a missing table shows a dash rather than `$0.00`.
- The account health sweep runs on a schedule (`llmhub/health.py`, `health_sweep_every_h`, env
  `LLMHUB_HEALTH_SWEEP_EVERY_H`, default 6 hours, `0` disables). It was manual only, and its
  summary lived in a module-level variable that died with the process; a row per sweep now goes
  to `health_sweeps`, so `GET api/health/status` survives a restart and reports the schedule and
  the next run. The clock counts from the last recorded sweep, so restarting neither re-probes
  everything nor postpones the next sweep. A probe costs one request against a free allowance,
  so the scheduled run skips accounts real traffic already proved inside the window
  (`recently_served`) and pairs whose concurrency slot is busy (`in_use`), and each probe is
  bounded at 60 s so one slow CLI backend cannot stretch the run.
- `env` on a `kind: cli` provider block: values set outright for the child process, applied
  after `env_passthrough`, with `~` expanded and `env_deny` still outranking them. It exists so
  two licences of one CLI can be registered side by side - the Gemini CLI keeps its login under
  `$HOME/.gemini` and has no flag pointing elsewhere, so a second subscription needs a second
  block with its own `HOME`. Quick add copies `env` from a template that sets it.
- Gemini CLI as a `kind: cli` backend (template `gemini-cli`, command `gemini`, dialect
  `GeminiDialect`): the CLI signs in with a Google account, so the plan behind that account
  sets the allowance (free login 1000 requests a day, AI Pro 1500, Ultra 2000) instead of the
  API key's 250-a-day Flash-only tier. Read-only (`--approval-mode plan`), `--skip-trust` so an
  untrusted empty workdir cannot force the mode back to asking a human, and `env_deny` for
  `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_APPLICATION_CREDENTIALS` /
  `GOOGLE_GENAI_USE_VERTEXAI` so a stray key cannot outrank the login. Token counts are summed
  across every model a run touched. Sign in with `gemini` in a terminal before adding the
  account; there is no key to paste.
- Owner kill switch for calls in flight: `POST api/live/{call_id}/cancel` and
  `POST api/live/cancel` with `{"app": "..."}` or `{"all": true}`. The vendor call is cancelled
  (an http request closed, a CLI process group killed), the concurrency slot freed, and a
  caller still waiting gets 503 `cancelled_by_owner`. Every live chip in the dashboard has an
  x, and an app row with more than one call in flight has a "kill N" button behind a confirm.
- Client disconnect detection: `/v1/chat/completions` polls `request.is_disconnected()` every
  second and cancels the run when the caller stops waiting. uvicorn does not do this on its
  own, so an abandoned request used to hold its slot and its vendor call to the end.
- Usage status `abandoned` for an attempt nobody is waiting for any more, with error code
  `client_gone` or `cancelled_by_owner`, plus events of the same kinds. It is excluded from
  every error count (`api/usage`, `api/status` model rows, app rows, `last_error`): the vendor
  never got the chance to fail.
- `api/live` in-flight rows carry `call_id` and `state` (`waiting` for a concurrency slot, or
  `running` against the vendor). A waiting chip is muted and labelled in the strip and in the
  Models tab's Apps cell.
- Dead backend detection: three consecutive timeouts on one (account, model) park it as
  `unavailable` with code `timeouts`; any answer resets the count.

### Changed

- The run budget now bounds waits and attempts, not just the move to the next candidate. A
  semaphore acquire waits at most half of what is left of the budget and then notes the
  attempt `busy` / `slot_wait`; a pair with `concurrency * 2` runs already queued is skipped
  without waiting (`slot_queue_full`); the budget is re-checked after the acquire; and one
  attempt runs under `min(remaining, vendor bound) + 15 s` before it is cancelled as
  `attempt_timeout`. Jobs still wait unbounded - a job holds no socket.
- The in-flight registry entry is created when the run starts rather than at the vendor call,
  so a call queued for a slot is visible, under the model it is queued on. `in_flight_count`
  now counts running calls only.
- `DELETE /jobs/{id}` on a running job cancels the worker instead of only flipping the row;
  the job ends `cancelled` with error code `cancelled_by_owner` and reports `stopped_worker`.

### Fixed

- Orphaned calls. Runs whose clients had timed out hours earlier queued indefinitely on a busy
  pair's semaphore (61 in flight on the live hub, the oldest 3516 s), each eventually spending
  a full vendor timeout on an answer nobody would read.

## [0.4.0] - 2026-09-10

### Added

- `unavailable` table: (account, model) pairs the vendor refuses to serve, parked with a TTL
  and expiry on read. `not_found` (404, `model_not_found`, 402/`payment_required`) parks for 7
  days, `unavailable` (a 4xx that reports the provider's own upstream as down) for 10 minutes.
  `LLMHUB_NOT_FOUND_TTL_S`, `LLMHUB_UNAVAILABLE_TTL_S`.
- `Classification.retry_after_s`, parsed from vendor prose ("try again in 27m44.063s"), a
  `Retry-After` header, or Google's `retryDelay` error field. A wait under 8 s is slept, a
  longer one becomes a cooldown of that length; on a quota hit it caps the park.
- Run time budget: `LLMHUB_RUN_BUDGET_S` (90 s) for sync and stream calls, none for jobs. A run
  that stops on it answers 502 with `budget_exhausted: true`.
- `/v1/models` and `api/status` model rows carry `remaining_out`, `remaining_in`,
  `window_limit` and `resets_at`, so a client can size `max_tokens` before sending.
- `observed_request_caps.max_out_tokens`: the learned output-token ceiling, exposed via the
  `X-Hub-Max-Output` header on a 413 and `observed_out_cap` in `api/status`.
- Dashboard Models tab: every column header sorts (asc, desc, then registry order), replacing
  the single sort dropdown. A filter bar (status, caps, provider, free text, "in use now",
  "used today", "only non-ok") mirrors the Promos tab and persists in the browser. The Apps
  column shows one live chip per in-flight call (app name, elapsed time, attempt) ahead of the
  muted recent-app chips, joined against the same `api/live` feed as the strip above the tabs.
- Selection constraints. Alias fields `min_context`, `avoid` (caps) and `max_latency_ms`, with
  request headers `X-Hub-Min-Context`, `X-Hub-Avoid` and `X-Hub-Max-Latency-Ms`; alias and
  header combine to the stricter side (max, union, min). `min_context` is a requirement -
  a context below it is rejected `context_small`, an undeclared one `context_unknown`. `avoid`
  and `max_latency_ms` only order the pool: the sort key becomes
  `(busy, header_rank, penalty, prefer_rank, order)`, so a thinking or slow model sorts last
  but stays as a fallback. `Selection.constraints` is echoed in the 429 body under
  `error.constraints`.
- Per-app model bans. `app_model_bans` (app, model, reason, created_at) plus
  `GET|POST api/apps/{app}/bans` and `DELETE api/apps/{app}/bans/{model}`; the reason is
  mandatory (8 chars, 422 without it) because the hub cannot measure what the app judged. A
  banned pair is rejected `app_banned` for that app's sync calls, streams and jobs. Dashboard
  Apps table: a Bans count per row, expanding to the reasons with an Unban button.
- Alias `extract` in `providers.example.yaml`: long inputs, strict JSON, verbatim quoting,
  `min_context: 24000`, `avoid: [reasoning]`, `max_latency_ms: 30000`.
- `python -m llmhub refresh-context [--dry-run] [--provider NAME]`: fetches each discoverable
  provider's own model listing once and fills `model.context` where the vendor's row named a
  context window and the registered model had none, without ever overwriting a context the
  owner already set. Backs `providers.yaml` up to `~/.llmhub/backups/` first, same as
  `promos-dedupe`. `discover_model_rows` (`accounts.py`) carries the context window alongside
  each discovered id; `discover_models` itself is unchanged.

### Changed

- No vendor failure escapes the router. `error` no longer re-raises to the client: the run
  moves to the next candidate. Three candidates from three distinct providers all answering
  `error` stop the run early.
- `AllCandidatesFailed` maps to 400 with the vendor body only when every attempt was `error`
  and a vendor actually returned an HTTP status; otherwise 502 `upstream_failure` with
  `attempts`, the last vendor body under `last` and `Retry-After: 30`.
- `RETRY_DELAYS` is `(2.0,)`: one short retry per candidate instead of a 5/15/45 ladder.
- "Rate limit reached ... per day (TPD)" is a `daily` quota for any provider, not a retry.
- `POST api/models/{key}/forgive` also clears the parked-route rows.
- Observed quota caps are metric- and window-aware. `Classification.quota_detail`
  (`metric`, `window`, `limit`, `used`) is parsed from groq's `on <metric> per <unit> (XPX):
  Limit N, Used M` line, google's `details[].violations[]` (`quotaId`, `quotaValue`), google's
  prose `metric: ..., limit: N`, and finally the window markers plus a bare `Limit N`. A cap
  the vendor states is recorded verbatim; only a body naming no metric but mentioning tokens
  still falls back to "what this account spent" on `out_tokens`.
- A quota body whose window is a minute is a `retry` with `retry_after_s`, not an exhaustion:
  nothing is parked and nothing is learned.
- Quota scope resolution order: `quota_detail.window`, prose markers, the rule, the provider
  template default. A `quotaId` naming `PerDay` now beats the template's `hourly` guess.
- Windows can cap requests: `QuotaWindow.requests`, `window_usage["requests"]` (rows whose
  status is not `quota`), `has_room` plans one more call, and `room()` reports
  `remaining_requests` next to `remaining_out`/`remaining_in`. The 429 body, the selection
  rejection rows, `api/status` model rows and `X-Hub-Remaining-Requests` carry it; the binding
  window reported in a rejection is the axis that actually blocked.
- Catalog: cohere ships `command-a-plus-05-2026` and `command-a-03-2025` with the `json` cap -
  the compatibility endpoint takes `response_format: {"type": "json_object"}`.

### Fixed

- A vendor 404 no longer surfaces as a 404 from the hub: the pair is parked and the next
  candidate serves the request.
- A daily quota no longer costs four attempts and 65 s of sleep before the fallback.
- "Request too large ... (OTPM)" is `too_large` on the output axis instead of being recorded as
  an input-size ceiling.
- A request-metered free tier no longer learns a token cap out of its own 429. A gemini key
  limited to 20 requests per day had been recording hourly `out_tokens` caps of 97-1595 (the
  tokens spent when the request quota ran out), which then rejected every request reserving
  4000 out tokens on every gemini model. groq's `Limit 200000` is stored as 200000 instead of
  as the tokens used to reach it.
- Gemini's OpenAI-compatible endpoint returns error bodies as a one-element JSON list; the
  classifier now unwraps that shape (and `{"errors": [...]}`), so the `QuotaFailure` details
  and `retryDelay` are read and a daily request quota is recorded as daily, not as the
  template's hourly default.

## [0.3.0] - 2026-09-09

### Added

- Live view: which app is on which model right now. The router keeps an in-memory registry of
  calls in flight (app, model, account, kind, attempt, job id, elapsed), written inside the
  semaphore guard before the vendor call and moved on a fallback; a stream's entry lives until
  the stream generator finishes. `GET api/live?window_min=15` serves it per app together with
  what the app used over the window, `api/status` model rows gain `apps_15m` and `in_flight`.
- Dashboard: a Live strip above the tabs, one row per app, in-flight chips with a ticking
  elapsed counter and muted chips with call counts for the window. It polls every 2 s while
  the page is visible and backs off to 10 s when nothing has been in flight for a minute.
  Models table gains an "Apps (15m)" column and an in-flight badge next to the model name.

- Promo rejection with a reason. `promos.status` gains `rejected`; `POST
  api/promos/{id}/reject {reason}` (and `PATCH` with `status: rejected`) refuse a reason
  shorter than 8 characters with 422, `POST api/promos/{id}/reopen` takes the lead back, and
  `GET api/promos/rejections` serves the history newest first. Every rejection event is kept
  in `promo_rejections`, so it outlives the row it came from.
- The curate prompt and the promo-hunt skill read the rejections and treat each reason as a
  standing rule: an offer from a rejected vendor, or one that fails for the same reason, is
  a `skip_rejected` counted in the run report as `skipped as rejected`.
- Promos tab: Reject with an inline reason form, Reopen on a rejected row, the reason shown
  under the note and on the status chip, a `rejected` status filter chip, and "hide used,
  expired, rejected".

- Job deadlines: `POST /jobs` takes `ttl_s` (default 6 h, cap 36 h) and a job that has not
  run by then leaves the queue as `expired` with `error.reason` = `no_window`,
  `window_too_small`, `provider_down` or `no_worker`. Nothing waits in the queue forever.
- Feasibility check at enqueue and on every scheduling pass: a request whose `max_tokens`
  is over the widest window the model will ever open is refused with 422 `window_too_small`
  and `error.window_limit` instead of being queued.
- Running jobs hold a lease (request timeout + 60 s). A `running` row with no worker behind
  it goes back to `queued`, and fails with `error.code = "lease_lost"` after three rounds.
- Jobs on models that report no window at all back off on their own clock (1, 5, 15, 30 min,
  then every 30) instead of waiting for a reset that is never announced.
- Per-app queue cap: over `LLMHUB_QUEUE_MAX_PER_APP` (default 200) live jobs, `POST /jobs`
  answers 429 `queue_full` with `Retry-After`. One dead client cannot fill the hub.
- Terminal job rows are purged after `LLMHUB_JOB_RETENTION_DAYS` (default 7).
- Fair share between apps: every freed worker goes to the app holding the fewest running
  jobs, up to `max(LLMHUB_JOBS_PER_APP_MIN, workers / apps with runnable work)`. One app can
  saturate an idle pool and gives slots back as other apps queue work; nothing is preempted.
  An event `queue` records the share each time the set of active apps changes.
- `GET api/status.queue` adds `live`, `oldest_queued_at`, `expired_last_24h`, `workers` and
  per-app `apps: {queued, running, cap, paused}`; `GET api/apps` carries the same per-app
  numbers. The Queue tab shows Running and Cap per app, and the header counter counts live
  jobs only.
- Settings: `LLMHUB_JOB_TTL_S`, `LLMHUB_JOB_TTL_MAX_S`, `LLMHUB_JOB_LEASE_MARGIN_S`,
  `LLMHUB_QUEUE_MAX_PER_APP`, `LLMHUB_JOB_RETENTION_DAYS`, `LLMHUB_JOBS_PER_APP_MIN`.
- Promo identity: every writer (scout, promo-hunt skill, plain `curl`) resolves the vendor
  before writing, so one vendor keeps one row on the watchlist. `POST api/promos` answers
  201 for a new vendor and 200 with `created: false, merged_into: <id>` for a known one,
  appends the note with the date and the source that reported it, and marks the row `used`
  when the registry already holds an account for that vendor.
- `promo_aliases`: the spelling and the host behind every resolution are learned, so the
  next post under a different name short-circuits. Quick add teaches the same table.
- `GET api/promos?duplicates=1` groups the watchlist by identity.
- `python -m llmhub promos-dedupe [--dry-run]`: one-time merge of a watchlist filed before
  identities existed, with a JSON backup under `~/.llmhub/backups/`.
- Quick add reuses a registered provider when the endpoint it resolved is already in the
  registry under another name (`provider_reused: true`) instead of adding a second block
  for the same host.
- Unsupported request parameters are learned per model: a 400 that names a parameter
  (`temperature` on reasoning routes, for example) strips it and retries the same candidate
  once; on success the parameter is stored and dropped from every later call. Listed per
  model in `GET api/status`.
- Scout run rows carry `invalid_reasons`: why a curator decision was dropped, with a snippet
  of the answer, instead of a bare count.
- Promos tab: filter by status, source and free text, "hide used and expired" toggle,
  sortable columns; filters and sort persist in the browser.
- `docs/ARCHITECTURE.md` with three Mermaid diagrams (system overview, request lifecycle,
  scout pipeline); the README overview diagram is the first of them.
- The two Claude Code skills (`promo-hunt`, `llmhub-client`) ship in `.claude/skills/`
  and load for any session started inside the repo (`docs/SKILLS.md`).

### Changed

- `GET /jobs` and `GET api/jobs` default to live states (`queued`, `waiting_quota`,
  `running`) newest first; `?state=all` returns the history. The old default returned the
  oldest 200 rows of the whole table, which hid live jobs behind finished ones.
- `LLMHUB_JOBS_PER_APP` is now an optional hard ceiling and defaults to 0 (none) instead of
  3. The everyday limit is the fair share; set it only to hold an app below its share.
- Scout matches promo rows by vendor identity instead of by url. A `used` row still keeps
  its status, url and endpoint; only its note grows.
- Scout no longer sends `temperature: 0` by default; routes pinned to another value
  rejected every extract call.
- The curator answer is parsed leniently: fenced JSON, a bare array, action synonyms
  (`add`, `patch`, `ignore`) and date-times in `expires_at` all resolve; the only hard
  failure left is an answer with no decision at all.
- Scout sources: fixed dead docs urls (novita, ollama), a `polite` flag that refetches a
  source at most weekly (inception), a longer timeout on pepper search pages.
- Promo-hunt skill: always post, never prefix `UPDATE:`; the hub merges by identity.

## [0.2.0] - 2026-09-09

### Added

- Aliases `fast` and `local`, and a new `strong` alias for the antigravity and explabs
  high-end models.
- Alias `spread`: fans a request across the least-recently-used of the top N eligible
  candidates instead of parking every call on the first `prefer` entry.
- Per-app job concurrency cap on the job queue, so one app's backlog cannot starve the
  others.
- Observed caps: a 429 that states its window records what the vendor actually allowed,
  used as the effective limit until a wider one is observed.
- Usage tab: tokens and requests by app, model and day, plus a timeseries chart.
- Quick add: guess-and-verify probes candidate base urls with the pasted key when no
  template matches, and picks up a `base_url`/`api_key_env` carried by a promo row.
- Provider catalog grew from 31 to 46 templates; discover skips non-chat model ids and
  strips vendor-specific id prefixes (e.g. gemini's `models/`) before registering them.
- Scout: scheduled promo hunting on the hub's own free models. Fetches a source list
  (`~/.llmhub/scout_sources.yaml`) with no LLM, extracts candidate offers on the `fast`
  alias, curates them on `auto`, and files the result on the Promos watchlist with
  `source: "scout"`. Daily at `LLMHUB_SCOUT_AT`, `POST api/scout/run`, or
  `python -m llmhub scout [--dry-run]`. Run history and reports under `api/scout/runs`.
- CLI provider backends (`kind: cli`): Google Antigravity (`agy`) and GitHub Copilot CLI
  run as gateway backends behind the same OpenAI-compatible interface.
- `opt_in_only` on a provider or model keeps metered or licensed capacity out of every
  alias pool unless the alias names it directly.
- Request size awareness: declared and observed per-model input/output ceilings, a
  `too_large` rejection, a 413 response with `X-Hub-Max-Request`, and truncated-JSON
  detection on `finish_reason: length`.
- CI: ruff and pytest on push and pull request, plus a Trivy scan of the lockfile,
  secrets and misconfig.

### Changed

- Copilot CLI is opt-in only: isolated from `auto`, `vision`, `fast`, `strong` and
  `local`, reachable only as `copilot/<model>` or its own alias.
- Quick add's generated account id derives from the OS username (`getpass.getuser()`,
  fallback `main`) instead of a hardcoded name.

### Fixed

- launchd job execs the venv interpreter directly instead of going through `uv run` or a
  shell, fixing a TCC-driven "operation not permitted" failure for CLI providers.

## [0.1.0] - 2026-09-07

### Added

- Local OpenAI-compatible gateway that routes chat completions across free vendor
  endpoints, with per-account quota windows (hourly/daily/monthly/allowance) tracked in
  SQLite, a deferred job queue, and a server-rendered dashboard.
- Accounts tab: add, rotate and remove provider accounts from the dashboard; per-provider
  model discovery; one-shot test call on an account.
- Events tab with filtering, and full-width table layout across Models, Usage, Queue and
  Accounts.
- Quick add: paste a key plus a free-text hint (or a promo row) and the hub resolves the
  provider template, writes the env line, discovers models, and runs a test call.
  Remaining-quota reporting on 429 responses and quota exhaustion scoped to the vendor
  error that caused it.
- Cloudflare quick add: account id resolved from pasted text or explicit fields, with
  Workers AI model discovery.

### Changed

- Registry and API design notes extended to cover dashboard-managed accounts/keys and
  promo-row quick add.

### Fixed

- Dashboard table layout for the Promos, Events and Models tabs.

[0.6.0]: https://github.com/jarokruczynski/llmhub/releases/tag/v0.6.0
[0.5.0]: https://github.com/jarokruczynski/llmhub/releases/tag/v0.5.0
[0.4.0]: https://github.com/jarokruczynski/llmhub/releases/tag/v0.4.0
