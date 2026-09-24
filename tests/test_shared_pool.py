from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from llmhub.config import Entry
from llmhub.providers_catalog import quota_group
from llmhub.router import AllCandidatesFailed, UpstreamError
from llmhub.runtime import Hub
from llmhub.status import model_rows
from llmhub.store import parse_iso
from llmhub.vendor_errors import classify

from .conftest import entry_of
from .test_vendor_errors import AGY_WEEKLY_REFUSAL

PRO = "gemini-3.1-pro-high"
LOW = "gemini-3.1-pro-low"
FLASH = "gemini-3.8-flash-low"
OPUS = "claude-opus-4-6-thinking"
WEEKLY = timedelta(hours=6, minutes=9, seconds=21)


def agy_block(account: str, template: str = "antigravity") -> dict[str, Any]:
    return {
        "kind": "cli",
        "template": template,
        "command": "agy",
        "accounts": [{"id": account, "api_key_env": None}],
        "models": [
            {"id": PRO, "caps": ["text"], "free": {}},
            {"id": LOW, "caps": ["text"], "free": {}},
            {"id": FLASH, "caps": ["text"], "free": {}},
            {"id": OPUS, "caps": ["text"], "free": {}},
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


def test_quota_group_by_prefix() -> None:
    assert quota_group("antigravity", PRO) == "Gemini Models"
    assert quota_group("antigravity", FLASH) == "Gemini Models"
    assert quota_group("antigravity", OPUS) == "Claude and GPT models"
    assert quota_group("antigravity", "gpt-oss-120b-medium") == "Claude and GPT models"
    assert quota_group("antigravity", "something-else") is None
    assert quota_group("groq", PRO) is None


async def test_a_refusal_parks_the_whole_group_on_both_logins(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity/{PRO}")
    origin = agy_hub.quota.exhausted_until(entry_of(agy_hub, f"antigravity/{PRO}", "antigravity-jaro"))
    assert origin is not None
    for provider, account in (("antigravity", "antigravity-jaro"), ("antigravity-2", "antigravity-2-jaro")):
        for model in (PRO, LOW, FLASH):
            entry = entry_of(agy_hub, f"{provider}/{model}", account)
            assert agy_hub.quota.exhausted_until(entry) == origin, entry.key
    row = agy_hub.store.exhausted_until("antigravity-2-jaro", f"antigravity-2/{LOW}", datetime.now(UTC))
    assert row is not None
    assert row["reason"].startswith(f"shared pool group Gemini Models via antigravity/{PRO}")


async def test_a_gemini_refusal_leaves_the_claude_group_open(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity/{PRO}")
    for provider, account in (("antigravity", "antigravity-jaro"), ("antigravity-2", "antigravity-2-jaro")):
        assert agy_hub.quota.exhausted_until(entry_of(agy_hub, f"{provider}/{OPUS}", account)) is None


async def test_a_claude_refusal_leaves_the_gemini_group_open(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity-2/{OPUS}")
    assert (
        agy_hub.quota.exhausted_until(entry_of(agy_hub, f"antigravity/{OPUS}", "antigravity-jaro"))
        is not None
    )
    for model in (PRO, LOW, FLASH):
        assert (
            agy_hub.quota.exhausted_until(entry_of(agy_hub, f"antigravity/{model}", "antigravity-jaro"))
            is None
        )


async def test_a_group_park_writes_one_event(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity/{PRO}")
    rows = agy_hub.store.query("SELECT message FROM events WHERE kind = 'quota'")
    spread = [row for row in rows if "shared pool group" in row["message"]]
    assert len(spread) == 1
    assert "on 5 pairs" in spread[0]["message"]


async def test_a_park_from_before_the_group_map_spreads_on_startup(agy_hub: Hub) -> None:
    origin = entry_of(agy_hub, f"antigravity-2/{PRO}", "antigravity-2-jaro")
    until = datetime.now(UTC) + WEEKLY
    agy_hub.quota.mark_exhausted(
        origin, "Individual quota reached. Resets in 2h.", until=until, reset_named=True
    )
    # the old build parked the same id on the other login, with its own note
    agy_hub.quota.mark_exhausted(
        entry_of(agy_hub, f"antigravity/{PRO}", "antigravity-jaro"),
        f"shared pool with antigravity-2/{PRO} (antigravity-2-jaro): quota",
        until=until,
        reset_named=True,
    )
    agy_hub.reload()
    for provider, account in (("antigravity", "antigravity-jaro"), ("antigravity-2", "antigravity-2-jaro")):
        assert agy_hub.quota.exhausted_until(entry_of(agy_hub, f"{provider}/{LOW}", account)) == until
        assert agy_hub.quota.exhausted_until(entry_of(agy_hub, f"{provider}/{OPUS}", account)) is None
    row = agy_hub.store.exhausted_until("antigravity-jaro", f"antigravity/{LOW}", datetime.now(UTC))
    assert row is not None and row["reason"].startswith(
        f"shared pool group Gemini Models via antigravity-2/{PRO}"
    )


async def test_status_says_where_a_group_park_came_from(agy_hub: Hub) -> None:
    await refuse_weekly(agy_hub, f"antigravity/{PRO}")
    rows = {(row["account"], row["key"]): row for row in model_rows(agy_hub)}
    low = rows[("antigravity-2-jaro", f"antigravity-2/{LOW}")]
    assert low["status"] == "exhausted"
    assert low["exhausted_reason"].startswith(f"shared pool group Gemini Models via antigravity/{PRO}")
    pro = rows[("antigravity-jaro", f"antigravity/{PRO}")]
    assert parse_iso(low["reason"]) == parse_iso(pro["reason"])
    assert rows[("antigravity-jaro", f"antigravity/{OPUS}")]["exhausted_reason"] is None


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
