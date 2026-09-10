from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

WINDOW_NAMES = ("hourly", "daily", "monthly", "allowance")
RESETTING_WINDOWS = ("hourly", "daily", "monthly")
DEFAULT_HOME = Path.home() / ".llmhub"
DEFAULT_ENV_DIR = DEFAULT_HOME / "env"
# pre-1.0 default, kept only as a fallback for installs that predate DEFAULT_ENV_DIR
LEGACY_ENV_DIR = Path.home() / ".detektor"

# Providers that are a local agent CLI instead of an HTTP endpoint: no base_url, no key, the
# login lives in the CLI's own config. See cli_backend.py.
CLI_KIND = "cli"
CLI_DEFAULT_CONCURRENCY = 2
CLI_DEFAULT_TIMEOUT_S = 240.0
CLI_DEFAULT_ENV_PASSTHROUGH = ("HOME", "PATH", "LANG")

# How many of the eligible candidates an alias spreads over when the yaml says nothing.
# Free tiers are per model, so walking one prefer list strictly parks all traffic on the
# first entry and leaves the rest of the pool idle. 1 = strict prefer order.
DEFAULT_SPREAD: dict[str, int] = {"auto": 4, "vision": 4, "fast": 4}


def as_utc(value: date | datetime | None, tz_name: str = "UTC") -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.combine(value, time(0, 0))
    if moment.tzinfo is None:
        from .quota import zone

        moment = moment.replace(tzinfo=zone(tz_name))
    return moment.astimezone(UTC)


class QuotaWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_tokens: int | None = None
    out_tokens: int | None = None
    total_tokens: int | None = None
    # a call count, not a token count: most free tiers meter requests per day, and a window
    # that only caps requests says nothing about how big any one of them may be
    requests: int | None = None
    expires_at: date | datetime | None = None

    def limits(self) -> dict[str, int]:
        return {
            field: value
            for field, value in (
                ("in_tokens", self.in_tokens),
                ("out_tokens", self.out_tokens),
                ("total_tokens", self.total_tokens),
                ("requests", self.requests),
            )
            if value is not None
        }


class ModelDef(BaseModel):
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    id: str
    caps: list[str] = Field(default_factory=list)
    context: int | None = None
    free: dict[str, QuotaWindow] | None = None
    reset_tz: str = "UTC"
    activated_at: date | datetime | None = None
    concurrency: int | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    # kind: cli only - per-session soft cap for a CLI that bills in credits, not tokens
    max_ai_credits: int | None = None
    # declared per-request ceilings; absent = unknown, never a static fallback (see Entry.input_ceiling)
    max_request_tokens: int | None = None
    max_output_tokens: int | None = None
    # None = inherit the provider's value; see ProviderDef.opt_in_only
    opt_in_only: bool | None = None

    @field_validator("free")
    @classmethod
    def _known_windows(cls, value: dict[str, QuotaWindow] | None) -> dict[str, QuotaWindow] | None:
        if value is None:
            return None
        unknown = set(value) - set(WINDOW_NAMES)
        if unknown:
            raise ValueError(f"unknown quota window(s): {sorted(unknown)}")
        return value

    @property
    def is_free(self) -> bool:
        return self.free is not None

    @property
    def windows(self) -> dict[str, QuotaWindow]:
        return self.free or {}

    @property
    def allowance_windows(self) -> dict[str, QuotaWindow]:
        return {name: spec for name, spec in self.windows.items() if name == "allowance"}

    def expires_at(self) -> datetime | None:
        stamps = [
            as_utc(spec.expires_at, self.reset_tz)
            for spec in self.allowance_windows.values()
            if spec.expires_at is not None
        ]
        return min(stamps) if stamps else None


class AccountDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    api_key_env: str | None = None
    activated_at: date | datetime | None = None


class ProviderDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str = "openai"
    base_url: str = ""
    accounts: list[AccountDef] = Field(default_factory=list)
    models: list[ModelDef] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    # kind: cli only
    command: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    concurrency: int | None = None
    timeout_s: float | None = None
    workdir: str | None = None
    env_passthrough: list[str] = Field(default_factory=lambda: list(CLI_DEFAULT_ENV_PASSTHROUGH))
    # names the child must never see, even when env_passthrough or the parent env carries them:
    # an inherited GITHUB_TOKEN would silently pick a different identity than the CLI's login
    env_deny: list[str] = Field(default_factory=list)
    # Keep this provider out of every alias pool. An alias pool is the whole registry and
    # `prefer` only ranks it, so without this an entry nobody preferred is still tried once
    # everything ahead of it is exhausted - which is how metered or licensed capacity gets
    # spent by a request that never asked for it. Such an entry is reachable two ways only:
    # an explicit provider/model_id request, or an alias whose own prefer list names it.
    opt_in_only: bool = False

    @model_validator(mode="after")
    def _endpoint_present(self) -> ProviderDef:
        """A provider needs somewhere to send the request: a base_url, or a cli command."""
        if self.kind == CLI_KIND:
            if not (self.command or "").strip():
                raise ValueError("kind: cli needs a command")
        elif not (self.base_url or "").strip():
            raise ValueError(f"kind: {self.kind} needs a base_url")
        return self


class AliasDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    require: list[str] = Field(default_factory=list)
    prefer: list[str] = Field(default_factory=list)
    spread: int | None = None
    # A requirement: the model's declared context must be known and at least this wide. An app
    # that reserves output tokens on top of a long input asked for a guarantee, and a model
    # whose context nobody wrote down cannot give one - so unknown is rejected, not assumed.
    min_context: int | None = None
    # Preferences, never filters: an entry carrying an avoided cap, or measured slower than
    # max_latency_ms, sorts below the rest of the pool but stays in it as a fallback.
    avoid: list[str] = Field(default_factory=list)
    max_latency_ms: int | None = None

    def spread_for(self, name: str) -> int:
        value = self.spread if self.spread is not None else DEFAULT_SPREAD.get(name, 1)
        return max(1, int(value))


class Registry(BaseModel):
    model_config = ConfigDict(extra="allow")

    providers: dict[str, ProviderDef] = Field(default_factory=dict)
    aliases: dict[str, AliasDef] = Field(default_factory=dict)

    def entries(self) -> Iterator[Entry]:
        for provider_name, provider in self.providers.items():
            for model in provider.models:
                for account in provider.accounts:
                    yield Entry(
                        key=f"{provider_name}/{model.id}",
                        provider_name=provider_name,
                        provider=provider,
                        model=model,
                        account=account,
                    )

    def entry(self, key: str, account_id: str | None = None) -> Entry | None:
        for entry in self.entries():
            if entry.key == key and (account_id is None or entry.account.id == account_id):
                return entry
        return None

    def keys(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.key not in seen:
                seen.append(entry.key)
        return seen


@dataclass(frozen=True)
class Entry:
    key: str
    provider_name: str
    provider: ProviderDef
    model: ModelDef
    account: AccountDef

    @property
    def account_id(self) -> str:
        return self.account.id

    @property
    def kind(self) -> str:
        return self.provider.kind

    @property
    def is_cli(self) -> bool:
        return self.provider.kind == CLI_KIND

    @property
    def opt_in_only(self) -> bool:
        """Model knob first, then the provider one."""
        if self.model.opt_in_only is not None:
            return bool(self.model.opt_in_only)
        return bool(self.provider.opt_in_only)

    @property
    def concurrency(self) -> int | None:
        """Model knob first, then the provider one; a cli provider is capped even when silent."""
        for value in (self.model.concurrency, self.provider.concurrency):
            if value and value > 0:
                return int(value)
        return CLI_DEFAULT_CONCURRENCY if self.is_cli else None

    @property
    def cli_command(self) -> str:
        return (self.provider.command or "").strip()

    @property
    def cli_timeout_s(self) -> float:
        value = self.provider.timeout_s
        return float(value) if value and value > 0 else CLI_DEFAULT_TIMEOUT_S

    @property
    def cli_workdir(self) -> Path:
        """Process cwd for the agent: empty by default, so the CLI sees no files of ours."""
        raw = (self.provider.workdir or "").strip()
        if raw:
            return Path(raw).expanduser()
        home = Path(os.environ.get("LLMHUB_HOME", str(DEFAULT_HOME)))
        return home / "cli-work" / self.provider_name

    @property
    def template_id(self) -> str:
        """Catalog template this provider came from; the provider name when it says nothing."""
        value = (self.provider.model_extra or {}).get("template")
        return str(value) if value else self.provider_name

    @property
    def base_url(self) -> str:
        return self.provider.base_url.rstrip("/")

    @property
    def api_key(self) -> str | None:
        if not self.account.api_key_env:
            return None
        return os.environ.get(self.account.api_key_env)

    @property
    def activated_at(self) -> datetime | None:
        return as_utc(
            self.model.activated_at if self.model.activated_at is not None else self.account.activated_at,
            self.model.reset_tz,
        )

    @property
    def key_present(self) -> bool:
        if not self.account.api_key_env:
            return True
        return bool(os.environ.get(self.account.api_key_env))

    def input_ceiling(self, est_out: int, observed: int | None) -> int | None:
        """Smallest known bound on the request's input tokens, or None when nothing is known.

        Absent everywhere must stay unknown rather than falling back to some default: a static
        fallback would silently hide capacity the same way a missing `context` used to (see
        the request size awareness note in DESIGN.md).
        """
        candidates = [self.model.max_request_tokens]
        if self.model.context is not None:
            room = self.model.context - est_out
            if room > 0:
                candidates.append(room)
        if observed is not None:
            candidates.append(observed)
        known = [value for value in candidates if value is not None]
        return min(known) if known else None


def _positive_int(env: dict[str, str], name: str, default: int) -> int:
    try:
        return max(1, int(env.get(name, str(default))))
    except ValueError:
        log.warning("bad %s value, using %d", name, default)
        return default


def _non_negative_int(env: dict[str, str], name: str, default: int) -> int:
    """Same as above for a knob where 0 says something: a ceiling that is switched off."""
    try:
        return max(0, int(env.get(name, str(default))))
    except ValueError:
        log.warning("bad %s value, using %d", name, default)
        return default


def _resolve_env_dir(env: dict[str, str]) -> Path:
    """Directory scanned for provider key files (`*.env`).

    LLMHUB_ENV_DIR always wins. Otherwise this is DEFAULT_ENV_DIR; an install that
    predates that default falls back to LEGACY_ENV_DIR when that is the only one that
    exists, with a one-time warning telling the operator to either move the files or
    set LLMHUB_ENV_DIR to keep using the old location on purpose.
    """
    override = env.get("LLMHUB_ENV_DIR")
    if override:
        return Path(override)
    if not DEFAULT_ENV_DIR.is_dir() and LEGACY_ENV_DIR.is_dir():
        log.warning(
            "env dir %s not found, using legacy %s; move its *.env files to %s or "
            "set LLMHUB_ENV_DIR=%s to keep this location",
            DEFAULT_ENV_DIR,
            LEGACY_ENV_DIR,
            DEFAULT_ENV_DIR,
            LEGACY_ENV_DIR,
        )
        return LEGACY_ENV_DIR
    return DEFAULT_ENV_DIR


@dataclass(frozen=True)
class Settings:
    home: Path
    registry_path: Path
    db_path: Path
    token: str | None
    env_dir: Path
    job_workers: int = 6
    # optional hard ceiling of running jobs per app; 0 = none. The everyday limit is the fair
    # share the dispatcher computes from the number of apps with runnable work.
    jobs_per_app: int = 0
    jobs_per_app_min: int = 1
    # a job that has not run by created_at + ttl_s leaves the queue as `expired`
    job_ttl_s: int = 6 * 3600
    job_ttl_max_s: int = 36 * 3600
    # a job holds its worker slot for the request timeout plus this margin; past that, with no
    # live worker renewing it, the lease is lost and the job goes back to the queue
    job_lease_margin_s: int = 60
    queue_max_per_app: int = 200
    job_retention_days: int = 7
    # how long an (account, model) pair stays parked after the vendor refused the route itself.
    # not_found is durable - the model is not on this key's plan - so it is parked for days; a
    # provider-side outage reported as a 4xx clears on its own, so minutes.
    not_found_ttl_s: int = 7 * 24 * 3600
    unavailable_ttl_s: int = 600
    # wall clock one sync or stream call may spend walking the candidate pool. The client read
    # timeout is 120 s, so this has to leave the response time to get back.
    run_budget_s: int = 90
    # the scout talks to the hub through its own OpenAI wire, so it goes through the same
    # routing, quota accounting and free-only policy as any other client
    hub_base_url: str = "http://127.0.0.1:8800/v1"
    scout_at: str | None = "08:00"
    scout_sources_path: Path | None = None

    @property
    def scout_sources(self) -> Path:
        return self.scout_sources_path or (self.home / "scout_sources.yaml")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        home = Path(env.get("LLMHUB_HOME", str(DEFAULT_HOME)))
        registry_path = Path(env.get("LLMHUB_REGISTRY", str(home / "providers.yaml")))
        db_path = Path(env.get("LLMHUB_DB", str(home / "hub.db")))
        token = env.get("LLMHUB_TOKEN") or None
        env_dir = _resolve_env_dir(env)
        scout_sources = env.get("LLMHUB_SCOUT_SOURCES")
        scout_at = env.get("LLMHUB_SCOUT_AT", "08:00").strip()
        return cls(
            home=home,
            registry_path=registry_path,
            db_path=db_path,
            token=token,
            env_dir=env_dir,
            job_workers=_positive_int(env, "LLMHUB_JOB_WORKERS", 6),
            jobs_per_app=_non_negative_int(env, "LLMHUB_JOBS_PER_APP", 0),
            jobs_per_app_min=_positive_int(env, "LLMHUB_JOBS_PER_APP_MIN", 1),
            job_ttl_s=_positive_int(env, "LLMHUB_JOB_TTL_S", 6 * 3600),
            job_ttl_max_s=_positive_int(env, "LLMHUB_JOB_TTL_MAX_S", 36 * 3600),
            job_lease_margin_s=_positive_int(env, "LLMHUB_JOB_LEASE_MARGIN_S", 60),
            queue_max_per_app=_positive_int(env, "LLMHUB_QUEUE_MAX_PER_APP", 200),
            job_retention_days=_positive_int(env, "LLMHUB_JOB_RETENTION_DAYS", 7),
            not_found_ttl_s=_positive_int(env, "LLMHUB_NOT_FOUND_TTL_S", 7 * 24 * 3600),
            unavailable_ttl_s=_positive_int(env, "LLMHUB_UNAVAILABLE_TTL_S", 600),
            run_budget_s=_positive_int(env, "LLMHUB_RUN_BUDGET_S", 90),
            hub_base_url=env.get("LLMHUB_BASE_URL", "http://127.0.0.1:8800/v1").rstrip("/"),
            scout_at=scout_at or None,
            scout_sources_path=Path(scout_sources) if scout_sources else None,
        )


def example_registry_path() -> Path:
    repo_copy = Path(__file__).resolve().parent.parent / "providers.example.yaml"
    if repo_copy.is_file():
        return repo_copy
    return Path(__file__).resolve().parent / "data" / "providers.example.yaml"


def example_path(filename: str) -> Path:
    """Ship-alongside example file: repo root when running from a checkout, package data
    in an installed wheel (see the hatch force-include block in pyproject.toml)."""
    repo_copy = Path(__file__).resolve().parent.parent / filename
    if repo_copy.is_file():
        return repo_copy
    return Path(__file__).resolve().parent / "data" / filename


def load_registry(path: Path) -> Registry:
    if not path.exists():
        example = example_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, path)
        log.info("registry missing, copied example %s -> %s", example, path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"registry {path} must be a mapping")
    return Registry.model_validate(raw)
