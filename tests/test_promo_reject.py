from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import respx

from llmhub.promo_identity import identify
from llmhub.runtime import Hub
from llmhub.scout.apply import apply_decisions, build_report
from llmhub.scout.curate import _user_message, curate
from llmhub.scout.llm import HubLLM
from llmhub.store import PROMO_STATUS_RANK, Store

HUB_URL = "http://127.0.0.1:8800/v1/chat/completions"

REASON = "IDE-only, no API endpoint the hub can call"


def post(store: Store, provider: str, **fields: Any) -> dict[str, Any]:
    row, _ = store.upsert_promo(
        identity=identify(provider, fields.get("url"), fields.get("base_url"), store).key,
        provider=provider,
        **fields,
    )
    return row


def chat_response(content: Any) -> httpx.Response:
    import json

    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-reject",
            "model": "alpha/m1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        headers={"x-hub-model": "alpha/m1"},
    )


# --- store --------------------------------------------------------------------------


def test_rank_puts_rejected_over_known_and_under_used() -> None:
    rank = PROMO_STATUS_RANK
    assert rank["new"] < rank["known"] < rank["rejected"] < rank["used"]
    assert rank["expired"] < rank["rejected"]


def test_reject_writes_the_row_and_the_history(hub: Hub) -> None:
    row = post(hub.store, "Windsurf", url="https://windsurf.test/pricing", note="pro trial")
    rejected = hub.store.reject_promo(int(row["id"]), REASON)
    assert rejected is not None
    assert rejected["status"] == "rejected"
    assert rejected["rejected_reason"] == REASON
    assert rejected["rejected_at"]

    history = hub.store.promo_rejections()
    assert len(history) == 1
    assert history[0]["provider"] == "Windsurf"
    assert history[0]["reason"] == REASON
    assert history[0]["note_snippet"] == "pro trial"


def test_reopen_clears_the_verdict_and_keeps_the_history(hub: Hub) -> None:
    row = post(hub.store, "Windsurf", url="https://windsurf.test/pricing")
    hub.store.reject_promo(int(row["id"]), REASON)
    reopened = hub.store.reopen_promo(int(row["id"]))
    assert reopened is not None
    assert (reopened["status"], reopened["rejected_reason"], reopened["rejected_at"]) == ("known", None, None)
    assert len(hub.store.promo_rejections()) == 1

    # a second rejection of the same row is a second event, not an overwrite
    hub.store.reject_promo(int(row["id"]), "still IDE-only a month later")
    assert len(hub.store.promo_rejections()) == 2


def test_a_repost_of_a_rejected_offer_stays_rejected(hub: Hub) -> None:
    row = post(hub.store, "Windsurf", url="https://windsurf.test/pricing", note="pro trial")
    hub.store.reject_promo(int(row["id"]), REASON)

    merged = post(
        hub.store,
        "windsurf ide",
        url="https://windsurf.test/promo",
        note="pro trial is back",
        status="new",
        source="scout",
    )
    assert merged["id"] == row["id"]
    assert merged["status"] == "rejected"
    assert merged["rejected_reason"] == REASON
    assert merged["updates_count"] == 1
    assert merged["note"].splitlines()[-1].endswith("scout] pro trial is back")


# --- api ----------------------------------------------------------------------------


async def test_patch_to_rejected_without_a_reason_is_422(client: httpx.AsyncClient, hub: Hub) -> None:
    row = hub.store.add_promo("windsurf", "https://windsurf.test/x", "trial", "new", "manual")
    answer = await client.patch(f"/api/promos/{row['id']}", json={"status": "rejected"})
    assert answer.status_code == 422
    assert "rejected_reason" in answer.text
    assert "proposed again" in answer.text
    assert hub.store.promo(int(row["id"]))["status"] == "new"

    short = await client.patch(
        f"/api/promos/{row['id']}", json={"status": "rejected", "rejected_reason": "no"}
    )
    assert short.status_code == 422


async def test_reject_and_reopen_round_trip(client: httpx.AsyncClient, hub: Hub) -> None:
    row = hub.store.add_promo("windsurf", "https://windsurf.test/x", "trial", "new", "manual")
    promo_id = int(row["id"])

    rejected = await client.post(f"/api/promos/{promo_id}/reject", json={"reason": REASON})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejected_reason"] == REASON

    history = (await client.get("/api/promos/rejections")).json()["rejections"]
    assert [item["reason"] for item in history] == [REASON]
    assert any(event["message"] == f"rejected windsurf: {REASON}" for event in hub.store.events())

    reopened = await client.post(f"/api/promos/{promo_id}/reopen", json={})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "known"
    assert reopened.json()["rejected_reason"] is None
    assert len((await client.get("/api/promos/rejections")).json()["rejections"]) == 1


async def test_reject_over_patch_also_records_the_history(client: httpx.AsyncClient, hub: Hub) -> None:
    row = hub.store.add_promo("windsurf", "https://windsurf.test/x", "trial", "new", "manual")
    answer = await client.patch(
        f"/api/promos/{row['id']}", json={"status": "rejected", "rejected_reason": REASON}
    )
    assert answer.status_code == 200 and answer.json()["status"] == "rejected"
    assert len(hub.store.promo_rejections()) == 1


async def test_reject_on_an_unknown_promo_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/promos/999/reject", json={"reason": REASON})).status_code == 404
    assert (await client.post("/api/promos/999/reopen", json={})).status_code == 404


async def test_posting_a_rejected_vendor_answers_rejected(client: httpx.AsyncClient, hub: Hub) -> None:
    created = (
        await client.post("/api/promos", json={"provider": "Windsurf", "url": "https://windsurf.test/x"})
    ).json()
    await client.post(f"/api/promos/{created['id']}/reject", json={"reason": REASON})

    again = await client.post(
        "/api/promos",
        json={"provider": "windsurf ide", "url": "https://windsurf.test/y", "note": "trial is back"},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["status"] == "rejected"


# --- curate -------------------------------------------------------------------------


def test_the_user_message_carries_the_rejections() -> None:
    message = _user_message(
        [{"provider": "windsurf"}],
        [],
        ["groq"],
        [],
        [{"provider": "windsurf", "reason": REASON}, {"provider": "lambdalabs", "reason": "needs a card"}],
    )
    assert "Rejected by the owner (do not propose these or similar offers" in message
    assert f"- windsurf: {REASON}" in message
    assert "- lambdalabs: needs a card" in message


def test_no_rejections_means_no_block() -> None:
    assert "Rejected by the owner" not in _user_message([{"provider": "groq"}], [], ["groq"], [], [])


SKIP_REJECTED_JSON = {
    "decisions": [
        {"action": "skip_rejected", "promo_id": 1, "reason": "matches the windsurf rejection: IDE-only"},
        {"action": "skip", "promo_id": 2, "reason": "nothing new"},
    ]
}


async def test_skip_rejected_is_a_skip_that_is_counted_apart(hub: Hub) -> None:
    async with httpx.AsyncClient() as client:
        llm = HubLLM(client, "http://127.0.0.1:8800/v1")
        with respx.mock:
            respx.post(HUB_URL).mock(return_value=chat_response(SKIP_REJECTED_JSON))
            result = await curate(
                llm,
                offers=[{"provider": "windsurf"}],
                promos=[],
                rejections=[{"provider": "windsurf", "reason": REASON}],
                template_ids=["groq"],
            )
    assert [decision["action"] for decision in result.decisions] == ["skip", "skip"]
    assert [decision["rejected_match"] for decision in result.decisions] == [True, False]

    applied = apply_decisions(hub.store, result.decisions, dry_run=True)
    assert (applied.skipped, applied.skipped_rejected) == (2, 1)
    assert applied.decisions[0]["outcome"] == "skipped_rejected"


def test_the_report_counts_the_rejected_skips() -> None:
    report = build_report(
        run_id=1,
        started_at="2026-09-09T08:00:00+00:00",
        counts={"skipped": 3, "skipped_rejected": 2},
        models_used={},
        tokens={},
        errors=[],
        decisions=[],
    )
    assert "- skipped as rejected: 2" in report


# --- skill --------------------------------------------------------------------------


def test_the_promo_hunt_skill_reads_the_rejections() -> None:
    text = Path(__file__).resolve().parents[1].joinpath(".claude/skills/promo-hunt/SKILL.md").read_text()
    assert "api/promos/rejections" in text
    assert "dropped as rejected" in text
