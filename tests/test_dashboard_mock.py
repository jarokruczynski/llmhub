from __future__ import annotations

from llmhub.dashboard import dev_mock


def test_dev_mock_ships_a_request_metered_window() -> None:
    windows = [row["windows"].get("daily") for row in dev_mock._models()]
    metered = [window for window in windows if window and window["metric"] == "requests"]
    assert len(metered) == 1
    assert (metered[0]["used"], metered[0]["limit"]) == (12, 20)


def test_dev_mock_ships_an_unverified_pair() -> None:
    rows = [row for row in dev_mock._models() if row["status"] == "unverified"]
    assert len(rows) == 1
    assert rows[0]["last_ok_at"] is None
    assert rows[0]["reason"] == "never answered yet"


def test_the_mock_serves_a_waiting_call_for_the_kill_switch() -> None:
    states = {call["state"] for call in dev_mock.LIVE_CALLS}
    assert states == {"running", "waiting"}
    assert len({call["call_id"] for call in dev_mock.LIVE_CALLS}) == len(dev_mock.LIVE_CALLS)


def test_the_mock_serves_attempt_rows_with_a_fallback_after_each_refusal() -> None:
    since = dev_mock.now() - dev_mock.timedelta(hours=6)
    rows = dev_mock._activity_calls(since, 5000)
    assert rows and rows == sorted(rows, key=lambda row: row["ts"], reverse=True)
    assert {row["status"] for row in rows} >= {"ok", "quota"}
    assert any(row["attempt"] == 2 for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)
