from __future__ import annotations

from pathlib import Path

from llmhub.dashboard import dev_mock


def test_dev_mock_ships_a_request_metered_window() -> None:
    windows = [row["windows"].get("daily") for row in dev_mock._models()]
    metered = [window for window in windows if window and window["metric"] == "requests"]
    assert len(metered) == 1
    assert (metered[0]["used"], metered[0]["limit"]) == (12, 20)


def test_the_window_cell_labels_a_request_count() -> None:
    # "12/20" with no unit reads as tokens, which is the whole failure this learned to avoid
    app_js = (Path(dev_mock.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert 'var isRequests = unit === "requests";' in app_js
    assert '(isRequests ? " req" : "")' in app_js
