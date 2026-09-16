# Studio v2 - requested changes

Owner's review of the v2 console after the first day of use. Nine items, in the owner's order.
Each one says what is wrong, what "done" means, and where in the code to start. Findings marked
*verified* were checked against the current tree, not guessed.

## Ground rules for all of it

- **Never root a request at the server.** The page is served at `/v2` on this Mac and at
  `/hub/v2` through the LAN proxy, which strips the `/hub` prefix. Everything goes through
  `apiUrl()` in `v2.js`; a bare `/api/...` or `/v1/...` path loads an empty page from any device
  other than this Mac. Same for links: relative only, `<base href="./">` is already set.
- **The repository is public.** No keys, no account names, no personal data, no `/Users/...`
  paths in anything committed.
- `.venv/bin/python -m pytest -q` must stay green, `tests/test_dashboard_v2.py` included.
- The classic dashboard keeps working and keeps its own look; v2 is a second console, not a
  replacement, and the two link to each other.

## 1. "Live Stream Activity" shows nothing

*Verified*: the panel is dead markup. `v2.html` has a `#overview-live-summary` div holding one
hardcoded sentence, and `v2.js` never references that id. Nothing can ever appear there.

Either delete the panel, or give it the data it promises. The data exists: `GET api/live` returns
one row per app with its in-flight calls, each carrying `call_id`, `state` (`waiting` for a
concurrency slot, `running` against the vendor), model, account and `job_id`, plus
`in_flight_total`. `POST api/live/{call_id}/cancel` ends one call and `POST api/live/cancel`
ends an app's or everything.

Done when: the panel either no longer exists, or shows current calls with their state and lets
one be cancelled, and says plainly when nothing is running instead of implying activity.

## 2. Layout breaks on resize, and data hides in a wide window

Resizing the window wrecks the layout, and in other cases a wide window still does not show all
columns. Both are the same bug: fixed widths and no overflow strategy.

Done when: every panel, form and table is usable from a phone-width window up to a wide desktop,
with no horizontal scrolling of the page itself. Wide tables scroll inside their own container,
never by moving the page. Nothing is clipped or hidden at any width, and a table that cannot fit
its columns says which ones it dropped rather than silently cutting them.

Note the console is opened from a phone over the LAN, so narrow widths are a normal case, not an
edge case.

## 3. Tables sort by clicking the column name

Every table. Click the header to sort, click again to reverse. The current sort must be visible
in the header, and sorting must not lose an active filter.

## 4. "Routing & Matrix" is unexplained, and the profiles should be editable

*Verified*: the tab renders "Configured Routing Aliases" plus an interactive routing simulator.
The owner's guess is right - an alias is a routing profile: a named group of models with a
preference order and a `spread`, e.g. `auto`, `fast`, `vision`, `strong`. They live in
`~/.llmhub/providers.yaml` under `aliases:`, each with `spread` and a `prefer` list of
`provider/model` entries, and they are what a client asks for with `"model": "auto"`.

Two things to do. First, say this on the page: the tab must explain what an alias is and what
`spread` and `prefer` do, in a sentence, so it is not guesswork.

Second, make them editable. *Verified*: no alias-editing endpoint exists today. The registry is
already written from code - `RegistryWriter` in `llmhub/accounts.py` is how quick-add persists
providers and accounts - so reuse it rather than writing YAML by hand, and reload through the
existing `POST api/registry/reload`. Reordering `prefer`, adding and removing a model, and
changing `spread` are the operations that matter. Validate against the live registry: an alias
that names a model no entry provides must be refused, not written.

The owner also asked whether a scout could fill these profiles. Keep that separate from hand
editing: a suggestion the owner accepts, never a silent rewrite of a file they curate.

## 5. Table view and card view, switchable

Wherever a list is shown, offer both a dense table and a card layout, and let the view be
switched. Remember the choice per list on the device (it is a per-viewer convenience, so browser
storage is the right place). Cards are what make the console usable on a phone; tables are what
make it usable on a desk.

## 6. Something must test account and model availability on a schedule

*Verified*: the scout today hunts promotions; it does not check health. The pieces for checking
already exist and must be reused rather than rebuilt: `POST api/accounts/{provider}/{account}/test`
probes one account, `python -m llmhub check` prints the registry and quota state, the router
already parks a pair as `unavailable` when a vendor refuses it, and quota windows already track
what is exhausted.

The hard constraint is cost: every probe spends one request against a free allowance, and some
allowances are small - the Gemini CLI plan counts whole requests, 1500 a day on the paid plan.
So a sweep must be paced, must skip pairs that are already parked or exhausted, and must be
visible on the page as "last checked" per account rather than run silently in a loop.

Done when: each account and model shows when it was last verified and what happened, the sweep
runs on a schedule, and its request cost is bounded and stated.

## 7. "Promos & Scout" has no filtering

Add filtering, consistent with whatever filtering pattern items 3 and 5 settle on. At minimum:
by status, by vendor, and free-text over the name.

## 8. Token usage: a table instead of the trend chart

Replace the "Token usage trend" chart with a table of how many tokens each model actually used,
filterable and sortable like every other table.

The data is already recorded per call: the usage rows carry app, provider, account, model, input,
output, cached and total tokens, and `api/usage` aggregates them (it takes `group_by`). Per-model
totals are the required grouping; keep the period selectable.

## 9. Estimated savings from routing through the hub

Show what the traffic would have cost if it had gone to a paid vendor instead of the free routes
the hub picked.

*Verified*: no price data exists anywhere in the registry or the catalog - this is the one item
that needs a new data source, not just a new view. It needs a per-model price table (input and
output, per million tokens) and a stated baseline, because "saved" only means something against
a named comparison: the same tokens priced at a specific paid model, not at each provider's own
list price, since most of the models used here have no paid tier at all.

Be honest in the UI about what the number is: an estimate, against a named baseline, over a
stated period. A single headline figure with no baseline is worse than no figure.

Token counts for the arithmetic are already in the usage rows, and note that cached input is
billed differently by most vendors, so it must not be counted at the full input rate.
