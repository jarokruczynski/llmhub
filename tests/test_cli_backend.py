from __future__ import annotations

import json
import plistlib
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from llmhub.accounts import default_account_id
from llmhub.cli_backend import (
    COPILOT_DENY_TOOLS,
    DEFAULT_DIALECT,
    AntigravityDialect,
    CliRequestError,
    CopilotDialect,
    build_prompt,
    dialect_for,
    flatten_messages,
    parse_jsonl,
    parse_model_lines,
)
from llmhub.config import AccountDef, Entry, ModelDef, ProviderDef, Registry
from llmhub.providers_catalog import COPILOT_MODEL_IDS, COPILOT_MODEL_NOTE
from llmhub.providers_catalog import template as catalog_template
from llmhub.runtime import Hub
from llmhub.status import check_rows
from llmhub.vendor_errors import classify

# Never the real `agy`: every test drives this stand-in, which answers the same JSON shape.
# Its behaviour is switched by AGY_MODE, which only reaches it because the provider block
# lists it in env_passthrough - the same mechanism the real CLI gets HOME through.
FAKE_CLI = """#!/bin/sh
if [ "$1" = "models" ]; then
  echo "Fetching available models..."
  printf 'gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)\\n'
  printf 'claude-opus-4-6-thinking\\tClaude Opus 4.6 (Thinking)\\n'
  exit 0
fi
if [ -n "$AGY_LOG" ]; then
  {
    echo "CWD=$(pwd)"
    echo "PATH=$PATH"
    echo "LEAK=${LLMHUB_TEST_SECRET:-none}"
    prev=""
    for a in "$@"; do
      printf 'ARG=%s\n' "$(printf '%s' "$a" | tr '\n' '~')"
      if [ "$prev" = "--json-schema" ]; then echo "SCHEMA=$(cat "$a")"; fi
      prev="$a"
    done
  } >> "$AGY_LOG"
fi
case "${AGY_MODE:-ok}" in
  quota)
    echo '{"conversation_id":"c-q","status":"ERROR","response":"You have reached your rate limit. Try again later.","usage":{"input_tokens":120,"output_tokens":0}}'
    exit 1 ;;
  auth)
    echo "You are not signed in. Run agy and sign in." >&2
    exit 1 ;;
  boom)
    echo "panic: agent crashed" >&2
    exit 3 ;;
  slow)
    sleep 30
    echo '{"status":"SUCCESS","response":"late"}' ;;
  *)
    printf '%s\\n' '{"conversation_id":"cb14e8b3","status":"SUCCESS","response":"ok\\n","duration_seconds":1.3,"num_turns":1,"usage":{"input_tokens":7051,"output_tokens":31,"thinking_tokens":30,"cache_read_tokens":8129,"total_tokens":7082}}' ;;
esac
"""

MODEL = "antigravity/gemini-3.8-flash-low"
BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "hello there"}],
    "max_tokens": 64,
}


def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "agy-fake"
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(0o755)
    return path


def cli_block(command: Path, **overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "kind": "cli",
        "template": "antigravity",
        "command": str(command),
        "concurrency": 2,
        "timeout_s": 30,
        "env_passthrough": ["HOME", "PATH", "LANG", "AGY_MODE", "AGY_LOG"],
        "accounts": [{"id": "antigravity-main", "api_key_env": None}],
        "models": [
            {"id": "gemini-3.8-flash-low", "caps": ["text", "json", "reasoning"], "free": {}},
            {"id": "claude-opus-4-6-thinking", "caps": ["text", "json", "reasoning"], "free": {}},
        ],
    }
    block.update(overrides)
    return block


def register_cli(hub: Hub, command: Path, **overrides: Any) -> None:
    data = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    data["providers"]["antigravity"] = cli_block(command, **overrides)
    data["aliases"]["strong"] = {"require": ["text"], "prefer": [MODEL], "spread": 3}
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    hub.reload()


@pytest.fixture
def cli_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agy.log"
    monkeypatch.setenv("AGY_LOG", str(path))
    monkeypatch.setenv("LLMHUB_TEST_SECRET", "must-not-be-passed")
    monkeypatch.delenv("AGY_MODE", raising=False)
    return path


@pytest.fixture
def cli_hub(hub: Hub, tmp_path: Path, cli_log: Path) -> Hub:
    register_cli(hub, fake_cli(tmp_path))
    return hub


def log_values(path: Path, prefix: str) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line[len(prefix) + 1 :] for line in lines if line.startswith(f"{prefix}=")]


def test_prompt_flattening_puts_system_first_and_ends_on_the_assistant_cue() -> None:
    prompt = flatten_messages(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": [{"type": "text", "text": "three"}]},
        ]
    )
    assert prompt == "be terse\n\nUser: one\n\nAssistant: two\n\nUser: three\n\nAssistant:"

    with_json = build_prompt(
        {"messages": [{"role": "user", "content": "x"}], "response_format": {"type": "json_object"}}
    )
    assert with_json.startswith("Answer with one JSON object")
    assert with_json.endswith("User: x\n\nAssistant:")


async def test_image_parts_are_refused_as_text_only(cli_hub: Hub, client: httpx.AsyncClient) -> None:
    with pytest.raises(CliRequestError):
        flatten_messages([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                }
            ],
        },
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 400
    assert "text only" in response.text


async def test_success_maps_usage_and_runs_sandboxed_in_an_empty_workdir(
    cli_hub: Hub, client: httpx.AsyncClient, cli_log: Path
) -> None:
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["id"] == "chatcmpl-cb14e8b3"
    assert payload["model"] == "gemini-3.8-flash-low"
    assert payload["choices"][0]["message"]["content"] == "ok\n"
    assert payload["usage"]["prompt_tokens"] == 7051
    assert payload["usage"]["completion_tokens"] == 31
    assert payload["usage"]["total_tokens"] == 7082
    assert payload["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 8129
    assert response.headers["x-hub-model"] == MODEL
    assert response.headers["x-hub-account"] == "antigravity-main"
    assert response.headers["x-hub-attempts"] == f"{MODEL}:ok"

    args = log_values(cli_log, "ARG")
    assert args[:6] == ["--output-format", "json", "--sandbox", "--print-timeout", "30s", "--model"]
    assert args[6] == "gemini-3.8-flash-low"
    assert args[-2] == "-p"
    assert args[-1] == "User: hello there~~Assistant:"
    assert "--dangerously-skip-permissions" not in args

    workdir = cli_hub.settings.home / "cli-work" / "antigravity"
    assert log_values(cli_log, "CWD") == [str(workdir)]
    assert list(workdir.iterdir()) == []
    assert str(Path.home() / ".local" / "bin") in log_values(cli_log, "PATH")[0].split(":")
    # only the listed variables reach the child
    assert log_values(cli_log, "LEAK") == ["none"]

    rows = cli_hub.store.query("SELECT * FROM usage")
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["in_tokens"], rows[0]["out_tokens"]) == ("ok", 7051, 31)
    assert rows[0]["cached_tokens"] == 8129
    assert rows[0]["estimated"] == 0


async def test_json_schema_is_written_to_a_file_and_removed_afterwards(
    cli_hub: Hub, client: httpx.AsyncClient, cli_log: Path
) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    response = await client.post(
        "/v1/chat/completions",
        json=dict(
            BODY,
            response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
        ),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 200
    args = log_values(cli_log, "ARG")
    assert "--json-schema" in args
    assert json.loads(log_values(cli_log, "SCHEMA")[0]) == schema
    # the file lives in the workdir for the length of the run only
    assert list((cli_hub.settings.home / "cli-work" / "antigravity").iterdir()) == []


async def test_streaming_request_gets_one_chunk_and_done(cli_hub: Hub, client: httpx.AsyncClient) -> None:
    chunks: list[bytes] = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=dict(BODY, stream=True),
        headers={"X-Hub-App": "my-app"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-hub-model"] == MODEL
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    body = b"".join(chunks).decode()
    events = [line for line in body.split("\n\n") if line.strip()]
    assert len(events) == 2
    first = json.loads(events[0].removeprefix("data: "))
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["content"] == "ok\n"
    assert first["choices"][0]["finish_reason"] == "stop"
    assert first["usage"]["completion_tokens"] == 31
    assert events[1] == "data: [DONE]"
    assert cli_hub.store.query("SELECT * FROM usage")[0]["stream"] == 1


async def test_quota_failure_is_classified_and_parks_the_model(
    cli_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGY_MODE", "quota")
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502

    row = cli_hub.store.query("SELECT * FROM usage")[0]
    assert row["status"] == "quota"
    assert (row["in_tokens"], row["out_tokens"]) == (120, 0)
    exhausted = cli_hub.store.query("SELECT * FROM exhausted")
    assert [(item["account"], item["model"]) for item in exhausted] == [("antigravity-main", MODEL)]
    # a scopeless quota word from this vendor means its daily plan, not the next hour
    assert classify(None, "rate limit reached", "antigravity").scope == "daily"


async def test_signed_out_failure_is_auth_and_says_to_run_the_cli(
    cli_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGY_MODE", "auth")
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502

    row = cli_hub.store.query("SELECT * FROM usage")[0]
    assert row["status"] == "error"
    assert row["out_tokens"] == 0
    events = cli_hub.store.query("SELECT * FROM events WHERE kind = 'auth'")
    assert len(events) == 1
    assert "run `" in events[0]["message"]
    assert "sign in" in events[0]["message"]
    # no exhaustion marker: signing in again is the fix, not waiting for a window
    assert cli_hub.store.query("SELECT * FROM exhausted") == []


async def test_a_crashing_cli_is_an_error_with_a_cooldown(
    cli_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGY_MODE", "boom")
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502
    # the run failed on every candidate; the CLI's own body rides along under "last"
    error = response.json()["error"]["last"]["error"]
    assert error["type"] == "error"
    assert error["code"] == "cli_exit_3"
    assert "agent crashed" in error["stderr"]

    entry = cli_hub.registry.entry(MODEL, "antigravity-main")
    assert entry is not None
    assert cli_hub.router.in_cooldown(entry, datetime.now(UTC)) is not None
    assert cli_hub.store.query("SELECT * FROM usage")[0]["error_code"] == "cli_exit_3"


async def test_a_timeout_kills_the_process_group(
    hub: Hub, client: httpx.AsyncClient, tmp_path: Path, cli_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_cli(hub, fake_cli(tmp_path), timeout_s=1)
    monkeypatch.setenv("AGY_MODE", "slow")
    started = time.perf_counter()
    response = await client.post("/v1/chat/completions", json=BODY, headers={"X-Hub-App": "my-app"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 502
    assert response.json()["error"]["last"]["error"]["code"] == "cli_timeout"
    # the sleep is 30 s: the call came back on its own timeout, not on the child finishing
    assert elapsed < 10
    assert log_values(cli_log, "ARG")[4] == "1s"
    assert hub.store.query("SELECT * FROM usage")[0]["status"] == "error"


def test_config_validates_cli_blocks_and_fills_in_the_defaults(cli_hub: Hub) -> None:
    ok = Registry.model_validate(
        {"providers": {"agy": {"kind": "cli", "command": "agy", "models": [{"id": "m", "free": {}}]}}}
    )
    provider = ok.providers["agy"]
    assert provider.command == "agy"
    assert provider.env_passthrough == ["HOME", "PATH", "LANG"]
    assert provider.base_url == ""

    with pytest.raises(ValueError, match="cli needs a command"):
        Registry.model_validate({"providers": {"agy": {"kind": "cli"}}})
    with pytest.raises(ValueError, match="needs a base_url"):
        Registry.model_validate({"providers": {"nowhere": {"kind": "openai"}}})

    data = yaml.safe_load(cli_hub.settings.registry_path.read_text(encoding="utf-8"))
    block = data["providers"]["antigravity"]
    block.pop("concurrency")
    block.pop("timeout_s")
    cli_hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    cli_hub.reload()

    entry = cli_hub.registry.entry(MODEL, "antigravity-main")
    assert entry is not None
    assert entry.concurrency == 2
    assert entry.cli_timeout_s == 240.0
    assert entry.cli_workdir == cli_hub.settings.home / "cli-work" / "antigravity"
    assert entry.key_present is True
    assert cli_hub.router.semaphore(entry)._value == 2


async def test_vision_requirement_excludes_cli_candidates(cli_hub: Hub, client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=BODY,
        headers={"X-Hub-App": "my-app", "X-Hub-Require": "vision"},
    )
    assert response.status_code == 429
    rejected = response.json()["error"]["rejected"]
    assert [row["reason"] for row in rejected] == ["capability"]
    assert rejected[0]["model"] == MODEL


async def test_quick_add_registers_a_cli_template_without_a_key(
    hub: Hub, client: httpx.AsyncClient, tmp_path: Path, cli_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llmhub.providers_catalog import TEMPLATES_BY_ID

    command = fake_cli(tmp_path)
    monkeypatch.setitem(TEMPLATES_BY_ID["antigravity"], "command", str(command))
    monkeypatch.setitem(
        TEMPLATES_BY_ID["antigravity"],
        "env_passthrough",
        ["HOME", "PATH", "LANG", "AGY_MODE", "AGY_LOG"],
    )

    body = (await client.post("/api/accounts/quick", json={"source": "antigravity"})).json()
    assert body["provider"] == "antigravity"
    assert body["account_id"] == default_account_id("antigravity")
    assert body["created_provider"] is True
    assert body["discovered"] is False
    assert len(body["models"]) == 14
    assert body["test"]["ok"] is True
    assert body["test"]["model"] == "antigravity/gemini-3.8-flash-high"

    block = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))["providers"]["antigravity"]
    assert block["kind"] == "cli"
    assert block["command"] == str(command)
    assert "base_url" not in block
    assert block["accounts"] == [{"id": default_account_id("antigravity"), "api_key_env": None}]
    assert not list(hub.settings.env_dir.iterdir())
    # the probe asks for something an agent can answer, not a bare "ping"
    assert log_values(cli_log, "ARG")[-1] == "User: Say OK.~~Assistant:"

    test = (await client.post(f"/api/accounts/antigravity/{default_account_id('antigravity')}/test")).json()
    assert test["ok"] is True
    assert test["sample"].strip() == "ok"


async def test_discover_runs_the_models_subcommand(cli_hub: Hub, client: httpx.AsyncClient) -> None:
    body = (await client.post("/api/providers/antigravity/discover")).json()
    assert body["ok"] is True
    assert body["models"] == ["gemini-3.8-flash-low", "claude-opus-4-6-thinking"]
    assert body["count"] == 2
    assert body["registered"] == ["claude-opus-4-6-thinking", "gemini-3.8-flash-low"]

    # the progress line the CLI prints before the table is not a model id
    assert parse_model_lines("Fetching available models...\nm-1\tName\n") == ["m-1"]


def test_the_launch_agent_execs_the_venv_interpreter_itself() -> None:
    """macOS grants file access to the job's first program, so it cannot be a shell or uv.

    Under `/bin/zsh -lc ... uv run python`, the privacy database attributes the job to zsh
    (a platform binary, which is ungrantable) or to uv, and every agent CLI the hub spawns
    is then refused its own state directory - ~/.gemini for `agy` - with EPERM.
    """
    template = Path(__file__).resolve().parent.parent / "launchd" / "com.llmhub.gateway.plist.template"
    job = plistlib.loads(template.read_bytes())
    argv = job["ProgramArguments"]

    assert argv[0] == "__REPO__/.venv/bin/python3"
    assert argv[1:3] == ["-m", "llmhub"]
    assert not any(part in ("/bin/sh", "/bin/zsh", "/bin/bash", "uv") for part in argv)
    assert job["WorkingDirectory"] == "__REPO__"
    # no login shell runs, so the environment the CLI children inherit comes from here
    assert set(job["EnvironmentVariables"]) >= {"HOME", "PATH", "LANG"}


def test_check_shows_cli_instead_of_a_key_flag(cli_hub: Hub) -> None:
    rows = {(row["key"], row["account"]): row for row in check_rows(cli_hub)}
    assert rows[(MODEL, "antigravity-main")]["key_present"] == "cli"
    assert rows[("alpha/m1", "alpha-1")]["key_present"] is True


# --- copilot: a second dialect (JSONL output, fenced tools, credit budget) -----------------

# Verbatim (trimmed) stdout of copilot 1.0.83 for `-p "User: Say OK.\n\nAssistant:"
# --output-format json`, captured 2026-09-08. Everything the parser has to survive is in
# here: dotted event types, the prompt echoed back in user.message.data.content, a streaming
# delta, an assistant.message whose only content is a tool request, the final answer in
# assistant.message.data.content, and counters that are running session totals.
COPILOT_STREAM = """{"type":"session.auto_mode_resolved","data":{"chosenModel":"mai-code-1.1-flash","routingMethod":"auto_v2"},"id":"d232ab20-b239-4853-bbf4-84e4df9a9954"}
{"type":"session.mcp_servers_loaded","data":{"servers":[{"name":"github-mcp-server","status":"disabled","source":"builtin"}]},"ephemeral":true,"id":"0af8fd6a-487a-4b03-b518-c3b779212aa7"}
{"type":"user.message","data":{"content":"User: hello there\\n\\nAssistant:","messageId":"f868e2c8-22dc-487b-89b0-fba4a1c1ba7d","turnId":"0"},"id":"12151ab6-0e51-4c39-b508-148da9c8589e"}
{"type":"assistant.turn_start","data":{"turnId":"0"},"id":"656351c6-66f7-4ab1-bb00-23da4d95a10d"}
{"type":"model.call_start","data":{"turnId":"0","model":"mai-code-1.1-flash"},"ephemeral":true,"id":"7b659fdd-f382-4c8c-9ed0-e2a00f6ca17d"}
{"type":"assistant.message_delta","data":{"messageId":"74dfd791","deltaContent":"OK"},"ephemeral":true,"id":"eed16443-0f5a-4710-87cc-336c6516d13f"}
{"type":"assistant.message","data":{"messageId":"74dfd791","model":"mai-code-1.1-flash","content":"","toolRequests":[{"toolCallId":"call_CvQ","name":"bash","arguments":{"command":"whoami"}}],"turnId":"0"},"id":"aa000000-0000-0000-0000-000000000001"}
{"type":"assistant.message","data":{"messageId":"74dfd791","model":"mai-code-1.1-flash","content":"OK.","toolRequests":[],"turnId":"0","phase":"final_answer","rte":true},"id":"bc358515-4bf4-430e-961a-66e9199d6d25"}
{"type":"assistant.turn_end","data":{"turnId":"0"},"id":"2a8b8f29-78d2-4f44-8869-506e29fb38ad"}
{"type":"session.usage_checkpoint","data":{"totalNanoAiu":223542000,"totalPremiumRequests":1,"promptCacheBreakState":[{"conversation":"main","models":{"mai-code-1.1-flash":{"vendor":"openai","prompt_tokens":13535,"cache_read":1280,"cache_write":0,"tool_tokens":8226}}}]},"id":"0683b6f3-d966-490e-9609-ded07308e2b4"}
{"type":"assistant.idle","data":{},"ephemeral":true,"id":"f6188624-9277-4b23-9b74-564c55524bce"}
{"type":"result","timestamp":"2026-09-08T08:23:35.912Z","sessionId":"201e61be-0c0b-4d92-9e7a-3edff6f7cc55","exitCode":0,"usage":{"premiumRequests":1,"totalApiDurationMs":2275,"sessionDurationMs":3569,"codeChanges":{"linesAdded":0,"linesRemoved":0,"filesModified":[]}}}"""

# Stand-in for the real `copilot`, switched by COPILOT_MODE. The ok branch replays the
# capture above, with one line of progress prose and one half-written line mixed in.
FAKE_COPILOT = """#!/bin/sh
if [ -n "$COPILOT_LOG" ]; then
  {
    echo "CWD=$(pwd)"
    echo "GHTOKEN=${GITHUB_TOKEN:-none}"
    echo "ALLOWALL=${COPILOT_ALLOW_ALL:-none}"
    for a in "$@"; do
      printf 'ARG=%s\n' "$(printf '%s' "$a" | tr '\n' '~')"
    done
  } >> "$COPILOT_LOG"
fi
case "${COPILOT_MODE:-ok}" in
  auth)
    echo "Error: You are not logged in. Run 'copilot login' to authenticate." >&2
    exit 1 ;;
  licence)
    echo "Error: access denied, this account is not entitled to GitHub Copilot." >&2
    exit 1 ;;
  quota)
    echo '{"type":"error","error":"You have used all available AI credits for this billing period."}'
    exit 1 ;;
  silent)
    echo 'starting up'
    echo '{"type":"result","sessionId":"sess-empty","exitCode":0,"usage":{"premiumRequests":1}}'
    ;;
  *)
    echo 'thinking about it...'
    cat <<'JSONL'
__STREAM__
JSONL
    echo '{ half a line'
    ;;
esac
""".replace("__STREAM__", COPILOT_STREAM)

COPILOT_MODEL = "copilot/auto"
COPILOT_BODY = {
    "model": COPILOT_MODEL,
    "messages": [{"role": "user", "content": "hello there"}],
    "max_tokens": 64,
}


def fake_copilot(tmp_path: Path) -> Path:
    path = tmp_path / "copilot-fake"
    path.write_text(FAKE_COPILOT, encoding="utf-8")
    path.chmod(0o755)
    return path


def copilot_block(command: Path, **overrides: Any) -> dict[str, Any]:
    known = catalog_template("copilot")
    block: dict[str, Any] = {
        "kind": "cli",
        "template": "copilot",
        "command": str(command),
        "timeout_s": 30,
        # GITHUB_TOKEN is on both lists on purpose: env_deny has to win
        "env_passthrough": ["HOME", "PATH", "LANG", "COPILOT_MODE", "COPILOT_LOG", "GITHUB_TOKEN"],
        "env_deny": list(known["env_deny"]),
        "accounts": [{"id": "copilot-main", "api_key_env": None}],
        "models": [
            {
                "id": model["id"],
                "caps": list(model["caps"]),
                "free": {},
                "max_ai_credits": model["max_ai_credits"],
            }
            for model in known["models"]
            if model["id"] in ("auto", "claude-opus-5")
        ],
    }
    block.update(overrides)
    return block


def register_copilot(hub: Hub, command: Path, **overrides: Any) -> None:
    data = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))
    data["providers"]["copilot"] = copilot_block(command, **overrides)
    data["aliases"]["copilot"] = {
        "require": ["text"],
        "prefer": [COPILOT_MODEL, "copilot/claude-opus-5"],
        "spread": 1,
    }
    hub.settings.registry_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    hub.reload()


@pytest.fixture
def copilot_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "copilot.log"
    monkeypatch.setenv("COPILOT_LOG", str(path))
    monkeypatch.setenv("GITHUB_TOKEN", "personal-account-token")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "1")
    monkeypatch.delenv("COPILOT_MODE", raising=False)
    return path


@pytest.fixture
def copilot_hub(hub: Hub, tmp_path: Path, copilot_log: Path) -> Hub:
    register_copilot(hub, fake_copilot(tmp_path))
    return hub


def flag_values(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, item in enumerate(args[:-1]) if item == flag]


def copilot_entry(command: str = "copilot") -> Entry:
    """A registry entry as the live block writes it, for the parts that read the command name."""
    return Entry(
        key="copilot/auto",
        provider_name="copilot",
        provider=ProviderDef(kind="cli", command=command),
        model=ModelDef(id="auto", free={}, max_ai_credits=60),
        account=AccountDef(id="copilot-main", api_key_env=None),
    )


def test_the_dialect_comes_from_the_template_and_antigravity_is_the_fallback() -> None:
    assert isinstance(dialect_for("copilot"), CopilotDialect)
    assert isinstance(dialect_for("antigravity"), AntigravityDialect)
    # a hand-written provider block with no template, or one the catalog dropped
    assert dialect_for("some-other-cli") is DEFAULT_DIALECT
    assert dialect_for(None) is DEFAULT_DIALECT
    assert isinstance(DEFAULT_DIALECT, AntigravityDialect)

    provider = ProviderDef(kind="cli", command="agy")
    model = ModelDef(id="gemini-3.8-flash-low", free={})
    args = DEFAULT_DIALECT.build_args("/bin/agy", provider, model, prompt="hi", timeout_s=30)
    assert args == [
        "/bin/agy",
        "--output-format",
        "json",
        "--sandbox",
        "--print-timeout",
        "30s",
        "--model",
        "gemini-3.8-flash-low",
        "-p",
        "hi",
    ]


async def test_copilot_argv_fences_the_agent_and_repeats_deny_tool(
    copilot_hub: Hub, client: httpx.AsyncClient, copilot_log: Path
) -> None:
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 200

    args = log_values(copilot_log, "ARG")
    assert args[:6] == ["-p", "User: hello there~~Assistant:", "--output-format", "json", "--model", "auto"]
    assert args[6:14] == [
        "--allow-all-tools",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-remote-export",
        "--no-custom-instructions",
        "--no-auto-update",
        "--no-color",
    ]
    assert flag_values(args, "--log-level") == ["none"]
    # the flag is variadic, so each tool needs its own pair or it would eat what follows
    assert args.count("--deny-tool") == len(COPILOT_DENY_TOOLS)
    assert flag_values(args, "--deny-tool") == list(COPILOT_DENY_TOOLS)
    # the names have to be the ones the CLI actually registers; a guess denies nothing
    assert "bash" in COPILOT_DENY_TOOLS and "create" in COPILOT_DENY_TOOLS
    assert flag_values(args, "--max-ai-credits") == ["60"]
    for never in ("--allow-all", "--sandbox", "--print-timeout", "--add-dir", "--json-schema"):
        assert never not in args
    assert log_values(copilot_log, "CWD") == [str(copilot_hub.settings.home / "cli-work" / "copilot")]


def test_copilot_max_ai_credits_is_clamped_to_the_cli_minimum() -> None:
    provider = ProviderDef(kind="cli", command="copilot")
    dialect = dialect_for("copilot")

    small = dialect.build_args(
        "/bin/copilot", provider, ModelDef(id="auto", max_ai_credits=5), prompt="x", timeout_s=30
    )
    assert flag_values(small, "--max-ai-credits") == ["30"]

    unset = dialect.build_args("/bin/copilot", provider, ModelDef(id="auto"), prompt="x", timeout_s=30)
    assert "--max-ai-credits" not in unset


def test_copilot_jsonl_parsing_reads_the_real_1_0_83_stream() -> None:
    payload = parse_jsonl("thinking about it...\n" + COPILOT_STREAM + "\n{ half a line\n")

    # the answer, not the prompt echoed back in user.message and not the tool-request turn
    assert payload["response"] == "OK."
    assert payload["num_turns"] == 1
    assert payload["conversation_id"] == "201e61be-0c0b-4d92-9e7a-3edff6f7cc55"
    # tokens live in the checkpoint's cache bookkeeping; there is no output count at all
    assert payload["usage"] == {"input_tokens": 13535, "cache_read_tokens": 1280}
    assert payload["ai_credits"] == 0.223542
    assert payload["premium_requests"] == 1

    # the counters are running session totals, so two checkpoints must not double the bill
    twice = parse_jsonl(COPILOT_STREAM + "\n" + COPILOT_STREAM)
    assert twice["ai_credits"] == 0.223542
    assert twice["usage"]["input_tokens"] == 13535


def test_copilot_jsonl_parsing_survives_junk_and_reads_every_answer_shape() -> None:
    stream = "\n".join(
        [
            "starting up",
            '{"type":"session","session_id":"sess-1"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"first"},'
            '{"type":"text","text":"second"}],"role":"assistant"},'
            '"usage":{"input_tokens":10,"output_tokens":2}}',
            "{ not json",
            '{"response":"via response"}',
            '{"content":"via content"}',
            '{"text":"via text","usage":{"prompt_tokens":5,"completion_tokens":1,'
            '"thinking_tokens":3,"ai_credits":0.25}}',
            "",
        ]
    )
    payload = parse_jsonl(stream)

    assert payload["response"] == "via text"
    assert payload["conversation_id"] == "sess-1"
    assert payload["num_turns"] == 4
    assert payload["usage"] == {"input_tokens": 15, "output_tokens": 3, "thinking_tokens": 3}
    assert payload["ai_credits"] == 0.25

    # each shape on its own; a turn the CLI only echoed back is not an answer
    assert (
        parse_jsonl('{"type":"assistant","message":{"content":[{"type":"text","text":"a"}]}}')["response"]
        == "a"
    )
    assert parse_jsonl('{"response":"b"}')["response"] == "b"
    assert parse_jsonl('{"content":"c"}')["response"] == "c"
    assert parse_jsonl('{"text":"d"}')["response"] == "d"
    assert parse_jsonl('{"type":"user","message":{"content":[{"type":"text","text":"e"}]}}')["response"] == ""
    assert parse_jsonl('{"type":"user.message","data":{"content":"e"}}')["response"] == ""
    assert parse_jsonl("not json at all\n")["response"] == ""


async def test_copilot_success_maps_the_stream_onto_a_completion(
    copilot_hub: Hub, client: httpx.AsyncClient
) -> None:
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 200
    payload = response.json()

    assert payload["id"] == "chatcmpl-201e61be-0c0b-4d92-9e7a-3edff6f7cc55"
    assert payload["model"] == "auto"
    assert payload["choices"][0]["message"]["content"] == "OK."
    assert payload["usage"]["prompt_tokens"] == 13535
    assert payload["usage"]["completion_tokens"] == 0
    assert payload["usage"]["total_tokens"] == 13535
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 1280
    assert payload["hub_cli"]["ai_credits"] == 0.223542
    assert payload["hub_cli"]["premium_requests"] == 1
    assert response.headers["x-hub-model"] == COPILOT_MODEL

    row = copilot_hub.store.query("SELECT * FROM usage")[0]
    assert (row["status"], row["in_tokens"], row["out_tokens"]) == ("ok", 13535, 0)


async def test_a_stream_with_no_answer_is_an_error_not_a_blank_completion(
    copilot_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_MODE", "silent")
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})

    assert response.status_code == 502
    assert response.json()["error"]["last"]["error"]["code"] == "cli_exit_0"
    entry = copilot_hub.registry.entry(COPILOT_MODEL, "copilot-main")
    assert entry is not None
    assert copilot_hub.router.in_cooldown(entry, datetime.now(UTC)) is not None


async def test_copilot_takes_a_json_schema_as_an_instruction_not_a_flag(
    copilot_hub: Hub, client: httpx.AsyncClient, copilot_log: Path
) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    response = await client.post(
        "/v1/chat/completions",
        json=dict(
            COPILOT_BODY,
            response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
        ),
        headers={"X-Hub-App": "my-app"},
    )
    assert response.status_code == 200

    args = log_values(copilot_log, "ARG")
    assert "--json-schema" not in args
    prompt = args[1].replace("~", "\n")
    assert prompt.startswith("Answer with one JSON object matching this JSON Schema")
    assert json.dumps(schema) in prompt
    assert prompt.endswith("User: hello there\n\nAssistant:")
    # nothing was written next to the agent: there is no schema file to clean up
    assert list((copilot_hub.settings.home / "cli-work" / "copilot").iterdir()) == []


async def test_env_deny_keeps_github_token_out_of_the_child(
    copilot_hub: Hub, client: httpx.AsyncClient, copilot_log: Path
) -> None:
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 200
    # both are set in this process and GITHUB_TOKEN is even in env_passthrough; env_deny wins
    assert log_values(copilot_log, "GHTOKEN") == ["none"]
    assert log_values(copilot_log, "ALLOWALL") == ["none"]


async def test_copilot_auth_failure_names_the_login_command(
    copilot_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_MODE", "auth")
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502
    # one row, so the ladder did not run the CLI again: an auth failure is not retryable
    assert len(copilot_hub.store.query("SELECT * FROM usage")) == 1

    events = copilot_hub.store.query("SELECT * FROM events WHERE kind = 'auth'")
    assert len(events) == 1
    # the event names the configured command, which in the live registry is a bare `copilot`
    assert events[0]["message"].endswith(" login` with the licensed account")
    hint = dialect_for("copilot").auth_hint(
        copilot_entry(), classify(None, "you are not logged in", "copilot")
    )
    assert hint == "copilot/auto: run `copilot login` with the licensed account"
    # signing in is the fix: no exhaustion marker, but the model is parked for a cooldown
    assert copilot_hub.store.query("SELECT * FROM exhausted") == []
    entry = copilot_hub.registry.entry(COPILOT_MODEL, "copilot-main")
    assert entry is not None
    assert copilot_hub.router.in_cooldown(entry, datetime.now(UTC)) is not None


async def test_a_missing_copilot_licence_is_auth_with_its_own_hint(
    copilot_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_MODE", "licence")
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502

    found = classify(None, "access denied, this account is not entitled", "copilot")
    assert (found.kind, found.rule) == ("auth", "copilot-not-entitled")
    events = copilot_hub.store.query("SELECT * FROM events WHERE kind = 'auth'")
    assert "no Copilot CLI entitlement" in events[0]["message"]


async def test_copilot_credit_exhaustion_is_a_monthly_quota(
    copilot_hub: Hub, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_MODE", "quota")
    response = await client.post("/v1/chat/completions", json=COPILOT_BODY, headers={"X-Hub-App": "my-app"})
    assert response.status_code == 502

    row = copilot_hub.store.query("SELECT * FROM usage")[0]
    assert row["status"] == "quota"
    exhausted = copilot_hub.store.query("SELECT * FROM exhausted")
    assert [(item["account"], item["model"]) for item in exhausted] == [("copilot-main", COPILOT_MODEL)]
    # credits and premium requests renew with the billing month, not at midnight
    found = classify(None, "you have used all available AI credits", "copilot")
    assert (found.kind, found.rule, found.scope) == ("quota", "copilot-credits", "monthly")


async def test_copilot_discover_is_a_400_because_the_cli_lists_nothing(
    copilot_hub: Hub, client: httpx.AsyncClient
) -> None:
    response = await client.post("/api/providers/copilot/discover")
    assert response.status_code == 400
    assert "no model listing" in response.json()["detail"]
    assert "template" in response.json()["detail"]


async def test_copilot_quick_add_registers_the_template_without_a_key(
    hub: Hub, client: httpx.AsyncClient, tmp_path: Path, copilot_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llmhub.providers_catalog import TEMPLATES_BY_ID

    command = fake_copilot(tmp_path)
    monkeypatch.setitem(TEMPLATES_BY_ID["copilot"], "command", str(command))
    monkeypatch.setitem(
        TEMPLATES_BY_ID["copilot"],
        "env_passthrough",
        ["HOME", "PATH", "LANG", "COPILOT_MODE", "COPILOT_LOG", "GITHUB_TOKEN"],
    )

    body = (await client.post("/api/accounts/quick", json={"source": "github copilot"})).json()
    assert body["provider"] == "copilot"
    assert body["account_id"] == default_account_id("copilot")
    assert body["created_provider"] is True
    assert body["discovered"] is False
    assert len(body["models"]) == len(COPILOT_MODEL_IDS)
    assert body["test"]["ok"] is True
    assert body["test"]["model"] == "copilot/auto"

    block = yaml.safe_load(hub.settings.registry_path.read_text(encoding="utf-8"))["providers"]["copilot"]
    assert block["kind"] == "cli"
    assert block["accounts"] == [{"id": default_account_id("copilot"), "api_key_env": None}]
    assert block["env_deny"] == [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_ALLOW_ALL",
    ]
    assert [model["max_ai_credits"] for model in block["models"]] == [60] * len(COPILOT_MODEL_IDS)
    assert not list(hub.settings.env_dir.iterdir())


def test_the_catalog_copilot_template_ships_a_fixed_model_list() -> None:
    known = catalog_template("copilot")

    assert (known["kind"], known["command"], known["api_key_env"]) == ("cli", "copilot", None)
    assert known["aliases"] == ["copilot", "github copilot", "gh copilot"]
    assert known["hostnames"] == ["github.com", "githubcopilot.com"]
    assert known["docs_url"] == "https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli"
    assert known["quota_scope_default"] == "monthly"
    assert known["env_deny"] == [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_ALLOW_ALL",
    ]
    assert known["extra_args"] == []
    ids = [model["id"] for model in known["models"]]
    assert ids[0] == "auto"
    # ids come from `copilot help config`, so they use dots and never a dashed version suffix
    assert {"claude-opus-5", "claude-sonnet-4.6", "claude-haiku-4.5", "gpt-5-mini", "grok-4.5"} <= set(ids)
    assert not [name for name in ids if re.search(r"-\d+-\d+$", name)]
    assert len(ids) == len(set(ids))
    for model in known["models"]:
        assert model["caps"] == ["text", "json", "reasoning"]
        assert model["free"] == {}
        assert model["max_ai_credits"] == 60
        assert model["notes"] == COPILOT_MODEL_NOTE
    assert "not the owner's to spend freely" in known["notes"]


async def test_the_auto_alias_does_not_reach_for_copilot(copilot_hub: Hub, client: httpx.AsyncClient) -> None:
    """The registry keeps copilot out of auto/vision/fast/strong: it is opt-in per call."""
    selection = copilot_hub.router.select(model_request="auto")
    keys = [entry.key for entry in selection.candidates]

    assert selection.candidates[0].provider_name != "copilot"
    assert all(entry.provider_name != "copilot" for entry in selection.candidates[: selection.spread])
    # a prefer list ranks, it does not gate: copilot sits behind the whole pool as a last
    # resort, so keeping it out of the alias is what keeps ordinary traffic off the licence
    assert keys[-2:] == [COPILOT_MODEL, "copilot/claude-opus-5"]

    # its own alias is the opposite: spread 1, so the two copilot models come first and the
    # free pool is only there to catch a failure
    own = copilot_hub.router.select(model_request="copilot")
    assert [entry.key for entry in own.candidates[:2]] == [COPILOT_MODEL, "copilot/claude-opus-5"]
