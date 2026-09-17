from __future__ import annotations

from datetime import UTC, datetime, timedelta

from llmhub.runtime import Hub
from llmhub.status import entry_status
from llmhub.store import to_iso

KEY = "alpha/m1"
ACCOUNT = "alpha-1"


def record(hub: Hub, *, status: str, when: datetime, error_code: str | None = None) -> None:
    hub.store.start_usage(
        app="test",
        provider="alpha",
        account=ACCOUNT,
        model=KEY,
        status=status,
        latency_ms=1,
        attempt=1,
        error_code=error_code,
        ts=to_iso(when),
    )


def status_of(hub: Hub, now: datetime) -> tuple[str, str | None]:
    entry = hub.registry.entry(KEY, ACCOUNT)
    assert entry is not None
    return entry_status(hub, entry, now, set())


def test_a_fresh_refusal_is_not_reported_as_ok(hub: Hub) -> None:
    now = datetime.now(UTC)
    record(hub, status="error", when=now - timedelta(hours=1), error_code="insufficient_quota")

    status, reason = status_of(hub, now)

    assert status == "warning"
    assert reason is not None and "insufficient_quota" in reason


def test_a_later_success_retires_the_refusal(hub: Hub) -> None:
    now = datetime.now(UTC)
    record(hub, status="error", when=now - timedelta(hours=2), error_code="insufficient_quota")
    record(hub, status="ok", when=now - timedelta(minutes=5))

    assert status_of(hub, now)[0] == "ok"


def test_a_refusal_older_than_the_window_stops_counting(hub: Hub) -> None:
    now = datetime.now(UTC)
    record(hub, status="error", when=now - timedelta(days=3), error_code="insufficient_quota")

    assert status_of(hub, now)[0] == "ok"


def test_the_model_row_carries_the_warning_and_its_reason(hub: Hub) -> None:
    from llmhub.status import model_rows

    now = datetime.now(UTC)
    record(hub, status="error", when=now - timedelta(minutes=30), error_code="insufficient_quota")

    row = next(row for row in model_rows(hub, now) if row["key"] == KEY and row["account"] == ACCOUNT)

    assert row["status"] == "warning"
    assert "insufficient_quota" in row["last_error"]
