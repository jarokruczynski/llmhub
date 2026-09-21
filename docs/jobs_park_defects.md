# Two defects in the job park path

Found 2026-09-21 by triaging `jobs` history in `~/.llmhub/hub.db`. Both are in
`JobWorker.process`. Line numbers are against `c26546e`, the last commit before the fixes.
Fixed on branch `fix/jobs-transient-park-and-backoff` in two independent commits.

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
| 2026-09-12 .. 09-15 | `hub.db.pre-retention-2026-09-16` | 15955 |
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
triage saw a burst beginning on 09-14. The snapshots answer it: 3242 kills on 09-12 and 09-13
with the same signature. The snapshot's own oldest job row is 2026-09-12T10:16 because the
job purge had already run, so the true origin is older than any surviving data. Nothing in
the history suggests the defect was ever absent; it became visible when a high-volume app
started using the queue.

**What the attempt lists contained**, classified by the set of statuses in the blob:

| | kills | share |
| --- | --- | --- |
| transient only (`quota`/`retry`/`unavailable`/`abandoned`) | 14128 | 67.5% |
| transient + `auth` | 3049 | 14.6% |
| at least one hard status (`too_large`, `unsupported_param`, `not_found`, `error`) | 3749 | 17.9% |

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
- `too_large`, `unsupported_param`, `not_found`, `auth` and `error` still fail immediately.
  These are facts waiting cannot change, and a job that is genuinely malformed must not sit in
  the queue for six hours pretending otherwise.
- In particular `auth` still fails, which is the conservative half of the call. A dead key on
  one candidate while the rest of the pool answers `retry` is arguably a fact about that pair
  rather than about the job, and parking those too would recover a further 3049 kills. That is
  a judgement about how loudly a broken key should announce itself, not a bug, so it is left
  alone here. Owner's call.
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

**Tests.** `.venv/bin/python -m pytest -q`: 621 on `main`, 623 after commit 1, 626 after
commit 2. Five new tests in `tests/test_jobs.py` cover the transient park, the `auth` fail, the
parked job not being re-claimed, `forgive` waking it, and `park_at`'s window/backoff choice.
Both commits are green on their own, so either can be taken without the other.

## Not determined

- How many of the 20926 killed jobs would have succeeded on a retry. The request bodies are
  retained but were never re-run, and the vendor state of those minutes is gone.
- Whether the defect existed before 2026-09-12. No job rows survive that far back in any
  snapshot; the retention purge had already removed them when the snapshots were taken.
- The exact tier-1 share, for the truncation reason above. 14128 is an upper bound.
