from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from llmhub.config import Entry
from llmhub.router import AllCandidatesFailed, UpstreamError
from llmhub.runtime import Hub
from llmhub.store import parse_iso
from llmhub.vendor_errors import classify

from .conftest import entry_of
from .test_vendor_errors import AGY_WEEKLY_REFUSAL

PRO = "gemini-3.1-pro-high"
WEEKLY = timedelta(hours=6, minutes=9, seconds=21)


def agy_block(account: str, template: str = "antigravity") -> dict[str, Any]:
    return {
        "kind": "cli",
        "template": template,
        "command": "agy",
        "accounts": [{"id": account, "api_key_env": None}],
        "models": [
            {"id": PRO, "caps": ["text"], "free": {}},
            {"id": "gemini-3.8-flash-low", "caps": ["text"], "free": {}},
        ],
    }


@pytest.fixture
def agy_hub(hub: Hub) -> Hub:
    data = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    data["providers"]["antigravity"] = agy_block("antigravity-jaro")
    data["providers"]["antigravity-2"] = agy_block("antigravity-2-jaro")
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    hub.reload()
    return hub


async def refuse_weekly(hub: Hub, model: str) -> tuple[datetime, datetime]:
    async def call(entry: Entry, attempt_no: int) -> str:
        raise UpstreamError(classify(None, AGY_WEEKLY_REFUSAL, entry.template_id), None)

    selection = hub.router.select(model_request=model)
    before = datetime.now(UTC)
    with pytest.raises(AllCandidatesFailed):
        await hub.router.run(selection.candidates, call)
    return before, datetime.now(UTC)


async def test_weekly_refusal_parks_both_logins_until_the_named_reset(agy_hub: Hub) -> None:
    before, after = await refuse_weekly(agy_hub, f"antigravity-2/{PRO}")
    for key, account in (
        (f"antigravity-2/{PRO}", "antigravity-2-jaro"),
        (f"antigravity/{PRO}", "antigravity-jaro"),
    ):
        entry = entry_of(agy_hub, key, account)
        until = agy_hub.quota.exhausted_until(entry)
        assert until is not None
        assert before + WEEKLY - timedelta(seconds=1) <= until <= after + WEEKLY
        # the client's 429 next_window_at for a pinned request is the same instant
        next_window = agy_hub.router.next_window_at(key)
        assert next_window is not None and parse_iso(next_window) == until


async def test_shared_pool_parks_only_the_same_model_id(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity/{PRO}")
    flash = entry_of(agy_hub, "antigravity-2/gemini-3.8-flash-low", "antigravity-2-jaro")
    assert agy_hub.quota.exhausted_until(flash) is None


async def test_a_template_without_a_shared_pool_parks_only_itself(hub: Hub) -> None:
    data = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    data["providers"]["one"] = agy_block("one-1", template="other-cli")
    data["providers"]["two"] = agy_block("two-1", template="other-cli")
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    hub.reload()
    assert hub.router.pool_siblings(entry_of(hub, f"one/{PRO}", "one-1")) == []


async def test_a_sibling_parked_longer_is_not_shortened(agy_hub: Hub) -> None:
    sibling = entry_of(agy_hub, f"antigravity/{PRO}", "antigravity-jaro")
    later = datetime.now(UTC) + timedelta(days=2)
    agy_hub.quota.mark_exhausted(sibling, "earlier refusal", until=later, reset_named=True)
    await refuse_weekly(agy_hub, f"antigravity-2/{PRO}")
    assert agy_hub.quota.exhausted_until(sibling) == later
