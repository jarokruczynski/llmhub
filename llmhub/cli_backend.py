"""Agent CLIs as hub backends (`kind: cli`).

Some free paths are not an HTTP API but a coding-agent CLI with a headless print mode. The
backend flattens an OpenAI chat request into one prompt, runs the CLI as a subprocess in an
empty working directory with a trimmed environment, and maps its JSON output back to a
`chat.completion`. Everything else - routing, quota windows, usage rows, headers - is the
normal gateway path.

Every CLI has its own flags and its own idea of what JSON on stdout looks like, so those two
parts live in a `CliDialect` picked by the provider's catalog template; the call itself is one
code path. `parse` normalises whatever the CLI printed onto one field set, so the completion
mapper below does not care which program ran.

Security posture: the process gets an empty cwd, no api keys and only the environment
variables the provider block lists minus the ones it denies. The tool-granting escape hatches
(`--dangerously-skip-permissions`, `--allow-all`) are never passed, and the argument vector
goes to execve directly, never through a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CLI_DEFAULT_ENV_PASSTHROUGH, DEFAULT_HOME, Entry, ModelDef, ProviderDef
from .router import UpstreamError
from .runtime import Hub
from .vendor_errors import Classification, classify

log = logging.getLogger(__name__)

# The CLIs the owner installs land here, and the LaunchAgent's PATH does not carry it.
LOCAL_BIN = Path.home() / ".local" / "bin"

PROBE_PROMPT = "Say OK."
JSON_OBJECT_INSTRUCTION = "Answer with one JSON object and nothing else, no prose, no code fence."
JSON_SCHEMA_INSTRUCTION = "Answer with one JSON object matching this JSON Schema, nothing else:"
ASSISTANT_CUE = "Assistant:"
TEXT_ONLY = "cli providers take text only"
MAX_ERROR_TEXT = 2000
DISCOVER_TIMEOUT_S = 60.0


class CliRequestError(Exception):
    """The request cannot be expressed as a CLI prompt, or the CLI is not usable (400)."""


@dataclass
class CliRun:
    returncode: int
    stdout: str
    stderr: str
    latency_ms: int
    timed_out: bool = False


@dataclass
class CliResult:
    """Same shape as the HTTP CallResult, so the gateway serves both the same way."""

    entry: Entry
    status_code: int
    payload: dict[str, Any] | None
    raw: bytes
    latency_ms: int
    usage_id: int
    media_type: str = "application/json"
    extra: dict[str, Any] = field(default_factory=dict)


# --- request -> one prompt ----------------------------------------------------------------


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in ("image_url", "input_image", "image") or "image_url" in item:
            raise CliRequestError(TEXT_ONLY)
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def flatten_messages(messages: Any) -> str:
    """System messages first, then the turns, then a bare `Assistant:` line to answer on."""
    systems: list[str] = []
    turns: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").lower()
        body = text_of(message.get("content")).strip()
        if role in ("system", "developer"):
            if body:
                systems.append(body)
        elif role == "assistant":
            turns.append(f"{ASSISTANT_CUE} {body}".rstrip())
        else:
            turns.append(f"User: {body}".rstrip())
    return "\n\n".join([*systems, *turns, ASSISTANT_CUE])


def response_schema(body: dict[str, Any]) -> dict[str, Any] | None:
    fmt = body.get("response_format")
    if not isinstance(fmt, dict) or str(fmt.get("type")) != "json_schema":
        return None
    spec = fmt.get("json_schema")
    if isinstance(spec, dict):
        schema = spec.get("schema")
        return schema if isinstance(schema, dict) else spec
    return None


def wants_json_object(body: dict[str, Any]) -> bool:
    fmt = body.get("response_format")
    return isinstance(fmt, dict) and str(fmt.get("type")) == "json_object"


def build_prompt(body: dict[str, Any], schema: dict[str, Any] | None = None) -> str:
    """The whole request as one prompt; `schema` is for a CLI that has no schema flag."""
    prompt = flatten_messages(body.get("messages"))
    if schema is not None:
        prompt = f"{JSON_SCHEMA_INSTRUCTION}\n{json.dumps(schema)}\n\n{prompt}"
    elif wants_json_object(body):
        prompt = f"{JSON_OBJECT_INSTRUCTION}\n\n{prompt}"
    return prompt


# --- process plumbing ---------------------------------------------------------------------


def cli_env(provider: ProviderDef) -> dict[str, str]:
    """Only the listed variables minus the denied ones, plus ~/.local/bin on PATH.

    `env_deny` wins over `env_passthrough`: a name on both lists does not reach the child.
    That is how a CLI with its own login store is kept away from an ambient GITHUB_TOKEN
    belonging to a different identity. (launchd's PATH lacks ~/.local/bin, hence the append.)
    """
    names = provider.env_passthrough or list(CLI_DEFAULT_ENV_PASSTHROUGH)
    denied = set(provider.env_deny)
    env = {name: os.environ[name] for name in names if name not in denied and os.environ.get(name)}
    parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    if str(LOCAL_BIN) not in parts:
        parts.append(str(LOCAL_BIN))
    env["PATH"] = os.pathsep.join(parts)
    return env


def default_workdir(provider_name: str) -> Path:
    home = Path(os.environ.get("LLMHUB_HOME", str(DEFAULT_HOME)))
    return home / "cli-work" / provider_name


def workdir_of(provider_name: str, provider: ProviderDef) -> Path:
    raw = (provider.workdir or "").strip()
    return Path(raw).expanduser() if raw else default_workdir(provider_name)


def prepare_workdir(path: Path) -> Path:
    """Create the cwd the agent runs in, and refuse anything that looks like a real checkout."""
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists() or (path / "pyproject.toml").exists():
        raise CliRequestError(f"cli workdir {path} is a source tree; point workdir somewhere empty")
    return path


def resolve_command(command: str, env: dict[str, str]) -> str:
    """An absolute path is taken as is; a bare name is looked up on the child's own PATH."""
    if not command:
        raise CliRequestError("cli provider has no command")
    candidate = Path(command).expanduser()
    if candidate.is_absolute():
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            raise CliRequestError(f"cli command {candidate} is not executable")
        return str(candidate)
    found = shutil.which(command, path=env.get("PATH"))
    if not found:
        raise CliRequestError(f"cli command {command!r} not found on PATH {env.get('PATH')}")
    return found


def print_timeout(timeout_s: float) -> str:
    return f"{max(1, int(round(timeout_s)))}s"


def kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def run_cli(args: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float) -> CliRun:
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        # the agent starts children of its own, so the whole process group goes, not the parent
        kill_group(process)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return CliRun(-1, "", f"timed out after {timeout_s:g}s", latency_ms, timed_out=True)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return CliRun(
        int(process.returncode or 0),
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        latency_ms,
    )


def parse_json_object(text: str) -> dict[str, Any]:
    """The payload, even when the CLI prefixed it with progress lines."""
    stripped = text.strip()
    if not stripped:
        return {}
    with contextlib.suppress(ValueError):
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    decoder = json.JSONDecoder()
    index = stripped.find("{")
    while index != -1:
        with contextlib.suppress(ValueError):
            parsed, _ = decoder.raw_decode(stripped[index:])
            if isinstance(parsed, dict):
                return parsed
        index = stripped.find("{", index + 1)
    return {}


def jsonl_objects(text: str) -> list[dict[str, Any]]:
    """Every stdout line that is a JSON object; progress prose and half-written lines drop out."""
    objects: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                objects.append(parsed)
    return objects


def content_text(value: Any) -> str | None:
    """A message body that is either a plain string or a list of `{type: text, text: ...}`."""
    if isinstance(value, str):
        return value or None
    if not isinstance(value, list):
        return None
    parts = [
        part["text"]
        for part in value
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
        and str(part.get("type") or "text") == "text"
    ]
    return "\n".join(parts) if parts else None


ASSISTANT_TEXT_KEYS = ("response", "content", "text")
ASSISTANT_ROLE = "assistant"


def is_answer_object(obj: dict[str, Any]) -> bool:
    """Whether this object may hold the final answer.

    The stream carries the prompt back (`user.message` with the whole prompt in
    `data.content`), plus tool, session and model events. Reading text out of any of those
    would hand the caller its own prompt as the answer, so only an object that names itself
    assistant - or one with no type at all, the plain `{"response": ...}` shape - qualifies.
    Event types are dotted (`assistant.message`, `assistant.message_delta`), hence the split.
    """
    kind = str(obj.get("type") or obj.get("role") or "").lower()
    return not kind or kind.split(".", 1)[0] == ASSISTANT_ROLE


def assistant_text(obj: dict[str, Any]) -> str | None:
    """The assistant text in one JSONL object, across the shapes the CLI is known to print."""
    if not is_answer_object(obj):
        return None
    for holder in (obj.get("data"), obj.get("message")):
        if isinstance(holder, dict):
            found = content_text(holder.get("content"))
            if found is not None:
                return found
    for key in ASSISTANT_TEXT_KEYS:
        found = content_text(obj.get(key))
        if found is not None:
            return found
    return None


# Token counts under whichever name the CLI uses, folded onto the field names usage_block reads.
USAGE_TOKEN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input_tokens", ("input_tokens", "prompt_tokens", "input")),
    ("output_tokens", ("output_tokens", "completion_tokens", "output")),
    ("total_tokens", ("total_tokens", "total")),
    ("thinking_tokens", ("thinking_tokens", "reasoning_tokens", "thoughts_tokens")),
    ("cache_read_tokens", ("cache_read_tokens", "cached_tokens", "cache_read_input_tokens")),
)
USAGE_CREDIT_KEYS = ("ai_credits", "credits", "premium_requests")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def sum_usage(objects: list[dict[str, Any]]) -> tuple[dict[str, int], float]:
    """Token counts summed over every `usage` block in the stream, plus the credits spent."""
    tokens: dict[str, int] = {}
    credits = 0.0
    for obj in objects:
        block = obj.get("usage")
        if not isinstance(block, dict):
            continue
        for name, keys in USAGE_TOKEN_FIELDS:
            for key in keys:
                value = _number(block.get(key))
                if value is not None:
                    tokens[name] = tokens.get(name, 0) + int(value)
                    break
        for key in USAGE_CREDIT_KEYS:
            value = _number(block.get(key))
            if value is not None:
                credits += value
                break
    return tokens, credits


# What Copilot 1.0.83 actually reports, and where. Everything here is a running session
# total, so the highest value seen wins - summing them would multiply one call's cost by the
# number of checkpoints the stream happened to print.
CHECKPOINT_TYPE = "session.usage_checkpoint"
RESULT_TYPE = "result"
NANO_PER_CREDIT = 1_000_000_000
SESSION_ID_KEYS = ("sessionId", "session_id", "conversation_id", "id")


def _highest(current: float, value: Any) -> float:
    found = _number(value)
    return max(current, found) if found is not None else current


def session_totals(objects: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, float]]:
    """Copilot's own counters: nano-AIU spent, premium requests, and the prompt tokens.

    Token counts are not in a `usage` block at all - they sit per model inside the usage
    checkpoint's cache bookkeeping, and only for the input side. There is no output count,
    so `completion_tokens` stays 0 for this vendor.
    """
    prompt_tokens: dict[str, int] = {}
    cached_tokens: dict[str, int] = {}
    nano_aiu = 0.0
    premium = 0.0
    for obj in objects:
        kind = str(obj.get("type") or "")
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if kind == CHECKPOINT_TYPE:
            nano_aiu = _highest(nano_aiu, data.get("totalNanoAiu"))
            premium = _highest(premium, data.get("totalPremiumRequests"))
            for state in data.get("promptCacheBreakState") or []:
                models = state.get("models") if isinstance(state, dict) else None
                for name, row in (models or {}).items():
                    if not isinstance(row, dict):
                        continue
                    prompt_tokens[name] = int(_highest(prompt_tokens.get(name, 0), row.get("prompt_tokens")))
                    cached_tokens[name] = int(_highest(cached_tokens.get(name, 0), row.get("cache_read")))
        elif kind == RESULT_TYPE:
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            premium = _highest(premium, usage.get("premiumRequests"))
    tokens = {
        name: total
        for name, total in (
            ("input_tokens", sum(prompt_tokens.values())),
            ("cache_read_tokens", sum(cached_tokens.values())),
        )
        if total
    }
    billing: dict[str, float] = {}
    if nano_aiu:
        billing["ai_credits"] = round(nano_aiu / NANO_PER_CREDIT, 6)
    if premium:
        billing["premium_requests"] = int(premium) if float(premium).is_integer() else round(premium, 4)
    return tokens, billing


def parse_jsonl(text: str) -> dict[str, Any]:
    """A JSONL stream folded into the one payload shape the completion mapper reads."""
    objects = jsonl_objects(text)
    answers = [found for found in (assistant_text(obj) for obj in objects) if found is not None]
    tokens, credits = sum_usage(objects)
    counters, billing = session_totals(objects)
    for name, value in counters.items():
        tokens.setdefault(name, value)
    payload: dict[str, Any] = {
        "response": answers[-1] if answers else "",
        "num_turns": len(answers),
        "usage": tokens,
    }
    for key in SESSION_ID_KEYS:
        found = next((obj[key] for obj in objects if isinstance(obj.get(key), str)), None)
        if found:
            payload["conversation_id"] = found
            break
    errors = [str(obj["error"]) for obj in objects if obj.get("error")]
    if errors:
        payload["error"] = errors[-1]
    if "ai_credits" in billing:
        payload["ai_credits"] = billing["ai_credits"]
    elif credits:
        payload["ai_credits"] = round(credits, 4)
    if "premium_requests" in billing:
        payload["premium_requests"] = billing["premium_requests"]
    return payload


# --- dialects: what each CLI is called with, and what its stdout means --------------------


class CliDialect:
    """Argv shape and stdout parser for one family of CLI, picked by the catalog template."""

    id = "antigravity"
    # a CLI without a schema flag gets the schema as an instruction inside the prompt
    schema_in_prompt = False
    # a CLI without a `models` subcommand answers discovery out of its template instead
    lists_models = True

    def build_args(
        self,
        executable: str,
        provider: ProviderDef,
        model: ModelDef,
        *,
        prompt: str,
        timeout_s: float,
        schema_path: Path | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str) -> dict[str, Any]:
        raise NotImplementedError

    def succeeded(self, run: CliRun, payload: dict[str, Any]) -> bool:
        return run.returncode == 0

    def auth_hint(self, entry: Entry, classification: Classification) -> str:
        command = entry.cli_command
        return f"{entry.key}: {command} is not signed in; run `{command}` in a terminal and sign in"


class AntigravityDialect(CliDialect):
    """`agy -p ... --output-format json --sandbox`: one JSON object with a `status` field."""

    id = "antigravity"

    def build_args(
        self,
        executable: str,
        provider: ProviderDef,
        model: ModelDef,
        *,
        prompt: str,
        timeout_s: float,
        schema_path: Path | None = None,
    ) -> list[str]:
        args = [executable, *[str(item) for item in provider.extra_args]]
        args += ["--output-format", "json", "--sandbox", "--print-timeout", print_timeout(timeout_s)]
        if model.id:
            args += ["--model", model.id]
        if schema_path is not None:
            args += ["--json-schema", str(schema_path)]
        args += ["-p", prompt]
        return args

    def parse(self, stdout: str) -> dict[str, Any]:
        return parse_json_object(stdout)

    def succeeded(self, run: CliRun, payload: dict[str, Any]) -> bool:
        return run.returncode == 0 and str(payload.get("status") or "").upper() == "SUCCESS"


# Non-interactive mode needs --allow-all-tools, which on its own hands a coding agent the
# shell, so every tool the CLI registers is denied back. These are the real names it loads,
# read off a live run. The CLI also accepts a few legacy aliases (`shell` still blocks
# `bash`), but aliases are undocumented and the real names cover every registered tool, so
# only real names are listed. The flag is variadic, hence one repeated pair per tool rather
# than `--deny-tool bash view ...`, which would swallow the flag that follows.
# Measured: denying them blocks execution but does not unregister them, so the tool schemas
# still cost ~8.2k prompt tokens per call and the price per call does not change.
COPILOT_DENY_TOOLS = (
    "bash",
    "read_bash",
    "stop_bash",
    "list_bash",
    "view",
    "create",
    "edit",
    "web_fetch",
    "fetch_copilot_cli_documentation",
    "skill",
    "sql",
    "session_store_sql",
    "read_agent",
    "list_agents",
    "write_agent",
    "grep",
    "glob",
    "task",
)
# fences that keep the run to "answer the prompt": no questions back, no MCP servers, no
# remote session, no repo instructions, no self-update, no colour codes, no log files
COPILOT_FENCES = (
    "--allow-all-tools",
    "--no-ask-user",
    "--disable-builtin-mcps",
    "--no-remote",
    "--no-remote-export",
    "--no-custom-instructions",
    "--no-auto-update",
    "--no-color",
)
COPILOT_MIN_AI_CREDITS = 30
COPILOT_LOGIN_HINT = "run `{command} login` with the licensed account"
COPILOT_LICENCE_HINT = "this login has no Copilot CLI entitlement; check the seat's licence before retrying"
NOT_ENTITLED_RULE = "copilot-not-entitled"


class CopilotDialect(CliDialect):
    """`copilot -p ... --output-format json`: JSONL, one object per line, tools fenced off."""

    id = "copilot"
    schema_in_prompt = True
    lists_models = False

    def build_args(
        self,
        executable: str,
        provider: ProviderDef,
        model: ModelDef,
        *,
        prompt: str,
        timeout_s: float,
        schema_path: Path | None = None,
    ) -> list[str]:
        args = [executable, "-p", prompt, "--output-format", "json"]
        if model.id:
            args += ["--model", model.id]
        args += [*COPILOT_FENCES, "--log-level", "none"]
        for tool in COPILOT_DENY_TOOLS:
            args += ["--deny-tool", tool]
        if model.max_ai_credits:
            # the CLI rejects anything under its own minimum, so a smaller number in the
            # registry would fail every call instead of capping it
            args += ["--max-ai-credits", str(max(COPILOT_MIN_AI_CREDITS, int(model.max_ai_credits)))]
        args += [str(item) for item in provider.extra_args]
        return args

    def parse(self, stdout: str) -> dict[str, Any]:
        return parse_jsonl(stdout)

    def succeeded(self, run: CliRun, payload: dict[str, Any]) -> bool:
        # exit 0 with no answer in the stream means its shape moved under us; a loud error
        # with a cooldown beats handing the caller a blank completion
        return run.returncode == 0 and bool(payload.get("response"))

    def auth_hint(self, entry: Entry, classification: Classification) -> str:
        if classification.rule == NOT_ENTITLED_RULE:
            return f"{entry.key}: {COPILOT_LICENCE_HINT}"
        return f"{entry.key}: {COPILOT_LOGIN_HINT.format(command=entry.cli_command)}"


DIALECTS: dict[str, CliDialect] = {
    dialect.id: dialect for dialect in (AntigravityDialect(), CopilotDialect())
}
DEFAULT_DIALECT = DIALECTS["antigravity"]


def dialect_for(template_id: str | None) -> CliDialect:
    """The template's dialect; anything unknown gets the single-JSON-object one."""
    return DIALECTS.get(str(template_id or ""), DEFAULT_DIALECT)


def provider_template(provider_name: str, provider: ProviderDef) -> str:
    """Same rule as Entry.template_id, for the paths that hold a bare provider block."""
    value = (provider.model_extra or {}).get("template")
    return str(value) if value else provider_name


# --- output -> chat.completion ------------------------------------------------------------


def usage_block(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("usage")
    usage = raw if isinstance(raw, dict) else {}

    def count(name: str) -> int:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    prompt_tokens = count("input_tokens")
    completion_tokens = count("output_tokens")
    total = count("total_tokens") or prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total,
        "prompt_tokens_details": {"cached_tokens": count("cache_read_tokens")},
        "completion_tokens_details": {"reasoning_tokens": count("thinking_tokens")},
    }


def completion_from(entry: Entry, payload: dict[str, Any]) -> dict[str, Any]:
    conversation = str(payload.get("conversation_id") or uuid.uuid4().hex)
    content = payload.get("response")
    meta: dict[str, Any] = {
        "conversation_id": payload.get("conversation_id"),
        "num_turns": payload.get("num_turns"),
        "duration_seconds": payload.get("duration_seconds"),
    }
    for name in ("ai_credits", "premium_requests"):
        # not tokens: what this vendor actually bills on, so it goes back to the caller
        if payload.get(name) is not None:
            meta[name] = payload[name]
    return {
        "id": f"chatcmpl-{conversation}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": entry.model.id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content if isinstance(content, str) else ""},
                "finish_reason": "stop",
            }
        ],
        "usage": usage_block(payload),
        "hub_cli": meta,
    }


def sse_chunks(completion: dict[str, Any]) -> list[bytes]:
    """No real streaming: the whole answer as one chunk, then [DONE]."""
    choice = (completion.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content")) or ""
    chunk = {
        "id": completion.get("id"),
        "object": "chat.completion.chunk",
        "created": completion.get("created"),
        "model": completion.get("model"),
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": completion.get("usage"),
    }
    return [b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n", b"data: [DONE]\n\n"]


# --- failures -----------------------------------------------------------------------------


def failure_classification(entry: Entry, run: CliRun, payload: dict[str, Any]) -> Classification:
    """Vendor error table first; anything it does not recognise is a plain error."""
    if run.timed_out:
        return Classification("error", "cli_timeout", run.stderr, "cli-timeout")
    pieces = [
        str(payload.get("error") or ""),
        str(payload.get("message") or ""),
        str(payload.get("response") or ""),
        run.stderr,
        run.stdout,
    ]
    text = "\n".join(piece for piece in pieces if piece)[:MAX_ERROR_TEXT]
    found = classify(None, text, entry.template_id)
    if found.rule is not None:
        return found
    message = (run.stderr.strip() or run.stdout.strip() or f"exit {run.returncode}")[:500]
    return Classification("error", f"cli_exit_{run.returncode}", message)


def error_body(entry: Entry, classification: Classification, run: CliRun) -> str:
    return json.dumps(
        {
            "error": {
                "message": classification.message or classification.kind,
                "type": classification.kind,
                "code": classification.code,
                "provider": entry.provider_name,
                "model": entry.key,
                "exit_code": run.returncode,
                "stderr": run.stderr[:500],
            }
        }
    )


# --- the call -----------------------------------------------------------------------------


async def call_cli(
    hub: Hub, entry: Entry, body: dict[str, Any], app: str, attempt_no: int, *, stream: bool = False
) -> CliResult:
    """One CLI run, mapped onto the result shape and usage bookkeeping of an HTTP call."""
    dialect = dialect_for(entry.template_id)
    env = cli_env(entry.provider)
    executable = resolve_command(entry.cli_command, env)
    workdir = prepare_workdir(entry.cli_workdir)
    schema = response_schema(body)
    prompt = build_prompt(body, schema if schema is not None and dialect.schema_in_prompt else None)
    schema_path: Path | None = None
    if schema is not None and not dialect.schema_in_prompt:
        schema_path = workdir / f"schema-{uuid.uuid4().hex}.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
    args = dialect.build_args(
        executable,
        entry.provider,
        entry.model,
        prompt=prompt,
        timeout_s=entry.cli_timeout_s,
        schema_path=schema_path,
    )
    try:
        run = await run_cli(args, cwd=workdir, env=env, timeout_s=entry.cli_timeout_s)
    finally:
        if schema_path is not None:
            with contextlib.suppress(OSError):
                schema_path.unlink()

    payload = dialect.parse(run.stdout)
    if not dialect.succeeded(run, payload):
        classification = failure_classification(entry, run, payload)
        row_status = "quota" if classification.is_quota else "error"
        usage_id = hub.store.start_usage(
            app=app,
            provider=entry.provider_name,
            account=entry.account_id,
            model=entry.key,
            status=row_status,
            latency_ms=run.latency_ms,
            attempt=attempt_no,
            error_code=classification.code,
            stream=stream,
        )
        # a refusal still costs the agent's input tokens; out is 0 by definition here
        hub.store.update_usage_tokens(usage_id, in_tokens=usage_block(payload)["prompt_tokens"], out_tokens=0)
        if classification.kind == "auth":
            hub.store.add_event(
                kind="auth",
                message=dialect.auth_hint(entry, classification),
                app=app,
                model=entry.key,
                account=entry.account_id,
            )
        else:
            hub.store.add_event(
                kind=row_status,
                message=f"{entry.key} cli {classification.code or classification.kind}: "
                f"{classification.message}"[:500],
                app=app,
                model=entry.key,
                account=entry.account_id,
            )
        if classification.kind == "error":
            # the router raises an `error` straight to the caller, so the cooldown is set here
            hub.router.set_cooldown(entry)
        raise UpstreamError(classification, None, error_body(entry, classification, run), run.latency_ms)

    completion = completion_from(entry, payload)
    usage_id = hub.store.start_usage(
        app=app,
        provider=entry.provider_name,
        account=entry.account_id,
        model=entry.key,
        status="ok",
        latency_ms=run.latency_ms,
        attempt=attempt_no,
        stream=stream,
    )
    usage = completion["usage"]
    hub.store.update_usage_tokens(
        usage_id,
        in_tokens=usage["prompt_tokens"],
        out_tokens=usage["completion_tokens"],
        cached_tokens=usage["prompt_tokens_details"]["cached_tokens"],
        total_tokens=usage["total_tokens"],
    )
    return CliResult(
        entry=entry,
        status_code=200,
        payload=completion,
        raw=json.dumps(completion).encode("utf-8"),
        latency_ms=run.latency_ms,
        usage_id=usage_id,
    )


# --- discovery ----------------------------------------------------------------------------


def parse_model_lines(stdout: str) -> list[str]:
    """First column of `<command> models`; progress prose has neither a tab nor a lone token."""
    ids: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" in line:
            name = line.split("\t", 1)[0].strip()
        elif len(line.split()) == 1:
            name = line
        else:
            continue
        if name and name not in ids:
            ids.append(name)
    return ids


NO_MODEL_LISTING = "this CLI has no model listing; models come from the template"


async def discover_cli_models(
    provider_name: str, provider: ProviderDef, timeout_s: float = DISCOVER_TIMEOUT_S
) -> tuple[list[str], CliRun]:
    dialect = dialect_for(provider_template(provider_name, provider))
    if not dialect.lists_models:
        # checked before the binary is resolved: the answer is about the CLI's feature set,
        # not about whether this machine has it installed
        raise CliRequestError(f"{provider_name}: {NO_MODEL_LISTING}")
    env = cli_env(provider)
    executable = resolve_command((provider.command or "").strip(), env)
    workdir = prepare_workdir(workdir_of(provider_name, provider))
    args = [executable, *[str(item) for item in provider.extra_args], "models"]
    run = await run_cli(args, cwd=workdir, env=env, timeout_s=timeout_s)
    if run.returncode != 0:
        return [], run
    return parse_model_lines(run.stdout), run
