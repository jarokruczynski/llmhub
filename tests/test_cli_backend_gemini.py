from __future__ import annotations

import json
from pathlib import Path

from llmhub.cli_backend import (
    CliRun,
    GeminiDialect,
    cli_env,
    completion_from,
    dialect_for,
    usage_block,
)
from llmhub.config import AccountDef, Entry, ModelDef, ProviderDef
from llmhub.providers_catalog import template

SESSION = "9c7258b0-b72b-46c9-bca8-cb8d172b6a15"
OUTPUT = json.dumps(
    {
        "session_id": SESSION,
        "response": "OK",
        "stats": {
            "models": {
                "gemini-3.8-flash": {
                    "api": {"totalRequests": 1},
                    "tokens": {
                        "input": 11468,
                        "prompt": 11468,
                        "candidates": 1,
                        "total": 11778,
                        "cached": 12,
                        "thoughts": 309,
                        "tool": 0,
                    },
                },
                "gemini-3.8-flash-lite": {
                    "tokens": {"prompt": 100, "candidates": 5, "thoughts": 0, "cached": 0}
                },
            }
        },
    }
)


def entry_for(model_id: str = "gemini-3.8-flash") -> Entry:
    return Entry(
        key=f"gemini-cli/{model_id}",
        provider_name="gemini-cli",
        provider=ProviderDef(kind="cli", command="gemini"),
        model=ModelDef(id=model_id, free={}),
        account=AccountDef(id="gemini-cli-jaro", api_key_env=None),
    )


def test_template_and_dialect_are_wired_together() -> None:
    spec = template("gemini-cli")
    assert spec is not None
    assert (spec["kind"], spec["command"], spec["api_key_env"]) == ("cli", "gemini", None)
    assert dialect_for(spec["id"]).id == "gemini-cli"


def test_argv_is_headless_read_only_and_never_trusts_the_workdir() -> None:
    args = GeminiDialect().build_args(
        "gemini",
        ProviderDef(kind="cli", command="gemini"),
        ModelDef(id="gemini-pro-latest"),
        prompt="say ok",
        timeout_s=60,
    )
    assert args[0] == "gemini"
    assert args[-2:] == ["-p", "say ok"]
    assert "--skip-trust" in args
    assert args[args.index("--approval-mode") + 1] == "plan"
    assert args[args.index("--model") + 1] == "gemini-pro-latest"
    assert "--yolo" not in args


def test_parse_sums_tokens_across_every_model_the_run_touched() -> None:
    payload = GeminiDialect().parse(f"warning: some prefix line\n{OUTPUT}")
    assert payload["response"] == "OK"
    assert payload["conversation_id"] == SESSION
    assert payload["usage"] == {
        "input_tokens": 11568,
        "output_tokens": 6,
        "thinking_tokens": 309,
        "cache_read_tokens": 12,
    }


def test_completion_carries_the_answer_and_the_reasoning_split() -> None:
    payload = GeminiDialect().parse(OUTPUT)
    completion = completion_from(entry_for(), payload)
    assert completion["choices"][0]["message"]["content"] == "OK"
    assert usage_block(payload)["completion_tokens_details"]["reasoning_tokens"] == 309


def test_an_empty_answer_is_a_failure_even_on_exit_zero() -> None:
    dialect = GeminiDialect()
    ok = CliRun(returncode=0, stdout=OUTPUT, stderr="", latency_ms=1)
    blank = CliRun(returncode=0, stdout=json.dumps({"session_id": SESSION}), stderr="", latency_ms=1)
    assert dialect.succeeded(ok, dialect.parse(ok.stdout)) is True
    assert dialect.succeeded(blank, dialect.parse(blank.stdout)) is False


def test_auth_hint_points_at_the_interactive_login() -> None:
    hint = GeminiDialect().auth_hint(entry_for(), classification=None)
    assert "gemini" in hint


def test_explicit_env_sets_values_the_parent_does_not_have(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/parent/home")
    monkeypatch.setenv("PATH", "/usr/bin")
    provider = ProviderDef(kind="cli", command="gemini", env={"HOME": "/second/licence"})
    env = cli_env(provider)
    assert env["HOME"] == "/second/licence"
    assert "/usr/bin" in env["PATH"]


def test_explicit_env_expands_a_leading_tilde(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/parent/home")
    provider = ProviderDef(kind="cli", command="gemini", env={"HOME": "~/homes/second"})
    assert cli_env(provider)["HOME"] == str(Path("~/homes/second").expanduser())


def test_env_deny_outranks_an_explicit_value(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/parent/home")
    provider = ProviderDef(
        kind="cli",
        command="gemini",
        env={"GEMINI_API_KEY": "leaked", "HOME": "/second/licence"},
        env_deny=["GEMINI_API_KEY"],
    )
    env = cli_env(provider)
    assert "GEMINI_API_KEY" not in env
    assert env["HOME"] == "/second/licence"


def test_two_blocks_of_one_cli_keep_separate_logins(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/parent/home")
    first = ProviderDef(kind="cli", command="gemini")
    second = ProviderDef(kind="cli", command="gemini", env={"HOME": "/second/licence"})
    assert cli_env(first)["HOME"] == "/parent/home"
    assert cli_env(second)["HOME"] == "/second/licence"
