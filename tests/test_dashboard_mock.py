from __future__ import annotations

from llmhub.dashboard import dev_mock


def test_dev_mock_ships_a_request_metered_window() -> None:
    windows = [row["windows"].get("daily") for row in dev_mock._models()]
    metered = [window for window in windows if window and window["metric"] == "requests"]
    assert len(metered) == 1
    assert (metered[0]["used"], metered[0]["limit"]) == (12, 20)


def test_the_mock_serves_a_waiting_call_for_the_kill_switch() -> None:
    states = {call["state"] for call in dev_mock.LIVE_CALLS}
    assert states == {"running", "waiting"}
    assert len({call["call_id"] for call in dev_mock.LIVE_CALLS}) == len(dev_mock.LIVE_CALLS)
