# Architecture

This is a snapshot of the current code, not a design log - see
[DESIGN.md](DESIGN.md) for how each piece got here and what changed along the way.

## System overview

Every caller goes through the same gateway: an OpenAI SDK or curl, a Claude Code
session using the `llmhub-client` skill, another local app, or the scout pipeline
calling back into its own hub with `X-Hub-App: scout`. The gateway hands the
request to the router, which picks a candidate from either an HTTP provider
(OpenAI-compatible wire format) or a CLI provider (an agent CLI run headless in
an empty, sandboxed working directory). State lives in one SQLite database and
one YAML registry; the dashboard and the quick-add flow read and write both
through the same process. A LaunchAgent execs the venv's `python3 -m llmhub`
directly (not through `uv run` or a shell - macOS ties privacy grants to the
first program in the exec chain), and Caddy publishes the dashboard on
`llmhub.localhost` and under `mac.local/hub/*` on the LAN.

```mermaid
flowchart LR
    subgraph clients["Clients"]
        SDK["Any OpenAI SDK / curl"]
        CC["Claude Code sessions (llmhub-client skill)"]
        APPA["my-app"]
        APPB["batch-ocr"]
        SCOUTCLIENT["scout pipeline (calls itself back in)"]
    end

    subgraph outer["launchd + Caddy layer"]
        CADDY["Caddy: llmhub.localhost, mac.local/hub/*"]

        subgraph proc["llmhub process (LaunchAgent execs venv python directly)"]
            GW["gateway: /v1/chat/completions, /jobs, api/*"]
            ROUTER["router"]
            SCHED["scheduler: scout daily, job workers"]
            DASH["dashboard (served by same process, LAN + bearer token)"]
            QUICKADD["quick add: promo row -> template resolution -> env file + registry -> hot reload"]
        end
    end

    subgraph httpbackends["HTTP providers (OpenAI wire)"]
        HTTPLIST["explabs, gemini, groq, openrouter, cloudflare, cohere, zai, dashscope, ollama, ..."]
    end

    subgraph clibackends["CLI providers (headless, empty sandboxed workdir)"]
        CLILIST["agy, copilot"]
    end

    STORE[("SQLite store: usage, events, jobs, observed limits, observed request caps, unsupported params, promos, scout runs")]
    REGISTRY["registry: ~/.llmhub/providers.yaml + key files in env dir"]

    SDK --> CADDY
    CC --> CADDY
    APPA --> CADDY
    APPB --> CADDY
    CADDY --> GW

    GW --> ROUTER
    ROUTER --> HTTPLIST
    ROUTER --> CLILIST
    ROUTER --> STORE
    ROUTER --> REGISTRY
    GW --> STORE

    SCHED --> ROUTER
    SCHED --> SCOUTCLIENT
    SCOUTCLIENT -->|X-Hub-App: scout| CADDY

    DASH --> STORE
    DASH --> REGISTRY
    QUICKADD --> REGISTRY
    QUICKADD --> DASH
```

## Request lifecycle

`router.select` resolves `model` to a pool (the whole registry for an alias, or
one entry for an explicit `provider/model_id`), then filters that pool in a
fixed order - disabled, opt-in-only (unless the alias names the entry itself),
missing capability (`X-Hub-Require`), paid without `X-Hub-Allow-Paid`, no key,
expired allowance, exhausted window, cooldown, no quota room (against `est_in` /
`est_out` and the declared or observed windows), then a size ceiling check that
produces `too_large` instead of a doomed upstream call. What survives is ordered
by `X-Hub-Prefer`, then the alias's own `prefer`, then LRU rotation across the
top `spread` candidates, so a wide pool actually gets used instead of parking
every call on the first entry. `router.run` then walks that list: a success
writes a usage row and returns `X-Hub-Model` / `X-Hub-Remaining-Out`; a
`429`/`5xx` retries the same candidate on a 5/15/45 s backoff before moving on; a
quota error marks that (account, model) exhausted for the window and moves on;
`too_large` records the observed cap and moves on; an unsupported param is
stripped and the same candidate is retried once; a truncated JSON response (a
`response_format: json` reply that hit `finish_reason: length`) moves on without
retry; an auth error is raised straight to the caller, no fallback. Exhausting
every candidate that was rejected only for `too_large` answers `413`; any other
exhaustion answers `429`; every candidate failing outright answers `502`.

`POST /jobs` runs the same selection asynchronously: a job is `queued`, sits in
`waiting_quota` (with `next_window_at`) while nothing has room - it is never
failed for lack of quota - moves to `running` once a worker slot opens within
its per-app concurrency limit, and ends `done` or `failed`, firing
`callback_url` if one was given.

```mermaid
flowchart TD
    START(["POST /v1/chat/completions"]) --> RESOLVE["resolve model: alias (whole registry pool) or explicit provider/model_id"]
    RESOLVE --> POOLNEXT["take next candidate from pool"]

    POOLNEXT --> F1{"disabled?"}
    F1 -->|yes| REJ1["reject: disabled"] --> POOLNEXT
    F1 -->|no| F2{"opt-in-only and alias does not name it?"}
    F2 -->|yes| REJ2["reject: opt_in_only"] --> POOLNEXT
    F2 -->|no| F3{"missing capability (X-Hub-Require)?"}
    F3 -->|yes| REJ3["reject: capability"] --> POOLNEXT
    F3 -->|no| F4{"paid and no X-Hub-Allow-Paid?"}
    F4 -->|yes| REJ4["reject: paid"] --> POOLNEXT
    F4 -->|no| F5{"key present?"}
    F5 -->|no| REJ5["reject: no_key"] --> POOLNEXT
    F5 -->|yes| F6{"allowance expired?"}
    F6 -->|yes| REJ6["reject: expired"] --> POOLNEXT
    F6 -->|no| F7{"exhausted for window?"}
    F7 -->|yes| REJ7["reject: exhausted"] --> POOLNEXT
    F7 -->|no| F8{"in cooldown?"}
    F8 -->|yes| REJ8["reject: cooldown"] --> POOLNEXT
    F8 -->|no| F9{"quota room for est_in / est_out vs windows (incl. observed caps)?"}
    F9 -->|no| REJ9["reject: quota"] --> POOLNEXT
    F9 -->|yes| F10{"request fits input ceiling?"}
    F10 -->|no| REJ10["reject: too_large"] --> POOLNEXT
    F10 -->|yes| ELIGIBLE["add to eligible pool"] --> POOLNEXT

    POOLNEXT -.->|pool exhausted| CHECKPOOL{"eligible pool empty?"}
    CHECKPOOL -->|yes| ALLTOOLARGE{"every rejection was too_large?"}
    ALLTOOLARGE -->|yes| R413["413 too_large: error.max_request_tokens"]
    ALLTOOLARGE -->|no| R429A["429 no_candidates: remaining_out, next_window_at"]

    CHECKPOOL -->|no| ORDER["order: X-Hub-Prefer, then prefer, then LRU rotation over top `spread` candidates"]
    ORDER --> ATTEMPT["attempt next candidate"]

    ATTEMPT --> OUT1{"outcome?"}
    OUT1 -->|success| OK["record usage row; respond 200 with X-Hub-Model, X-Hub-Remaining-Out"]
    OUT1 -->|"429 / 5xx"| RETRY["retry with backoff 5s / 15s / 45s"]
    RETRY --> RETRYLEFT{"retries left?"}
    RETRYLEFT -->|yes| ATTEMPT
    RETRYLEFT -->|no| NEXTA["move to next candidate"]
    OUT1 -->|quota error| MARKQ["mark (account, model) exhausted for window scope"] --> NEXTA
    OUT1 -->|too_large| RECCAP["record observed request cap"] --> NEXTA
    OUT1 -->|unsupported param| STRIP["strip param, retry once on same candidate"] --> ATTEMPT
    OUT1 -->|truncated JSON| NEXTA
    OUT1 -->|auth error| NOAUTH["no retry, raise to caller"]

    NEXTA --> HASMORE{"more candidates?"}
    HASMORE -->|yes| ATTEMPT
    HASMORE -->|no| R502A["502 upstream failure: all candidates failed"]
    NOAUTH --> R502B["502 upstream failure: auth error"]

    START -.-> ASYNC["POST /jobs: same body, async"]
    ASYNC --> QUEUED["queued"]
    QUEUED --> WAITQ{"quota available now?"}
    WAITQ -->|no| WAITING["waiting_quota (next_window_at)"] --> WAITQ
    WAITQ -->|yes| RUNNING["running (per-app concurrency limit)"]
    RUNNING --> DONE["done / failed"]
    DONE --> CALLBACK["callback_url POST"]
```

## Scout pipeline

The scout hunts free-tier offers using the hub's own free models, on a schedule
or on demand, and never sets `X-Hub-Allow-Paid`. Collect fetches every source
with no LLM call at all - pepper.pl searches, every vendor's `docs_url` from the
provider catalog, the OpenRouter free model list, and Brave/SearXNG when
configured - through httpx with a page cache keyed by content hash, so an
unchanged page costs nothing on a second run. Extract runs on the `fast` alias
and turns each changed page into JSON offers with an evidence quote. Curate runs
on the strongest free model that currently has quota (the `strong` alias's own
prefer order, so it moves with the registry instead of a hardcoded list),
reading the offers plus the current promos and the catalog's template ids, and
decides new/update/skip per offer - it can ask for one more page before
deciding. Apply writes to the promos table with `source: scout`, merging on
duplicate identity so a row already marked `used` is never rewritten, and a run
report lands in `api/scout/runs`. The manual `/promo-hunt` Claude Code skill is
a separate path for deeper, human-directed digging; it also posts into promos,
just without the daily automation or the page cache.

```mermaid
flowchart LR
    subgraph triggers["Triggers"]
        DAILY["daily schedule LLMHUB_SCOUT_AT"]
        RUNNOW["Run now (Promos tab)"]
        CLIDRY["python -m llmhub scout --dry-run"]
    end

    subgraph sources["Sources"]
        PEPPER["pepper.pl searches"]
        DOCS["catalog docs_url pages"]
        OR["openrouter free model list"]
        WEBSEARCH["optional Brave / SearXNG"]
    end

    subgraph collect["Collect (no LLM)"]
        FETCH["httpx fetch, text extraction"]
        CACHE["page cache by content hash"]
        POLITE["polite: 10s timeout, 3 concurrent, own UA"]
    end

    EXTRACT["Extract on alias `fast`: JSON offers with evidence quotes"]
    CURATE["Curate on strongest free model with quota (alias `strong`): reads offers + current promos + template ids, decides new / update / skip, may ask for one more page"]
    APPLY["Apply: promos table, source=scout, merge on duplicate identity"]
    REPORT["Run report (api/scout/runs)"]

    subgraph skillpath["Manual path"]
        SKILL["/promo-hunt Claude Code skill"]
    end

    PROMOS[("Promos table")]

    DAILY --> FETCH
    RUNNOW --> FETCH
    CLIDRY --> FETCH

    PEPPER --> FETCH
    DOCS --> FETCH
    OR --> FETCH
    WEBSEARCH --> FETCH

    FETCH --> CACHE --> POLITE --> EXTRACT
    EXTRACT --> CURATE
    CURATE --> APPLY
    APPLY --> PROMOS
    APPLY --> REPORT

    SKILL --> PROMOS
```
