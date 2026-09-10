from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from llmhub.config import QuotaWindow
from llmhub.quota import estimate_request_cost, window_bounds
from llmhub.runtime import Hub
from llmhub.store import to_iso
from llmhub.vendor_errors import Classification, classify

from .conftest import entry_of, token_quota


def test_hourly_window_top_of_hour() -> None:
    now = datetime(2026, 9, 7, 13, 42, 17, tzinfo=UTC)
    start, end = window_bounds("hourly", "UTC", now)
    assert start == datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 7, 14, 0, tzinfo=UTC)


def test_daily_window_in_local_tz() -> None:
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    start, end = window_bounds("daily", "Europe/Warsaw", now)
    assert start == datetime(2026, 9, 6, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 7, 22, 0, tzinfo=UTC)


def test_daily_window_dst_shift() -> None:
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    start, end = window_bounds("daily", "Europe/Warsaw", winter)
    assert start == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 1, 15, 23, 0, tzinfo=UTC)


def test_daily_window_before_local_midnight_rolls_to_next_day() -> None:
    now = datetime(2026, 9, 6, 23, 30, tzinfo=UTC)
    start, end = window_bounds("daily", "Europe/Warsaw", now)
    assert start == datetime(2026, 9, 6, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 7, 22, 0, tzinfo=UTC)


def test_monthly_window_wraps_year() -> None:
    now = datetime(2026, 12, 20, 8, 0, tzinfo=UTC)
    start, end = window_bounds("monthly", "UTC", now)
    assert start == datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


def test_usage_accumulates_from_usage_table(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=10,
        attempt=1,
        ts=to_iso(datetime(2026, 9, 7, 10, 5, tzinfo=UTC)),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=100, out_tokens=900)

    state = hub.quota.window_state(entry, "hourly", entry.model.windows["hourly"], now)
    assert state.used == 900
    assert state.limit == 1000
    assert state.metric == "out_tokens"
    assert state.resets_at == datetime(2026, 9, 7, 11, 0, tzinfo=UTC)

    assert hub.quota.has_room(entry, 10, 50, now) is True
    assert hub.quota.has_room(entry, 10, 100, now) is True
    assert hub.quota.has_room(entry, 10, 101, now) is False


def test_usage_before_window_start_is_ignored(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=10,
        attempt=1,
        ts=to_iso(datetime(2026, 9, 7, 9, 59, tzinfo=UTC)),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=999)
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    state = hub.quota.window_state(entry, "hourly", entry.model.windows["hourly"], now)
    assert state.used == 0


def test_mark_exhausted_until_next_reset_and_forgive(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(entry, "insufficient_quota", now)
    assert until == datetime(2026, 9, 7, 11, 0, tzinfo=UTC)
    assert hub.quota.exhausted_until(entry, now) == until
    assert hub.quota.exhausted_until(entry, datetime(2026, 9, 7, 11, 1, tzinfo=UTC)) is None

    hub.quota.mark_exhausted(entry, "insufficient_quota", now)
    assert hub.quota.forgive("alpha/m1") == 1
    assert hub.quota.exhausted_until(entry, now) is None


def test_estimate_request_cost() -> None:
    body = {"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 256}
    assert estimate_request_cost(body) == (100, 256)
    body_default = {"messages": [{"role": "user", "content": "x" * 8}]}
    assert estimate_request_cost(body_default) == (2, 4096)


def spend_out(hub: Hub, ts: datetime, out_tokens: int) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=5,
        attempt=1,
        ts=to_iso(ts),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=out_tokens)


def exhausted_row(hub: Hub, entry) -> dict:
    row = hub.store.query_one(
        "SELECT * FROM exhausted WHERE account = ? AND model = ?", (entry.account_id, entry.key)
    )
    assert row is not None
    return row


def last_quota_event(hub: Hub) -> str:
    rows = hub.store.query("SELECT message FROM events WHERE kind = 'quota' ORDER BY id DESC LIMIT 1")
    return rows[0]["message"]


def test_daily_scope_holds_until_next_midnight(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(entry, "hit the free limit, resets at 00:00 UTC", now, scope="daily")
    assert until == datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "daily"
    assert "(daily)" in last_quota_event(hub)
    assert hub.quota.exhausted_until(entry, datetime(2026, 9, 7, 14, 1, tzinfo=UTC)) == until


def test_short_retry_hint_does_not_shorten_a_daily_park(hub: Hub) -> None:
    # Gemini's per-day quota body: "retry in 9.026s" next to a request-per-day violation.
    # 9s describes the per-minute pacing underneath the day, not the day itself.
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(
        entry, "quota exceeded", now, scope="daily", until=now + timedelta(seconds=9.026)
    )
    assert until == datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "daily"


def test_long_retry_hint_still_shortens_a_daily_park(hub: Hub) -> None:
    # groq's TPD body: "try again in 27m44s" on a rolling daily window - long enough to be
    # naming the window's own reset, so it keeps shortening the park as designed.
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    hint = now + timedelta(minutes=27, seconds=44)
    until = hub.quota.mark_exhausted(entry, "rate_limit_exceeded", now, scope="daily", until=hint)
    assert until == hint
    assert exhausted_row(hub, entry)["scope"] == "daily"


def test_allowance_scope_without_expiry_notes_no_reset(hub: Hub) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    entry = entry_of(hub, "gamma/openended", "gamma-1")
    until = hub.quota.mark_exhausted(entry, "insufficient balance", now, scope="allowance")
    assert until == datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "allowance"
    assert "no reset known" in last_quota_event(hub)


def test_allowance_scope_uses_declared_expiry(hub: Hub) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    until = hub.quota.mark_exhausted(entry, "insufficient balance", now, scope="allowance")
    assert until == datetime(2026, 12, 1, tzinfo=UTC)
    assert "no reset known" not in last_quota_event(hub)


def test_undeclared_scope_falls_back_to_earliest_window(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(entry, "monthly cap reached", now, scope="monthly")
    assert until == datetime(2026, 9, 7, 14, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "hourly"


def test_no_scope_hint_keeps_earliest_window(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    until = hub.quota.mark_exhausted(entry, "insufficient_quota", now)
    assert until == datetime(2026, 9, 7, 14, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "hourly"


def test_saturated_daily_window_wins_over_hourly(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    spend_out(hub, datetime(2026, 9, 7, 2, 0, tzinfo=UTC), 5000)
    assert hub.quota.window_at_limit(entry, "hourly", entry.model.windows["hourly"], now) is False
    assert hub.quota.saturated_scope(entry, now) == "daily"
    until = hub.quota.mark_exhausted(entry, "insufficient_quota", now)
    assert until == datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    assert exhausted_row(hub, entry)["scope"] == "daily"


def test_room_reports_per_window_remaining_and_binding_minimum(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    spend_out(hub, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 900)

    room = hub.quota.room(entry, now)
    assert room["windows"]["hourly"] == {
        "metric": "out_tokens",
        "remaining": 100,
        "resets_at": to_iso(datetime(2026, 9, 7, 11, 0, tzinfo=UTC)),
    }
    assert room["windows"]["daily"]["remaining"] == 4100
    assert room["remaining_out"] == 100
    assert room["remaining_in"] == 10000
    assert room["binding_window"] == "hourly"
    assert room["resets_at"] == to_iso(datetime(2026, 9, 7, 11, 0, tzinfo=UTC))


def test_room_without_declared_limits_has_no_numbers(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    room = hub.quota.room(entry_of(hub, "beta/m2", "beta-1"), now)
    assert room["windows"] == {}
    assert room["remaining_out"] is None
    assert room["remaining_in"] is None
    assert room["binding_window"] is None


def test_room_never_goes_negative(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    spend_out(hub, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 1500)
    room = hub.quota.room(entry, now)
    assert room["remaining_out"] == 0
    assert hub.quota.has_room(entry, 0, 1, now) is False


def spend_for(hub: Hub, entry, ts: datetime, out_tokens: int, in_tokens: int = 0) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider=entry.provider_name,
        account=entry.account_id,
        model=entry.key,
        status="ok",
        latency_ms=5,
        attempt=1,
        ts=to_iso(ts),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=in_tokens, out_tokens=out_tokens)


def test_observed_daily_cap_is_recorded_and_binds(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    spend_out(hub, datetime(2026, 9, 7, 9, 5, tzinfo=UTC), 400)

    row = hub.quota.record_observed(entry, token_quota("daily"), now)
    assert row == {
        "window": "daily",
        "metric": "out_tokens",
        "value": 400,
        "observed_at": to_iso(now),
    }
    assert hub.store.observed_limits("alpha-1", "alpha/m1")["daily"]["out_tokens"] == 400
    # declared daily out_tokens is 5000, the observed 400 is what the vendor really served
    state = hub.quota.windows(entry, now)["daily"]
    assert (state.limit, state.metric, state.limit_source) == (400, "out_tokens", "observed")
    assert hub.quota.has_room(entry, 0, 1, now) is False
    assert hub.quota.room(entry, now)["remaining_out"] == 0
    assert hub.quota.windows(entry, now)["hourly"].limit_source == "declared"


def test_observed_cap_gives_an_undeclared_model_a_window(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    assert entry.model.windows == {}
    spend_for(hub, entry, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 500)

    assert hub.quota.has_room(entry, 0, 1, now) is True
    hub.quota.record_observed(entry, token_quota("hourly"), now)
    assert hub.quota.has_room(entry, 0, 1, now) is False
    state = hub.quota.windows(entry, now)["hourly"]
    assert (state.used, state.limit, state.limit_source) == (500, 500, "observed")


def test_observed_cap_not_recorded_when_declared_is_tighter_or_nothing_was_used(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    assert hub.quota.record_observed(entry, token_quota("hourly"), now) is None

    spend_out(hub, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 1200)
    assert hub.quota.record_observed(entry, token_quota("hourly"), now) is None
    assert hub.quota.record_observed(entry, token_quota("allowance"), now) is None
    assert hub.store.observed_limits("alpha-1", "alpha/m1") == {}


def test_forgive_keeps_observed_caps_until_dropped(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    spend_out(hub, datetime(2026, 9, 7, 9, 5, tzinfo=UTC), 400)
    hub.quota.record_observed(entry, token_quota("daily"), now)
    hub.quota.mark_exhausted(entry, "free_limit_reached", now, scope="daily")

    hub.quota.forgive("alpha/m1")
    assert hub.quota.exhausted_until(entry, now) is None
    assert hub.quota.has_room(entry, 0, 1, now) is False

    assert hub.quota.forget_observed("alpha/m1") == 1
    assert hub.quota.has_room(entry, 0, 1, now) is True


def test_observed_window_pins_exhaustion_to_that_window(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 3, 0, tzinfo=UTC), 500)
    hub.quota.record_observed(entry, token_quota("daily"), now)

    until = hub.quota.mark_exhausted(entry, "quota", now, scope="daily")
    # beta/m2 declares no windows; without the observed one this would only cool off an hour
    assert until == window_bounds("daily", "Europe/Warsaw", now)[1]
    assert exhausted_row(hub, entry)["scope"] == "daily"


# --- metric-aware observed caps ------------------------------------------------------------


def gemini_quota(quota_id: str, quota_value: str) -> Classification:
    metric = "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota, please check your plan and billing "
                f"details.\n* Quota exceeded for metric: {metric}, limit: {quota_value}",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaMetric": metric, "quotaId": quota_id, "quotaValue": quota_value}
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "9.026s"},
                ],
            }
        }
    )
    return classify(429, body, "gemini")


def groq_quota(message: str) -> Classification:
    return classify(429, json.dumps({"error": {"message": message, "code": "rate_limit_exceeded"}}), "groq")


def test_gemini_per_day_body_records_the_stated_request_cap(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 3, 0, tzinfo=UTC), 500)
    hit = gemini_quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")

    row = hub.quota.record_observed(entry, hit, now)
    assert row == {"window": "daily", "metric": "requests", "value": 20, "observed_at": to_iso(now)}
    observed = hub.store.observed_limits("beta-1", "beta/m2")
    assert observed["daily"]["requests"] == 20
    # the 500 out tokens this account spent are not a cap the vendor ever stated
    assert "out_tokens" not in observed["daily"]

    until = hub.quota.mark_exhausted(entry, "quota", now, scope=hit.scope)
    assert until == window_bounds("daily", "Europe/Warsaw", now)[1]
    assert hub.quota.exhausted_until(entry, now) == until


def test_gemini_per_minute_body_records_nothing_and_parks_nothing(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 500)
    hit = gemini_quota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "10")

    assert hit.kind == "retry"
    assert hit.retry_after_s == 9.026
    assert hub.quota.record_observed(entry, hit, now) is None
    assert hub.store.observed_limits("beta-1", "beta/m2") == {}
    assert hub.quota.exhausted_until(entry, now) is None


def test_groq_tpd_records_the_stated_total_token_limit(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 3, 0, tzinfo=UTC), 32711)
    hit = groq_quota(
        "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` on tokens "
        "per day (TPD): Limit 200000, Used 195998, Requested 7854. Please try again in 27m44s."
    )

    hub.quota.record_observed(entry, hit, now)
    observed = hub.store.observed_limits("beta-1", "beta/m2")
    # 200000 is what groq allows; 32711 is only what this hub counted
    assert observed["daily"] == {"total_tokens": 200000, "observed_at": to_iso(now)}


def test_groq_rpd_records_the_stated_request_limit(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    hit = groq_quota(
        "Rate limit reached for model `m` on requests per day (RPD): Limit 1000, Used 1000, "
        "Requested 1. Please try again in 5m."
    )

    hub.quota.record_observed(entry, hit, now)
    assert hub.store.observed_limits("beta-1", "beta/m2")["daily"]["requests"] == 1000


def test_a_token_body_without_numbers_still_falls_back_to_what_was_used(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 500)

    hub.quota.record_observed(entry, token_quota("hourly"), now)
    assert hub.store.observed_limits("beta-1", "beta/m2")["hourly"]["out_tokens"] == 500


def test_a_body_that_mentions_no_tokens_teaches_nothing(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    spend_for(hub, entry, datetime(2026, 9, 7, 10, 5, tzinfo=UTC), 500)
    hit = Classification("quota", None, "resource exhausted, try again later", "test", scope="hourly")

    assert hub.quota.record_observed(entry, hit, now) is None
    assert hub.store.observed_limits("beta-1", "beta/m2") == {}


def test_a_request_window_counts_calls_and_ignores_the_ones_it_refused(hub: Hub) -> None:
    now = datetime(2026, 9, 7, 10, 30, tzinfo=UTC)
    entry = entry_of(hub, "beta/m2", "beta-1")
    hub.store.record_observed_limit("beta-1", "beta/m2", "daily", "requests", 3, to_iso(now))
    for status in ("ok", "truncated", "quota"):
        hub.store.start_usage(
            app="test",
            provider="beta",
            account="beta-1",
            model="beta/m2",
            status=status,
            latency_ms=1,
            attempt=1,
            ts=to_iso(datetime(2026, 9, 7, 4, 0, tzinfo=UTC)),
        )

    state = hub.quota.windows(entry, now)["daily"]
    assert (state.metric, state.used, state.limit) == ("requests", 2, 3)
    room = hub.quota.room(entry, now)
    assert room["remaining_requests"] == 1
    assert room["remaining_out"] is None and room["remaining_in"] is None
    assert room["binding_window"] == "daily"
    assert hub.quota.has_room(entry, 0, 4000, now) is True

    hub.store.start_usage(
        app="test",
        provider="beta",
        account="beta-1",
        model="beta/m2",
        status="ok",
        latency_ms=1,
        attempt=1,
        ts=to_iso(datetime(2026, 9, 7, 5, 0, tzinfo=UTC)),
    )
    assert hub.quota.room(entry, now)["remaining_requests"] == 0
    assert hub.quota.has_room(entry, 0, 1, now) is False


def test_a_declared_request_window_is_a_limit_like_any_other() -> None:
    assert QuotaWindow(requests=20).limits() == {"requests": 20}
    assert QuotaWindow(out_tokens=100, requests=20).limits() == {"out_tokens": 100, "requests": 20}
