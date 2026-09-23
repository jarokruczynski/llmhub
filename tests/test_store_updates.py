from __future__ import annotations

import pytest

from llmhub.runtime import Hub


def test_an_update_names_only_real_columns(hub: Hub) -> None:
    assert hub.store.assignments("jobs", ["state", "updated_at"]) == "state = ?, updated_at = ?"


def test_a_name_that_is_not_a_column_is_refused_not_pasted(hub: Hub) -> None:
    with pytest.raises(ValueError, match="not a column of jobs"):
        hub.store.assignments("jobs", ["state = 'done', id"])


def test_an_unknown_table_is_refused(hub: Hub) -> None:
    with pytest.raises(ValueError, match="unknown table"):
        hub.store.assignments("nope", ["id"])
