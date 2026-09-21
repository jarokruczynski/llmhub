# Two defects in the job park path

Found 2026-09-21 by triaging `jobs` history in `~/.llmhub/hub.db`. Both are in
`JobWorker.process`. Line numbers are against `c26546e`, the last commit before the fixes.
Fixed in three commits, each green on its own: the transient park, the backoff, and the `auth`
widening with the dead-key reporting that pays for it.

The pre-09-14 figures below come from a snapshot of `hub.db` taken 2026-09-16, **since
deleted** - it held 09-12 to 09-16, which the live db's 7-day retention has long dropped. The
numbers were read out of it before it went; the file is gone and there is nothing to go
looking for.

## Defect 1 - a transient pool-wide refusal failed the job permanently

**Symptom.** Jobs died as `failed` with `error` = `upstream failure: [{...}]` while every
candidate in the list had only refused for a reason that would be gone a minute later.
Clients saw a terminal state and stopped retrying.

**Mechanism.** `llmhub/jobs.py:581-585`:

```python
except (NoCandidatesError, AllCandidatesFailed) as exc:
    if isinstance(exc, AllCandidatesFailed) and not all(
        attempt["status"] == "quota" for attempt in exc.attempts
    ):
        return self._fail(job_id, f"upstream failure: {exc.attempts}")
```

The park below this branch is reached only when *every* attempt was classified `quota`. One
attempt with any other status sends the job to `_fail`, which is terminal.

What makes that fatal is `llmhub/vendor_errors.py:669-670`:

```python
if detail_window == "minute":
    return Classification("retry", code, message, "quota-pacing", quota_detail=detail)
```

A 429 that names a per-minute cap is deliberately *not* `quota` - it is pacing, not a spent
budget, and there is no window for the hub to park on. That decision is right on its own. The
consequence is that the single most common refusal in a free pool - "too fast, wait a few
seconds" - carries a status that the branch above treats as a permanent judgement on the job.
`retry` is also what a 503 and a transport error classify as, so a vendor blip has the same
effect.

The two behaviours are individually defensible and jointly wrong: a whole pool saying "not
this second" became "this job can never run".

**Contradicts the contract.** `docs/CLIENT_PROMPT.md:248-250` promises:

> `waiting_quota` (no free candidate right now, `next_window_at` set, job is retried
> automatically - a job is never failed for lack of quota)

and `docs/DESIGN.md:217`: "Never fails a job for lack of quota - but it does expire one at its
deadline." 11471 of the failures carried error code 429.

**Scale.** Every `failed` row in seven days of live history came from this one branch - there
is no other failure reason in the database. Counting the 09-16 snapshot for the days retention
has since dropped, and the live db from 09-16 on:

| period | source | kills |
| --- | --- | --- |
| 2026-09-12 .. 09-15 | snapshot of 2026-09-16, since deleted | 15955 |
| 2026-09-16 .. 09-21 | live `hub.db` | 4971 |
| **total** | | **20926** |

Per day, per app:

| day | ytsb | detektor |
| --- | --- | --- |
| 09-12 | 105 | - |
| 09-13 | 3137 | - |
| 09-14 | 6492 | - |
| 09-15 | 6213 | 8 |
| 09-16 | 2917 | - |
| 09-17 | 2007 | - |
| 09-18 | 26 | - |
| 09-19 | 8 | 5 |
| 09-20 | - | 3 |
| 09-21 | - | 5 |

The `ytsb` collapse ends on 09-17, when that app was given its own hourly budget and stopped
saturating the pool. **The defect did not end with it** - `detektor` is still losing jobs to
it, most recently 2026-09-21T06:50, on two 503s from a pinned model.

**It predates 09-14.** The live db's 7-day retention starts there, which is why the first
triage saw a burst beginning on 09-14. The 09-16 snapshot answered it before it was deleted:
3242 kills on 09-12 and 09-13 with the same signature. That snapshot's own oldest job row was
2026-09-12T10:16, because the job purge had already run against it, so the true origin is
older than any data that ever survived. Nothing in
the history suggests the defect was ever absent; it became visible when a high-volume app
started using the queue.

**What the attempt lists contained**, classified by the set of statuses in the blob:

| | kills | share | after the fix |
| --- | --- | --- | --- |
| transient only (`quota`/`retry`/`unavailable`/`abandoned`) | 14128 | 67.5% | parks |
| transient + `auth` | 3049 | 14.6% | parks (commit 3) |
| at least one hard status (`too_large`, `unsupported_param`, `not_found`, `error`) | 3749 | 17.9% | still fails |

So 17177 of the 20926, 82%, were jobs the queue should have retried and did not.

Caveat: `_fail` truncates `error` at 1000 characters (`jobs.py:639`), which holds roughly the
first seven attempts of a pool walk. A later attempt with a hard status is invisible, so the
first row is an upper bound. It is not a small effect on the margins, but the 429-only and
503-only blobs that dominate the sample are short enough to be complete.

## Defect 2 - a parked job was re-attempted on every poll

**Symptom.** Single jobs accumulating thousands of attempts inside one lifetime, and the
vendor traffic to match.

**Mechanism.** `llmhub/jobs.py:592-593`:

```python
blind = not next_window or pool_is_windowless(self.hub, model_request)
next_attempt = to_iso(backoff_at(utcnow(), attempts)) if blind else None
```

When the pool declares a `next_window_at`, `next_attempt_at` is deliberately left `None` - the
intent being that the job wakes when the window resets. But the dispatcher never reads
`next_window_at`. `Store.claimable_jobs` (`llmhub/store.py:1258-1269`) filters on
`next_attempt_at` alone:

```sql
WHERE j.state IN ('queued', 'waiting_quota') AND COALESCE(a.paused, 0) = 0
  AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
```

`NULL` means claimable. So the park that was meant to sleep until the window instead made the
job eligible on the next poll, 2 s later (`JobQueue.poll_interval`, `jobs.py:144`), and every
poll after that until its deadline.

**Scale.** The worst case is job `job_a414435574b3`: 10204 attempts between 2026-09-21T00:08
and its expiry at 06:09. That is 21645 s / 10204 = one attempt every 2.12 s - the poll
interval exactly, confirming the job was re-claimed on literally every pass. Three sibling
jobs from the same batch reached 10203, 10200 and 10189. Across the live db: 56 `expired` rows
over 100 attempts, 11 over 1000. Six jobs were sitting in `waiting_quota` at 990-1092 attempts
while this was being written, so the defect is live, not historical.

All of the worst spinners have `next_window_at` set and `next_attempt_at` empty, which is the
predicted fingerprint.

**The two defects compound.** Each re-attempt is another chance to draw a `retry` from the
pool, and one draw is enough to trigger defect 1. The `detektor` job that died on 09-21T06:50
was on attempt 34 after 103 seconds: it had parked cleanly 33 times, then hit a 503 and was
failed. Without the spin it would have had two or three chances to be unlucky instead of
thousands.

## The fix

**Commit 1 - `jobs: a pool-wide transient refusal parks the job, it does not fail it`.**

Replaces the all-`quota` test with a set of statuses that say nothing about the request:

```python
TRANSIENT_ATTEMPT_STATUSES = frozenset({"quota", "retry", "unavailable", "abandoned"})
```

`abandoned` is included because it means the run hit its wall-clock budget before walking the
rest of the pool - the pool never judged the request at all.

What it deliberately does **not** change:

- `quota` still parks exactly as before, so the guarantee that already worked is untouched.
- A job is still bounded. A parked job expires at its ttl (6 h by default, `deadline_of`) and
  leaves as `expired` with a reason, which is the state the design already defines for "never
  got a slot". Nothing spins past its deadline.
- `too_large`, `unsupported_param`, `not_found` and `error` still fail immediately. These are
  facts waiting cannot change, and a job that is genuinely malformed must not sit in the queue
  for six hours pretending otherwise.
- `auth` still failed after this commit; commit 3 is what changed that.
- `vendor_errors.py` is not touched. Classifying a per-minute 429 as `retry` is correct; the
  defect was in what the job worker concluded from it.

**Commit 2 - `jobs: every park carries a backoff, not only the windowless ones`.**

`park_at(now, attempts, next_window)` gives every park a `next_attempt_at`: the existing
backoff ladder (1, 5, 15, 30 min, then 30), pulled earlier when a declared window opens before
it, and never set in the past.

The window is treated as a floor, not a ceiling, on purpose. Sleeping until `next_window_at`
and no sooner would be the smaller change, but a window can free ahead of its schedule - an
account resets, a key is added, quota is forgiven - and a daily window would park a job for 24
h, well past a 6 h ttl, so it would expire having never retried once. Re-probing on the
backoff keeps the original poll-to-discover behaviour at a sane rate: about a dozen attempts over a
6 h life instead of 10204.

Because a parked job now genuinely sleeps, `POST /api/models/{key}/forgive` clears
`next_attempt_at` on parked jobs and reports `woken` in its response. Without that, an operator
saying "this model works again" would have been made to wait out a backoff - a behaviour
change this fix would otherwise have introduced silently.

**Commit 3 - `jobs: one dead key does not fail a job the rest of the pool was only pacing`.**

Widens the park to tolerate `auth` - but only in company. `parks_the_job(attempts)` now
requires that every status be one the hub can wait out *and* that at least one of them be
genuinely transient:

```python
statuses <= TRANSIENT_ATTEMPT_STATUSES | TOLERATED_WITH_TRANSIENT
and statuses & TRANSIENT_ATTEMPT_STATUSES
```

So `retry + auth` parks and bare `auth` still fails. That line matters: a pool whose only
answer was "this key is not accepted" has no working key for the request, and a retry in
thirty minutes meets the same wall. Parking it would be the same defect in a new place.
Recovers the 3049 kills counted above as the `auth` tier.

**Keeping a dead key loud.** Before this, a broken key announced itself by killing jobs. That
was terrible as a notification and is now gone, so the signal moves to the pair: an `auth`
refusal marks the pair unavailable for `LLMHUB_UNAVAILABLE_TTL_S` (600 s, `router.py`, in the
same place `not_found` and `unavailable` already do it) and writes an `unavailable` event with
the vendor's own code and message. Three reasons that is the right place rather than a new
mechanism: the pair drops out of the pool so it stops being tried, `api/status` already reports
unavailable pairs, and `unavailable` is not in `NOISY_EVENT_KINDS`, so the row purge keeps it
while the `fallback` event `give_up` writes is swept away. `forgive` already clears it, and the
600 s ttl means a key that was only briefly unprovisioned heals itself.

The cooldown `give_up` sets for `auth` is deliberately left in place on top of the new parking:
`tests/test_cli_backend.py` asserts a copilot auth failure puts the model in cooldown, and the
copilot login hint keys off it. Swapping one parking mechanism for the other would have been
the tidier diff and a silent regression.

**Tests.** `.venv/bin/python -m pytest -q`: 621 on `main`, 623 after commit 1, 626 after
commit 2, 628 after commit 3. Eight new tests in `tests/test_jobs.py` cover the transient park,
the `auth`-alone failure, the parked job not being re-claimed, `forgive` waking it, `park_at`'s
window/backoff choice, the `parks_the_job` truth table, and a dead key still being parked and
reported while the job survives. Each commit is green on its own.

## Not determined

- How many of the 20926 killed jobs would have succeeded on a retry. The request bodies are
  retained but were never re-run, and the vendor state of those minutes is gone.
- Whether the defect existed before 2026-09-12. No job rows went back that far even in the
  09-16 snapshot; the retention purge had already removed them when it was taken, and the
  snapshot itself has since been deleted.
- The exact tier-1 share, for the truncation reason above. 14128 is an upper bound.
