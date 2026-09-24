from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from llmhub.config import Entry
from llmhub.pool_probe import PoolProbe, empty_groups
from llmhub.runtime import Hub
from llmhub.store import parse_iso

from .conftest import entry_of
from .test_shared_pool import FLASH, LOW, OPUS, PRO, agy_hub, refuse_weekly  # noqa: F401

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def report(gemini_weekly: float, claude_weekly: float, claude_5h: float = 1.0) -> dict[str, Any]:
    """The shape of `agy -p /quota --output-format json`, trimmed to what the probe reads."""
    return {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": gemini_weekly,
                                "reset_time": "2026-09-24T09:18:04Z",
                            },
                            # spent weekly: the five-hour bucket is disabled and shows 100%
                            {"id": "gemini-5h", "window": "5h", "disabled": True, "remaining_fraction": 1},
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": claude_weekly,
                                "reset_time": "2026-09-24T09:22:16Z",
                            },
                            {
                                "id": "3p-5h",
                                "window": "5h",
                                "remaining_fraction": claude_5h,
                                "reset_time": "2026-09-24T12:00:00Z",
                            },
                        ],
                    },
                ]
            },
        },
    }


def test_empty_groups_reads_only_live_buckets_at_zero() -> None:
    assert empty_groups(report(0, 0.4), NOW) == {"Gemini Models": parse_iso("2026-09-24T09:18:04Z")}
    assert empty_groups(report(0.5, 0.4), NOW) == {}
    # both buckets at zero: the later reset frees the group
    assert empty_groups(report(1, 0, claude_5h=0), NOW) == {
        "Claude and GPT models": parse_iso("2026-09-24T12:00:00Z")
    }
    # a reset already past binds nothing
    assert empty_groups(report(0, 1), NOW + timedelta(hours=3)) == {}
    assert empty_groups(None, NOW) == {}
    assert empty_groups({"status": "SUCCESS"}, NOW) == {}


async def test_probe_parks_each_empty_group_on_both_logins(agy_hub: Hub) -> None:  # noqa: F811
    seen: list[str] = []

    async def runner(entry: Entry) -> dict[str, Any]:
        seen.append(entry.provider_name)
        return report(0, 0)

    probe = PoolProbe(agy_hub, runner=runner)
    anchors = probe.anchors()
    assert len(anchors) == 1
    parked = await probe.probe(anchors[0], now=NOW)
    assert parked == 8
    for provider, account in (("antigravity", "antigravity-jaro"), ("antigravity-2", "antigravity-2-jaro")):
        for model, reset in ((PRO, "09:18:04"), (LOW, "09:18:04"), (FLASH, "09:18:04"), (OPUS, "09:22:16")):
            until = agy_hub.quota.exhausted_until(entry_of(agy_hub, f"{provider}/{model}", account), NOW)
            assert until == parse_iso(f"2026-09-24T{reset}Z"), (provider, model)
    row = agy_hub.store.exhausted_until("antigravity-2-jaro", f"antigravity-2/{OPUS}", NOW)
    assert row is not None and "Claude and GPT models at 0% per /quota" in row["reason"]


async def test_probe_leaves_a_group_with_room_alone(agy_hub: Hub) -> None:  # noqa: F811
    async def runner(entry: Entry) -> dict[str, Any]:
        return report(0, 0.7)

    probe = PoolProbe(agy_hub, runner=runner)
    await probe.probe(probe.anchors()[0], now=NOW)
    assert (
        agy_hub.quota.exhausted_until(entry_of(agy_hub, f"antigravity/{OPUS}", "antigravity-jaro"), NOW)
        is None
    )


async def test_a_refusal_asks_for_the_report(agy_hub: Hub) -> None:  # noqa: F811
    asked: list[str] = []
    agy_hub.router.pool_probe = lambda entry: asked.append(entry.key)
    await refuse_weekly(agy_hub, f"antigravity-2/{PRO}")
    assert asked == [f"antigravity-2/{PRO}"]
    # a reload keeps the hook
    agy_hub.reload()
    assert agy_hub.router.pool_probe is not None
