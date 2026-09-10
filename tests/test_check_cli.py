from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
import yaml

from llmhub.__main__ import parse_args, run_refresh_context
from llmhub.config import Settings
from llmhub.runtime import Hub
from llmhub.status import check_rows, check_table
from llmhub.store import to_iso

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_check_subcommand_parses_and_serve_stays_default() -> None:
    assert parse_args(["check"]).command == "check"
    serve = parse_args(["--port", "9000"])
    assert serve.command is None
    assert serve.port == 9000


def test_refresh_context_subcommand_parses_dry_run_and_provider() -> None:
    args = parse_args(["refresh-context"])
    assert args.command == "refresh-context"
    assert args.dry_run is False
    assert args.provider is None

    args = parse_args(["refresh-context", "--dry-run", "--provider", "beta"])
    assert args.dry_run is True
    assert args.provider == "beta"


@respx.mock
def test_run_refresh_context_dry_run_prints_the_table_and_writes_nothing(settings: Settings) -> None:
    respx.get("https://beta.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m2", "context_length": 32768}]})
    )
    output = run_refresh_context(dry_run=True, provider="beta")
    assert "beta" in output
    assert "dry run" in output

    data = yaml.safe_load(settings.registry_path.read_text(encoding="utf-8"))
    assert data["providers"]["beta"]["models"][0].get("context") is None


@respx.mock
def test_run_refresh_context_writes_the_backup_and_the_missing_field(settings: Settings) -> None:
    respx.get("https://beta.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m2", "context_length": 32768}]})
    )
    output = run_refresh_context(dry_run=False, provider="beta")
    assert "wrote context on 1 model(s)" in output
    assert "backup" in output

    data = yaml.safe_load(settings.registry_path.read_text(encoding="utf-8"))
    assert data["providers"]["beta"]["models"][0]["context"] == 32768

    backups = list((settings.home / "backups").glob("providers-*.yaml"))
    assert len(backups) == 1


def test_check_rows_cover_every_account_model_pair(hub: Hub) -> None:
    rows = check_rows(hub, NOW)
    pairs = {(row["key"], row["account"]) for row in rows}
    assert ("alpha/m1", "alpha-1") in pairs
    assert ("alpha/m1", "alpha-2") in pairs
    assert ("gamma/fixed", "gamma-1") in pairs
    assert len(rows) == len(list(hub.registry.entries()))
    for row in rows:
        assert set(row) == {
            "key",
            "account",
            "key_present",
            "status",
            "windows",
            "exhausted_until",
        }


def test_check_reports_missing_key_and_exhaustion(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_KEY_2")
    entry = hub.registry.entry("alpha/m1", "alpha-1")
    assert entry is not None
    until = hub.quota.mark_exhausted(entry, "insufficient_quota", NOW)

    rows = {(row["key"], row["account"]): row for row in check_rows(hub, NOW)}
    exhausted = rows[("alpha/m1", "alpha-1")]
    assert exhausted["status"] == "exhausted"
    assert exhausted["exhausted_until"] == to_iso(until)
    assert exhausted["key_present"] is True

    missing = rows[("alpha/m1", "alpha-2")]
    assert missing["key_present"] is False
    assert missing["status"] == "down"


def test_check_windows_column_shows_used_over_limit(hub: Hub) -> None:
    usage_id = hub.store.start_usage(
        app="test",
        provider="alpha",
        account="alpha-1",
        model="alpha/m1",
        status="ok",
        latency_ms=1,
        attempt=1,
        ts=to_iso(NOW),
    )
    hub.store.update_usage_tokens(usage_id, in_tokens=40, out_tokens=250)
    row = next(
        item for item in check_rows(hub, NOW) if item["key"] == "alpha/m1" and item["account"] == "alpha-1"
    )
    assert row["windows"] == "hourly 250/1000 daily 250/5000"


def test_check_table_is_plain_text_with_a_header(hub: Hub) -> None:
    table = check_table(hub, NOW)
    lines = table.splitlines()
    assert lines[0].split() == [
        "MODEL",
        "ACCOUNT",
        "KEY",
        "STATUS",
        "WINDOWS",
        "used/limit",
        "EXHAUSTED_UNTIL",
    ]
    assert set(lines[1]) <= {"-", " "}
    assert "alpha/m1" in table
    assert str(hub.settings.registry_path) in lines[-1]
    assert "test-key-1" not in table
