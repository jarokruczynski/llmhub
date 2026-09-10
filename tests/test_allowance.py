from __future__ import annotations

from datetime import UTC, datetime

from llmhub.runtime import Hub
from llmhub.status import model_rows
from llmhub.store import to_iso

from .conftest import entry_of

SEPTEMBER = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
NOVEMBER = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
AFTER_EXPIRY = datetime(2026, 12, 2, 12, 0, tzinfo=UTC)


def spend(hub: Hub, account: str, model: str, ts: datetime, tokens: int) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="gamma",
        account=account,
        model=model,
        status="ok",
        latency_ms=5,
        attempt=1,
        ts=to_iso(ts),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=0, out_tokens=0, total_tokens=tokens)


def test_allowance_counts_from_activated_at_and_never_resets(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    spend(hub, "gamma-1", "gamma/fixed", datetime(2026, 9, 2, 8, 0, tzinfo=UTC), 600)
    spend(hub, "gamma-1", "gamma/fixed", datetime(2026, 10, 5, 8, 0, tzinfo=UTC), 300)

    state = hub.quota.window_state(entry, "allowance", entry.model.windows["allowance"], NOVEMBER)
    assert state.used == 900
    assert state.limit == 1000
    assert state.metric == "total_tokens"
    assert state.started_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert state.resets_at == datetime(2026, 12, 1, tzinfo=UTC)

    september = hub.quota.window_state(entry, "allowance", entry.model.windows["allowance"], SEPTEMBER)
    assert september.used == 900


def test_allowance_ignores_usage_before_activation(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    spend(hub, "gamma-1", "gamma/fixed", datetime(2026, 8, 20, 8, 0, tzinfo=UTC), 900)
    state = hub.quota.window_state(entry, "allowance", entry.model.windows["allowance"], NOVEMBER)
    assert state.used == 0


def test_allowance_start_defaults_to_first_usage_row(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/openended", "gamma-2")
    assert entry.activated_at is None
    first = datetime(2026, 9, 3, 7, 30, tzinfo=UTC)
    spend(hub, "gamma-2", "gamma/openended", first, 250)
    spend(hub, "gamma-2", "gamma/openended", datetime(2026, 10, 1, 7, 30, tzinfo=UTC), 250)

    assert hub.quota.allowance_start(entry) == first
    state = hub.quota.window_state(entry, "allowance", entry.model.windows["allowance"], NOVEMBER)
    assert state.used == 500
    assert state.resets_at is None


def test_allowance_room_runs_out_and_stays_out(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    spend(hub, "gamma-1", "gamma/fixed", datetime(2026, 9, 2, 8, 0, tzinfo=UTC), 999)
    assert hub.quota.has_room(entry, 0, 1, SEPTEMBER) is True
    assert hub.quota.has_room(entry, 1, 1, SEPTEMBER) is False
    assert hub.quota.has_room(entry, 1, 1, NOVEMBER) is False


def test_expired_allowance_model_is_expired_and_unselectable(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    assert hub.quota.expired_at(entry, NOVEMBER) is None
    assert hub.quota.expired_at(entry, AFTER_EXPIRY) == datetime(2026, 12, 1, tzinfo=UTC)
    assert hub.quota.has_room(entry, 0, 1, AFTER_EXPIRY) is False

    selection = hub.router.select(model_request="gamma/fixed", now=AFTER_EXPIRY)
    assert selection.candidates == []
    assert selection.rejected_reasons() == {"expired"}

    row = next(
        item
        for item in model_rows(hub, AFTER_EXPIRY)
        if item["key"] == "gamma/fixed" and item["account"] == "gamma-1"
    )
    assert row["status"] == "expired"
    assert row["reason"] == to_iso(datetime(2026, 12, 1, tzinfo=UTC))
    assert row["windows"]["allowance"]["resets_at"] == to_iso(datetime(2026, 12, 1, tzinfo=UTC))


def test_open_ended_allowance_never_expires(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/openended", "gamma-1")
    assert entry.model.expires_at() is None
    assert hub.quota.expired_at(entry, AFTER_EXPIRY) is None
    selection = hub.router.select(model_request="gamma/openended", now=AFTER_EXPIRY)
    assert ("gamma/openended", "gamma-1") in [(item.key, item.account_id) for item in selection.candidates]


def test_allowance_exhaustion_lasts_until_expiry(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/fixed", "gamma-1")
    until = hub.quota.mark_exhausted(entry, "free quota has been exhausted", SEPTEMBER)
    assert until == datetime(2026, 12, 1, tzinfo=UTC)
    assert hub.quota.exhausted_until(entry, NOVEMBER) == until


def test_open_ended_allowance_exhaustion_retries_after_a_day(hub: Hub) -> None:
    entry = entry_of(hub, "gamma/openended", "gamma-1")
    until = hub.quota.mark_exhausted(entry, "free quota has been exhausted", SEPTEMBER)
    assert until == datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_expired_model_is_hidden_from_v1_models(hub: Hub) -> None:
    rows = model_rows(hub, AFTER_EXPIRY)
    expired = [row for row in rows if row["status"] == "expired"]
    assert {row["key"] for row in expired} == {"gamma/fixed"}


def test_monthly_window_still_resets(hub: Hub) -> None:
    entry = entry_of(hub, "alpha/m1", "alpha-1")
    assert "allowance" not in entry.model.windows
    assert hub.quota.next_reset(entry, SEPTEMBER) == datetime(2026, 9, 10, 13, 0, tzinfo=UTC)
