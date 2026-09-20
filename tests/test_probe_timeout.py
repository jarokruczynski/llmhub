from __future__ import annotations

import asyncio
from dataclasses import replace

from llmhub.api import probe_entry
from llmhub.runtime import Hub


async def test_a_vendor_that_never_answers_ends_the_probe(hub: Hub, monkeypatch) -> None:
    async def never_answers(*_args, **_kwargs):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr("llmhub.gateway.call_model", never_answers)
    hub.settings = replace(hub.settings, probe_timeout_s=1)
    entry = hub.registry.entry("alpha/m1", "alpha-1")
    assert entry is not None

    result = await probe_entry(hub, entry)

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["error_code"] == "probe_timeout"
    assert "1s" in result["error"]


async def test_the_probe_still_reports_a_normal_answer(hub: Hub, monkeypatch) -> None:
    hub.settings = replace(hub.settings, probe_timeout_s=30)
    entry = hub.registry.entry("alpha/m1", "alpha-1")
    assert entry is not None

    result = await probe_entry(hub, entry)

    # whatever the stub backend answers, the point is that it answered rather than timing out
    assert result["status"] != "timeout"
