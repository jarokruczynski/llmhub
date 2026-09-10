from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from llmhub.config import AliasDef, Entry
from llmhub.quota import window_bounds
from llmhub.router import AllCandidatesFailed, Router, UnknownModelError, UpstreamError
from llmhub.runtime import Hub
from llmhub.store import parse_iso
from llmhub.vendor_errors import Classification, classify

from .conftest import entry_of
from .test_vendor_errors import GEMINI_RPD, GROQ_TPD_BODY

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def keys(selection) -> list[tuple[str, str]]:
    return [(entry.key, entry.account_id) for entry in selection.candidates]


def test_alias_selection_order(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    assert keys(selection)[:3] == [
        ("alpha/m1", "alpha-1"),
        ("alpha/m1", "alpha-2"),
        ("beta/m2", "beta-1"),
    ]


def test_alias_spread_defaults(hub: Hub) -> None:
    assert hub.router.spread_for("auto") == 4
    assert hub.router.spread_for("vision") == 4
    assert hub.router.spread_for("alpha/m1") == 1


def test_spread_rotates_over_the_active_set(hub: Hub) -> None:
    seen: list[tuple[str, str]] = []
    for _ in range(5):
        first = hub.router.select(model_request="auto", now=NOW).candidates[0]
        seen.append((first.key, first.account_id))
        hub.router.touch(first)
    assert seen == [
        ("alpha/m1", "alpha-1"),
        ("alpha/m1", "alpha-2"),
        ("beta/m2", "beta-1"),
        ("gamma/fixed", "gamma-1"),
        ("alpha/m1", "alpha-1"),
    ]


def test_spread_keeps_the_rest_of_the_pool_as_fallback(hub: Hub) -> None:
    ordered = hub.router.select(model_request="auto", now=NOW)
    active, tail = keys(ordered)[:4], keys(ordered)[4:]
    assert tail
    for entry in ordered.candidates[:4]:
        hub.router.touch(entry)
    after = hub.router.select(model_request="auto", now=NOW)
    assert sorted(keys(after)[:4]) == sorted(active)
    assert keys(after)[4:] == tail


def test_spread_one_keeps_strict_prefer_order(hub: Hub) -> None:
    hub.registry.aliases["auto"].spread = 1
    hub.router.touch(entry_of(hub, "alpha/m1", "alpha-1"))
    selection = hub.router.select(model_request="auto", now=NOW)
    assert selection.spread == 1
    assert keys(selection)[0] == ("alpha/m1", "alpha-1")


def test_alias_without_a_default_stays_strict(hub: Hub) -> None:
    hub.registry.aliases["local"] = AliasDef(prefer=["beta/m2", "alpha/m1"])
    assert hub.router.spread_for("local") == 1
    hub.router.touch(entry_of(hub, "beta/m2", "beta-1"))
    assert keys(hub.router.select(model_request="local", now=NOW))[0] == ("beta/m2", "beta-1")


def test_header_prefer_beats_least_recently_used(hub: Hub) -> None:
    hub.router.touch(entry_of(hub, "beta/m2", "beta-1"))
    selection = hub.router.select(model_request="auto", prefer=["beta/m2"], now=NOW)
    assert keys(selection)[0] == ("beta/m2", "beta-1")


def test_busy_semaphore_sorts_last_inside_the_active_set(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    object.__setattr__(entry.model, "concurrency", 1)
    semaphore = hub.router.semaphore(entry)
    assert semaphore is not None
    semaphore._value = 0
    selection = hub.router.select(model_request="auto", now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(selection)[:3]


def test_last_used_seeds_from_the_usage_table(hub: Hub) -> None:
    for account, ts in (("alpha-1", "2026-09-07T09:00:00+00:00"), ("alpha-2", "2026-09-07T08:00:00+00:00")):
        hub.store.start_usage(
            app="test",
            provider="alpha",
            account=account,
            model="alpha/m1",
            status="ok",
            latency_ms=1,
            attempt=1,
            ts=ts,
        )
    router = Router(hub.registry, hub.store, hub.quota)
    assert keys(router.select(model_request="auto", now=NOW))[:3] == [
        ("beta/m2", "beta-1"),
        ("gamma/fixed", "gamma-1"),
        ("alpha/m1", "alpha-2"),
    ]


def test_paid_model_blocked_without_header(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/paid1", now=NOW)
    assert selection.candidates == []
    assert selection.rejected_reasons() == {"paid"}

    allowed = hub.router.select(model_request="alpha/paid1", allow_paid=True, now=NOW)
    assert keys(allowed) == [("alpha/paid1", "alpha-1"), ("alpha/paid1", "alpha-2")]


def test_paid_model_never_selected_by_alias(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", allow_paid=False, now=NOW)
    assert all(entry.model.is_free for entry in selection.candidates)


def test_capability_filter(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", require=["vision"], now=NOW)
    assert keys(selection) == [("beta/m2", "beta-1")]
    alias_selection = hub.router.select(model_request="vision", now=NOW)
    assert keys(alias_selection) == [("beta/m2", "beta-1")]


def test_header_prefer_moves_candidate_first(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", prefer=["beta/m2"], now=NOW)
    assert keys(selection)[0] == ("beta/m2", "beta-1")


def test_missing_key_rejected(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_KEY_1")
    selection = hub.router.select(model_request="auto", now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(selection)
    assert "no_key" in selection.rejected_reasons()


def test_disabled_model_rejected(hub: Hub) -> None:
    hub.store.set_model_disabled("alpha/m1", True)
    selection = hub.router.select(model_request="auto", now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(selection)
    assert keys(selection)[0] == ("beta/m2", "beta-1")


def test_exhausted_account_skipped_next_account_used(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hub.quota.mark_exhausted(entry, "insufficient_quota", NOW)
    selection = hub.router.select(model_request="auto", now=NOW)
    assert keys(selection)[0] == ("alpha/m1", "alpha-2")
    assert ("alpha/m1", "alpha-1") not in keys(selection)


def test_window_without_room_is_rejected(hub: Hub) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=1000)
    selection = hub.router.select(model_request="auto", est_out=10, now=None)
    assert ("alpha/m1", "alpha-1") not in keys(selection)
    assert "quota" in selection.rejected_reasons()


def test_quota_rejection_carries_room_and_request_size(hub: Hub) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=200, out_tokens=900)
    selection = hub.router.select(model_request="alpha/m1", est_in=50, est_out=300)
    row = next(
        item for item in selection.rejected if item["reason"] == "quota" and item["account"] == "alpha-1"
    )
    assert row["detail"] == "hourly"
    assert row["remaining_out"] == 100
    assert row["remaining_in"] == 9800
    assert row["requested_out"] == 300
    assert row["requested_in"] == 50
    assert row["resets_at"]


def test_unknown_model(hub: Hub) -> None:
    with pytest.raises(UnknownModelError):
        hub.router.select(model_request="nope/nope")


async def test_run_marks_quota_and_moves_to_next_candidate(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    seen: list[tuple[str, str]] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append((entry.key, entry.account_id))
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, '{"error": {"code": "insufficient_quota"}}'), 429)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    assert seen == [("alpha/m1", "alpha-1"), ("alpha/m1", "alpha-2")]
    assert [item["status"] for item in result.attempts] == ["quota", "ok"]
    assert hub.quota.exhausted_until(entry_of(hub, "alpha/m1", "alpha-1"), datetime.now(UTC)) is not None


async def test_run_retries_same_candidate_with_backoff(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, retry_delays=(5, 15, 45), sleep=fake_sleep)
    selection = router.select(model_request="alpha/m1", now=NOW)
    calls: list[int] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        calls.append(attempt_no)
        if len(calls) < 3:
            raise UpstreamError(Classification("retry", "503", "upstream down"), 503)
        return "served"

    result = await router.run(selection.candidates[:1], call)
    assert result.result == "served"
    assert slept == [5, 15]
    assert len(calls) == 3


async def test_run_gives_up_after_retries_and_falls_back(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, retry_delays=(5, 15, 45), sleep=fake_sleep)
    selection = router.select(model_request="auto", now=NOW)
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.account_id)
        if entry.account_id == "alpha-1":
            raise UpstreamError(Classification("retry", "500", "boom"), 500)
        return "served"

    result = await router.run(selection.candidates, call)
    assert slept == [5, 15, 45]
    assert seen.count("alpha-1") == 4
    assert result.entry.account_id == "alpha-2"
    assert router.in_cooldown(entry_of(hub, "alpha/m1", "alpha-1"), datetime.now(UTC)) is not None


async def test_run_all_candidates_failed(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        raise UpstreamError(Classification("retry", "500", "boom"), 500)

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await hub.router.run(selection.candidates, call)
    assert len(excinfo.value.attempts) == 2


def test_semaphore_is_per_account_and_model(hub: Hub) -> None:
    first = entry_of(hub, "alpha/m1", "alpha-1")
    second = entry_of(hub, "alpha/m1", "alpha-2")
    object.__setattr__(first.model, "concurrency", 1)

    sem_first = hub.router.semaphore(first)
    sem_second = hub.router.semaphore(second)
    assert sem_first is not None and sem_second is not None
    assert sem_first is not sem_second
    assert hub.router.semaphore(entry_of(hub, "alpha/m1", "alpha-1")) is sem_first


async def test_busy_account_sorts_after_free_account(hub: Hub) -> None:
    first = entry_of(hub, "alpha/m1", "alpha-1")
    object.__setattr__(first.model, "concurrency", 1)
    semaphore = hub.router.semaphore(first)
    assert semaphore is not None
    await semaphore.acquire()
    try:
        selection = hub.router.select(model_request="alpha/m1", now=NOW)
        assert keys(selection)[0] == ("alpha/m1", "alpha-2")
    finally:
        semaphore.release()


async def test_run_marks_daily_scope_from_vendor_message(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)
    body = (
        '{"error": {"message": "You\'ve hit the free limit for GPT-6 Astra free daily tier '
        '(375,000 input / 75,000 output tokens per day). It resets at 00:00 UTC.", '
        '"code": "free_limit_reached", "type": "insufficient_quota"}}'
    )

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, body, "explabs"), 429)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"

    entry = entry_of(hub, "alpha/m1", "alpha-1")
    now = datetime.now(UTC)
    until = hub.quota.exhausted_until(entry, now)
    assert until == window_bounds("daily", "UTC", now)[1]
    row = hub.store.query_one(
        "SELECT * FROM exhausted WHERE account = ? AND model = ?", ("alpha-1", "alpha/m1")
    )
    assert row is not None and row["scope"] == "daily"


async def test_quota_error_records_the_observed_cap(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=100, out_tokens=400)
    body = (
        '{"error": {"message": "free daily token allowance exhausted, resets at 00:00 UTC", '
        '"code": "free_limit_reached"}}'
    )

    async def call(candidate: Entry, attempt_no: int) -> str:
        if candidate.account_id == "alpha-1":
            raise UpstreamError(classify(429, body, "explabs"), 429)
        return "served"

    selection = hub.router.select(model_request="alpha/m1")
    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"

    observed = hub.store.observed_limits("alpha-1", "alpha/m1")
    assert observed["daily"]["out_tokens"] == 400
    assert observed["daily"]["observed_at"]
    # the declared daily cap was 5000; 400 is what this account actually got
    assert hub.quota.has_room(entry, 0, 1, datetime.now(UTC)) is False
    assert hub.store.observed_limits("alpha-2", "alpha/m1") == {}


async def test_run_parks_gemini_daily_quota_until_reset_not_the_short_retry_hint(hub: Hub) -> None:
    # Gemini's per-day request-quota body: "retry in 9.026s" next to a
    # GenerateRequestsPerDayPerProjectPerModel-FreeTier violation. 9s is the per-minute
    # pacing underneath the day, not the day, so it must not park the pair for 9 seconds.
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, GEMINI_RPD, "gemini"), 429)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"

    entry = entry_of(hub, "alpha/m1", "alpha-1")
    now = datetime.now(UTC)
    until = hub.quota.exhausted_until(entry, now)
    assert until == window_bounds("daily", "UTC", now)[1]
    row = hub.store.query_one(
        "SELECT * FROM exhausted WHERE account = ? AND model = ?", ("alpha-1", "alpha/m1")
    )
    assert row is not None and row["scope"] == "daily"


async def test_run_parks_groq_tpd_quota_for_the_full_retry_hint(hub: Hub) -> None:
    # groq's TPD body: "try again in 27m44.063999999s" on a rolling daily window - long
    # enough to be naming the window's own reset, so it keeps shortening the park.
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, GROQ_TPD_BODY, "groq"), 429)
        return "served"

    before = datetime.now(UTC)
    result = await hub.router.run(selection.candidates, call)
    after = datetime.now(UTC)
    assert result.entry.account_id == "alpha-2"

    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.exhausted_until(entry, datetime.now(UTC))
    assert until is not None
    hint = timedelta(minutes=27, seconds=44.063999999)
    assert before + hint <= until <= after + hint


def mark_opt_in(hub: Hub, provider: str) -> None:
    hub.registry.providers[provider].opt_in_only = True


def test_opt_in_only_provider_is_absent_from_every_alias(hub: Hub) -> None:
    mark_opt_in(hub, "gamma")
    for alias in ("auto", "vision"):
        selection = hub.router.select(model_request=alias, now=NOW)
        assert not [entry for entry in selection.candidates if entry.provider_name == "gamma"]


def test_opt_in_only_survives_everything_else_being_gone(hub: Hub) -> None:
    """The tail is exactly where an unnamed entry would otherwise be reached."""
    mark_opt_in(hub, "gamma")
    for key in ("alpha/m1", "alpha/paid1", "beta/m2"):
        hub.store.set_model_disabled(key, True)
    selection = hub.router.select(model_request="auto", now=NOW)
    assert selection.candidates == []
    reasons = {row["reason"] for row in selection.rejected}
    assert "opt_in_only" in reasons
    assert [row["detail"] for row in selection.rejected if row["reason"] == "opt_in_only"] == ["auto"] * 4


def test_opt_in_only_is_reachable_when_the_alias_names_it(hub: Hub) -> None:
    mark_opt_in(hub, "gamma")
    hub.registry.aliases["paidish"] = AliasDef(prefer=["gamma/fixed"], spread=1)
    selection = hub.router.select(model_request="paidish", now=NOW)
    assert [entry.key for entry in selection.candidates][:1] == ["gamma/fixed"]
    assert "gamma/openended" not in [entry.key for entry in selection.candidates]


def test_opt_in_only_still_answers_an_explicit_request(hub: Hub) -> None:
    mark_opt_in(hub, "gamma")
    selection = hub.router.select(model_request="gamma/fixed", now=NOW)
    # gamma has two accounts, so an explicit request still fans out over both
    assert {entry.key for entry in selection.candidates} == {"gamma/fixed"}
    assert len(selection.candidates) == 2


def test_opt_in_only_model_knob_overrides_the_provider(hub: Hub) -> None:
    mark_opt_in(hub, "gamma")
    models = {model.id: model for model in hub.registry.providers["gamma"].models}
    models["fixed"].opt_in_only = False
    keys_seen = [entry.key for entry in hub.router.select(model_request="auto", now=NOW).candidates]
    assert "gamma/fixed" in keys_seen
    assert "gamma/openended" not in keys_seen


# --- request size awareness --------------------------------------------------------------


def test_input_ceiling_is_context_minus_output(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")  # context: 100000
    assert entry.input_ceiling(est_out=500, observed=None) == 99500


def test_input_ceiling_takes_the_smallest_known_bound(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")  # context: 100000
    entry.model.max_request_tokens = 8000
    assert entry.input_ceiling(est_out=500, observed=5000) == 5000
    assert entry.input_ceiling(est_out=500, observed=9000) == 8000
    assert entry.input_ceiling(est_out=500, observed=None) == 8000


def test_input_ceiling_ignores_a_non_positive_context_minus_output(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")  # context: 100000
    assert entry.input_ceiling(est_out=100000, observed=None) is None
    entry.model.max_request_tokens = 3000
    assert entry.input_ceiling(est_out=100000, observed=None) == 3000


def test_input_ceiling_unknown_everywhere_is_none(hub: Hub) -> None:
    entry = entry_of(hub, "beta/m2", "beta-1")  # no context, no max_request_tokens
    assert entry.input_ceiling(est_out=500, observed=None) is None


def test_oversized_candidate_is_rejected_and_next_candidate_wins(hub: Hub) -> None:
    hub.registry.providers["alpha"].models[0].max_request_tokens = 50
    selection = hub.router.select(model_request="auto", est_in=200, est_out=10, now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(selection)
    assert keys(selection)[0] == ("beta/m2", "beta-1")
    row = next(item for item in selection.rejected if item["reason"] == "too_large")
    assert row["model"] == "alpha/m1"
    assert row["detail"] == 50
    assert row["requested_in"] == 200


def test_unknown_ceiling_never_rejects(hub: Hub) -> None:
    selection = hub.router.select(model_request="beta/m2", est_in=10_000_000, est_out=10, now=NOW)
    assert keys(selection) == [("beta/m2", "beta-1")]
    assert "too_large" not in selection.rejected_reasons()


async def test_run_records_request_cap_on_too_large_and_moves_to_next_candidate(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    body = (
        '{"error": {"message": "Request too large for model `m1` on tokens per minute (TPM): '
        'Limit 8000, Requested 11152"}}'
    )

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(413, body, "alpha"), 413)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    assert [item["status"] for item in result.attempts] == ["too_large", "ok"]

    cap = hub.store.request_cap("alpha-1", "alpha/m1")
    assert cap is not None
    assert cap["max_request_tokens"] == 8000
    assert cap["source"] == "alpha"
    # not marked exhausted and not quota: only the request cap was recorded
    assert hub.quota.exhausted_until(entry_of(hub, "alpha/m1", "alpha-1"), datetime.now(UTC)) is None


async def test_recorded_request_cap_is_skipped_by_the_next_selection(hub: Hub) -> None:
    hub.store.record_request_cap("alpha-1", "alpha/m1", 8000, source="groq")
    selection = hub.router.select(model_request="alpha/m1", est_in=9000, est_out=10, now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(selection)
    assert ("alpha/m1", "alpha-2") in keys(selection)
    row = next(item for item in selection.rejected if item["account"] == "alpha-1")
    assert row["reason"] == "too_large"
    assert row["detail"] == 8000


async def test_in_flight_registers_the_call_and_clears_it_after(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    seen: list[list[dict]] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(hub.router.in_flight())
        return "served"

    result = await hub.router.run(selection.candidates, call, app="my-app", model_request="auto", est_in=120)
    assert result.call_id > 0
    assert len(seen[0]) == 1
    row = seen[0][0]
    assert (row["app"], row["model"], row["requested"], row["kind"]) == (
        "my-app",
        "alpha/m1",
        "auto",
        "sync",
    )
    assert (row["attempt"], row["job_id"], row["tokens_in_estimate"]) == (1, None, 120)
    assert row["elapsed_s"] >= 0
    assert hub.router.in_flight() == []


async def test_in_flight_follows_the_fallback_and_bumps_the_attempt(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    seen: list[dict] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        rows = hub.router.in_flight()
        assert len(rows) == 1
        seen.append(rows[0])
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, '{"error": {"code": "insufficient_quota"}}'), 429)
        return "served"

    await hub.router.run(selection.candidates, call, app="batch-ocr", model_request="auto")
    assert [(row["account"], row["attempt"]) for row in seen] == [("alpha-1", 1), ("alpha-2", 2)]
    # one call, one entry: the fallback moved it instead of adding a second
    assert {row["call_id"] for row in seen} == {seen[0]["call_id"]}
    assert hub.router.in_flight() == []


async def test_in_flight_clears_when_every_candidate_fails(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        raise UpstreamError(Classification("retry", "500", "boom"), 500)

    with pytest.raises(AllCandidatesFailed):
        await hub.router.run(selection.candidates, call, app="my-app")
    assert hub.router.in_flight() == []


async def test_stream_entry_outlives_run_until_released(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        return "streaming"

    result = await hub.router.run(
        selection.candidates, call, app="my-app", model_request="auto", kind="stream"
    )
    rows = hub.router.in_flight()
    assert [(row["kind"], row["app"]) for row in rows] == [("stream", "my-app")]
    assert hub.router.in_flight_count("alpha/m1") == 1
    hub.router.release(result.call_id)
    assert hub.router.in_flight() == []
    assert hub.router.in_flight_count("alpha/m1") == 0


async def test_in_flight_carries_the_job_id(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)
    seen: list[dict] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(hub.router.in_flight()[0])
        return "served"

    await hub.router.run(selection.candidates, call, app="scout", kind="job", job_id="job_abc")
    assert (seen[0]["kind"], seen[0]["job_id"]) == ("job", "job_abc")


# --- failure handling --------------------------------------------------------------------

NOT_FOUND_BODY = (
    '{"error": {"message": "The model `m1` does not exist or you do not have access to it.", '
    '"type": "invalid_request_error", "code": "model_not_found"}}'
)
UNAVAILABLE_BODY = (
    '{"error": {"type": "server_error", "message": "Error from provider (Console): '
    'Upstream request failed: Model is unavailable."}}'
)
BAD_REQUEST_BODY = '{"error": {"message": "bad request", "code": "invalid_request"}}'


async def test_not_found_parks_the_pair_and_the_next_select_skips_it(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(404, NOT_FOUND_BODY, "alpha"), 404)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    assert [item["status"] for item in result.attempts] == ["not_found", "ok"]

    after = hub.router.select(model_request="alpha/m1", now=datetime.now(UTC))
    assert ("alpha/m1", "alpha-1") not in keys(after)
    row = next(item for item in after.rejected if item["account"] == "alpha-1")
    assert row["reason"] == "unavailable"
    assert row["detail"] == "not_found: model_not_found"
    assert row["until"]
    events = hub.store.query("SELECT * FROM events WHERE kind = 'unavailable'")
    assert len(events) == 1
    assert "not_found model_not_found" in events[0]["message"]


async def test_not_found_is_parked_for_days_and_unavailable_for_minutes(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    now = datetime.now(UTC)
    hub.router.mark_unavailable(entry, Classification("unavailable", "server_error", "down"), now)
    short = hub.store.unavailable_until("alpha-1", "alpha/m1", now)
    assert short is not None
    assert parse_iso(short["until_ts"]) - now == timedelta(seconds=600)

    hub.router.mark_unavailable(entry, Classification("not_found", "model_not_found", "nope"), now)
    long = hub.store.unavailable_until("alpha-1", "alpha/m1", now)
    assert long is not None
    assert parse_iso(long["until_ts"]) - now == timedelta(days=7)


async def test_an_expired_unavailable_row_re_admits_the_pair(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    past = datetime.now(UTC) - timedelta(minutes=1)
    hub.store.mark_unavailable("alpha-1", "alpha/m1", "unavailable", "server_error", "down", past)
    selection = hub.router.select(model_request="alpha/m1", now=datetime.now(UTC))
    assert ("alpha/m1", "alpha-1") in keys(selection)
    assert hub.store.unavailable_all() == {}
    assert entry.account_id == "alpha-1"


async def test_a_successful_call_clears_a_pair_this_process_parked(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hub.router.mark_unavailable(entry, Classification("unavailable", "server_error", "down"))
    # the owner re-admitted it from the dashboard: the memory marker is what makes the next
    # ok clean the row up rather than leaving a stale one behind
    hub.store.mark_unavailable(
        "alpha-1", "alpha/m1", "unavailable", "server_error", "down", datetime.now(UTC) - timedelta(seconds=1)
    )

    async def call(candidate: Entry, attempt_no: int) -> str:
        return "served"

    selection = hub.router.select(model_request="alpha/m1", now=datetime.now(UTC))
    await hub.router.run(selection.candidates, call)
    assert hub.store.unavailable_all() == {}
    assert ("alpha-1", "alpha/m1") not in hub.router._unavailable


async def test_a_vendor_error_moves_to_the_next_candidate(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(400, BAD_REQUEST_BODY, "alpha"), 400)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    assert [item["status"] for item in result.attempts] == ["error", "ok"]


def add_fourth_provider(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture registry has three providers; the stop rule needs a fourth to be visible."""
    monkeypatch.setenv("DELTA_KEY", "test-key-delta")
    data = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    data["providers"]["delta"] = {
        "kind": "openai",
        "base_url": "https://delta.test/v1",
        "accounts": [{"id": "delta-1", "api_key_env": "DELTA_KEY"}],
        "models": [{"id": "m4", "caps": ["text"], "free": {}}],
    }
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    hub.reload()


async def test_three_providers_saying_error_stop_the_run(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    add_fourth_provider(hub, monkeypatch)
    selection = hub.router.select(model_request="auto", now=NOW)
    assert len({entry.provider_name for entry in selection.candidates}) > 3
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.provider_name)
        raise UpstreamError(classify(400, BAD_REQUEST_BODY, entry.provider_name), 400)

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await hub.router.run(selection.candidates, call)
    assert len(set(seen)) == 3
    assert len(seen) < len(selection.candidates)
    assert {item["status"] for item in excinfo.value.attempts} == {"error"}


async def test_a_single_retry_then_the_next_candidate(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, sleep=fake_sleep)
    selection = router.select(model_request="auto", now=NOW)
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.account_id)
        if entry.account_id == "alpha-1":
            raise UpstreamError(Classification("retry", "500", "boom"), 500)
        return "served"

    result = await router.run(selection.candidates, call)
    assert slept == [2.0]
    assert seen.count("alpha-1") == 2
    assert result.entry.account_id == "alpha-2"


async def test_a_long_retry_after_becomes_a_cooldown_without_sleeping(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, sleep=fake_sleep)
    selection = router.select(model_request="auto", now=NOW)
    body = '{"error": {"message": "slow down, please try again in 5m0s"}}'

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(503, body, "alpha"), 503)
        return "served"

    result = await router.run(selection.candidates, call)
    assert slept == []
    assert result.entry.account_id == "alpha-2"
    now = datetime.now(UTC)
    until = router.in_cooldown(entry_of(hub, "alpha/m1", "alpha-1"), now)
    assert until is not None
    assert 290 < (until - now).total_seconds() <= 300


async def test_a_short_retry_after_is_slept_instead_of_the_default_delay(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, sleep=fake_sleep)
    selection = router.select(model_request="alpha/m1", now=NOW)
    body = '{"error": {"message": "please try again in 1.5s"}}'
    calls: list[int] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        calls.append(attempt_no)
        if len(calls) == 1:
            raise UpstreamError(classify(503, body, "alpha"), 503)
        return "served"

    result = await router.run(selection.candidates, call)
    assert slept == [1.5]
    assert result.result == "served"


async def test_the_budget_stops_the_run_before_the_next_candidate(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.account_id)
        raise UpstreamError(Classification("retry", "500", "boom"), 500)

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await hub.router.run(selection.candidates, call, budget_s=0.0)
    # the first candidate always runs; the budget stops everything after it
    assert len(seen) == 1
    assert excinfo.value.budget_exhausted is True


async def test_the_budget_skips_a_retry_sleep_it_cannot_afford(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, retry_delays=(5.0,), sleep=fake_sleep)
    selection = router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        raise UpstreamError(Classification("retry", "500", "boom"), 500)

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.run(selection.candidates, call, budget_s=1.0)
    assert slept == []
    assert excinfo.value.budget_exhausted is True


async def test_no_budget_walks_the_whole_pool(hub: Hub) -> None:
    selection = hub.router.select(model_request="auto", now=NOW)
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.key)
        raise UpstreamError(Classification("retry", "500", "boom"), 500)

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await hub.router.run(selection.candidates, call, budget_s=None)
    assert len(seen) == len(selection.candidates)
    assert excinfo.value.budget_exhausted is False


async def test_groq_daily_rate_limit_exhausts_without_retrying(hub: Hub) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    router = Router(hub.registry, hub.store, hub.quota, sleep=fake_sleep)
    selection = router.select(model_request="alpha/m1", now=NOW)
    body = (
        '{"error": {"message": "Rate limit reached for model `m1` on tokens per day (TPD): '
        'Limit 200000, Used 195998, Requested 7854. Please try again in 27m44.063999999s.", '
        '"code": "rate_limit_exceeded"}}'
    )
    seen: list[str] = []

    async def call(entry: Entry, attempt_no: int) -> str:
        seen.append(entry.account_id)
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, body, "alpha"), 429)
        return "served"

    result = await router.run(selection.candidates, call)
    assert slept == []
    assert seen == ["alpha-1", "alpha-2"]
    assert result.entry.account_id == "alpha-2"
    row = hub.store.query_one(
        "SELECT * FROM exhausted WHERE account = ? AND model = ?", ("alpha-1", "alpha/m1")
    )
    assert row is not None and row["scope"] == "daily"
    # the vendor named a wait shorter than the rest of the day, so that is when it comes back
    until = parse_iso(row["until_ts"])
    assert (until - datetime.now(UTC)).total_seconds() < 30 * 60


async def test_an_output_cap_is_recorded_and_the_next_select_rejects_a_bigger_request(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", est_out=300, now=NOW)
    body = (
        '{"error": {"message": "Request too large for model `m1` on output tokens per minute '
        '(OTPM): Limit 100, Requested 300"}}'
    )

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(429, body, "alpha"), 429)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    cap = hub.store.request_cap("alpha-1", "alpha/m1")
    assert cap is not None
    assert cap["max_out_tokens"] == 100
    # the out cap is its own axis: nothing was learned about the size of the prompt
    assert cap["max_request_tokens"] is None

    after = hub.router.select(model_request="alpha/m1", est_out=300, now=NOW)
    assert ("alpha/m1", "alpha-1") not in keys(after)
    row = next(item for item in after.rejected if item["account"] == "alpha-1")
    assert row["reason"] == "too_large"
    assert row["detail"] == {"max_out_tokens": 100}
    assert row["requested_out"] == 300
    # a request that fits under the cap is served by the same pair
    fits = hub.router.select(model_request="alpha/m1", est_out=50, now=NOW)
    assert ("alpha/m1", "alpha-1") in keys(fits)


async def test_a_stream_run_handles_failures_the_same_way(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(404, NOT_FOUND_BODY, "alpha"), 404)
        return "streaming"

    result = await hub.router.run(selection.candidates, call, kind="stream")
    assert result.entry.account_id == "alpha-2"
    assert hub.store.unavailable_until("alpha-1", "alpha/m1", datetime.now(UTC)) is not None
    # a stream stays in flight until the body generator releases it
    assert hub.router.in_flight_count("alpha/m1") == 1
    hub.router.release(result.call_id)


async def test_unavailable_is_a_short_park_and_the_run_continues(hub: Hub) -> None:
    selection = hub.router.select(model_request="alpha/m1", now=NOW)

    async def call(entry: Entry, attempt_no: int) -> str:
        if entry.account_id == "alpha-1":
            raise UpstreamError(classify(400, UNAVAILABLE_BODY, "zen"), 400)
        return "served"

    result = await hub.router.run(selection.candidates, call)
    assert result.entry.account_id == "alpha-2"
    row = hub.store.unavailable_until("alpha-1", "alpha/m1", datetime.now(UTC))
    assert row is not None
    assert row["kind"] == "unavailable"
    assert (parse_iso(row["until_ts"]) - datetime.now(UTC)).total_seconds() <= 600


def test_forgiving_a_model_clears_its_parked_rows(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hub.router.mark_unavailable(entry, Classification("not_found", "model_not_found", "nope"))
    assert hub.router.clear_unavailable("alpha/m1") == 1
    assert hub.store.unavailable_all() == {}
    assert hub.router._unavailable == set()
