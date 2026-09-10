from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from .config import CLI_KIND, Registry, Settings
from .providers_catalog import (
    PROVIDER_TEMPLATES,
    catalog_context,
    chat_model_ids,
    context_discover_spec,
    discover_spec,
    is_chat_model_id,
    missing_fields,
    normalize_model_id,
    render_base_url,
    template,
)

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DISCOVER_TIMEOUT = 10.0


class AccountError(Exception):
    """Bad input from the dashboard form. Carries the HTTP status the API should answer with."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class SourceUnresolved(AccountError):
    """Quick add could not name a provider. `needs` says what would settle it.

    `needs: ["source"]` - nothing recognisable, or two templates answered equally well and the
    tie is listed in `guess`. `needs: ["base_url"]` - the vendor has a name but no template, so
    the hub knows who it is and not where to call.
    """

    def __init__(self, message: str, needs: list[str], guess: list[str] | None = None) -> None:
        super().__init__(message, 422)
        self.needs = list(needs)
        self.guess = list(guess or [])


def normalize_api_key(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if not text:
        raise AccountError("api_key is empty")
    if any(char.isspace() for char in text):
        raise AccountError("api_key must not contain whitespace or newlines")
    return text


def check_name(value: str, what: str) -> str:
    text = (value or "").strip()
    if not NAME_RE.match(text):
        raise AccountError(f"{what} must match {NAME_RE.pattern}")
    return text


def default_env_name(provider: str) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    return f"{stem}_API_KEY"


def check_env_name(value: str) -> str:
    text = (value or "").strip()
    if not ENV_NAME_RE.match(text):
        raise AccountError(f"api_key_env must match {ENV_NAME_RE.pattern}")
    return text


def provider_slug(value: str) -> str:
    """Turn a free-text vendor name or a hostname into something check_name() accepts."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug


def default_account_id(provider: str) -> str:
    """`<provider>-<owner>` used when quick add gets no explicit account_id.

    The owner segment is the OS login (`getpass.getuser()`), so a fresh checkout on
    someone else's machine defaults to their own username instead of the original
    author's. Falls back to "main" when the OS reports no username at all.
    """
    try:
        owner = getpass.getuser() or "main"
    except OSError:
        owner = "main"
    return f"{provider}-{provider_slug(owner) or 'main'}"


# --- quick add: figuring out which provider a pasted key belongs to ------------------------

URL_RE = re.compile(r"(?:https?://|www\.)[^\s'\"<>()\[\],]+", re.IGNORECASE)
GENERIC_HOSTS = ("github.com", "reddit.com", "x.com", "twitter.com", "medium.com")

# Prefixes the vendors put in front of their own keys. 'anthropic' has no template on purpose:
# the hub has no free Anthropic endpoint, so that prefix resolves to a name and a 422 asking
# for base_url rather than to a provider it could call.
KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sk-or-v1-", "openrouter"),
    ("sk-ant-", "anthropic"),
    ("nvapi-", "nvidia-nim"),
    ("gsk_", "groq"),
    ("csk-", "cerebras"),
    ("xpl_", "explabs"),
    ("hf_", "huggingface"),
    ("AIza", "gemini"),
)

CONFIDENCE: dict[str, float] = {
    "promo": 0.95,
    "id": 1.0,
    "alias": 0.9,
    "hostname": 0.85,
    "token": 0.6,
    "key_prefix": 0.5,
}

# 'custom' has an empty base_url: it can never be the answer to "who is this key from".
MATCHABLE: tuple[dict[str, Any], ...] = tuple(item for item in PROVIDER_TEMPLATES if item["id"] != "custom")


def _norm(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _phrase_in(text: str, phrase: str) -> bool:
    """Whole-word match, so 'nim' does not fire inside 'minimax' and 'ai' inside 'chain'."""
    if not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def urls_in(text: str | None) -> list[str]:
    hosts: list[str] = []
    for match in URL_RE.findall(text or ""):
        raw = match if "://" in match else f"https://{match}"
        host = (urlsplit(raw).hostname or "").lower().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def provider_name_from_url(url: str | None) -> str:
    """Last-resort provider name for a base_url the catalog does not know: api.foo.test -> foo."""
    hosts = urls_in(url) or ([str(url).strip().lower()] if url else [])
    labels = [
        label for label in (hosts[0] if hosts else "").split(".") if label and label not in ("api", "www")
    ]
    return provider_slug(labels[0]) if labels else ""


# the substring stage runs without word boundaries, so it only ever looks at long, vendor-shaped
# words: short ones ('nim', 'grok', 'zen') and industry filler match far too much prose
TOKEN_MIN_LEN = 6
TOKEN_STOPLIST = frozenset(
    {"inference", "providers", "language", "workers", "credits", "openai", "compatible", "router"}
)


def _raw_alias_tokens(item: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for alias in (item["id"], *item.get("aliases", ())):
        for token in re.split(r"[^a-z0-9]+", str(alias).lower()):
            if len(token) >= TOKEN_MIN_LEN and token not in TOKEN_STOPLIST:
                tokens.add(token)
    return tokens


def _discriminating_tokens() -> dict[str, set[str]]:
    """Alias tokens that belong to exactly one template.

    'cloud' shows up in Groq, Cerebras, SambaNova and Alibaba Cloud; matching it would turn
    every second source string into a four-way tie. Words shared by two templates carry no
    information, so the loosest matching stage never looks at them.
    """
    owners: dict[str, int] = {}
    per_template = {item["id"]: _raw_alias_tokens(item) for item in MATCHABLE}
    for tokens in per_template.values():
        for token in tokens:
            owners[token] = owners.get(token, 0) + 1
    return {name: {token for token in tokens if owners[token] == 1} for name, tokens in per_template.items()}


ALIAS_TOKENS: dict[str, set[str]] = _discriminating_tokens()


def _alias_tokens(item: dict[str, Any]) -> set[str]:
    return ALIAS_TOKENS.get(item["id"], set())


def _match_id(text: str) -> list[str]:
    return [item["id"] for item in MATCHABLE if _phrase_in(text, item["id"])]


def _match_alias(text: str) -> list[str]:
    return [
        item["id"]
        for item in MATCHABLE
        if any(_phrase_in(text, str(alias).lower()) for alias in item.get("aliases", ()))
    ]


def _match_hostname(hosts: list[str]) -> list[str]:
    found: list[str] = []
    for item in MATCHABLE:
        for known in item.get("hostnames", ()):
            known = str(known).lower()
            if any(host == known or host.endswith(f".{known}") for host in hosts):
                found.append(item["id"])
                break
    return found


def _match_token(text: str) -> list[str]:
    return [item["id"] for item in MATCHABLE if any(token in text for token in _alias_tokens(item))]


def _settle(hits: list[str], kind: str, detail: str) -> tuple[str, float, str] | None:
    unique = sorted(set(hits))
    if not unique:
        return None
    if len(unique) > 1:
        raise SourceUnresolved(f"{detail} fits {', '.join(unique)}; say which one", ["source"], unique)
    return unique[0], CONFIDENCE[kind], f"{kind}: {detail}"


def key_prefix_provider(key: str | None) -> str | None:
    raw = (key or "").strip()
    if not raw:
        return None
    for prefix, name in KEY_PREFIXES:
        if raw.startswith(prefix) or raw.lower().startswith(prefix.lower()):
            return name
    return None


def resolve_source(
    text: str | None = None,
    promo_row: dict[str, Any] | None = None,
    key: str | None = None,
) -> tuple[str, float, str]:
    """Name the catalog template a pasted key belongs to.

    Order: the promo row (its provider name, then its url) beats the free-text source, which
    beats the shape of the key itself. Within the source text: exact template id, then an
    alias, then a hostname inside a url, then an alias token seen anywhere in the text.
    Raises SourceUnresolved (422) on a tie or on nothing at all.
    """
    named: list[str] = []

    if promo_row:
        provider_name = _norm(str(promo_row.get("provider") or ""))
        promo_hosts = urls_in(str(promo_row.get("url") or ""))
        if promo_hosts:
            promo_hosts = [host for host in promo_hosts if host not in GENERIC_HOSTS] or promo_hosts
        for hits, detail in (
            (_match_id(provider_name), f"promo provider {provider_name!r}"),
            (_match_alias(provider_name), f"promo provider {provider_name!r}"),
            (_match_hostname(promo_hosts), f"promo url {' '.join(promo_hosts)}"),
            (_match_token(provider_name), f"promo provider {provider_name!r}"),
        ):
            settled = _settle(hits, "promo", detail)
            if settled:
                return settled
        if provider_name:
            named.append(provider_name)

    source = _norm(text)
    if source:
        hosts = urls_in(source)
        for hits, kind, detail in (
            (_match_id(source), "id", "template id in the source text"),
            (_match_alias(source), "alias", "an alias in the source text"),
            (_match_hostname(hosts), "hostname", f"a url host in {' '.join(hosts)}"),
            (_match_token(source), "token", "an alias token in the source text"),
        ):
            settled = _settle(hits, kind, detail)
            if settled:
                return settled

    from_key = key_prefix_provider(key)
    if from_key:
        if template(from_key):
            return from_key, CONFIDENCE["key_prefix"], f"key_prefix: key looks like {from_key}"
        named.append(from_key)

    if named:
        raise SourceUnresolved(f"{named[0]} is not in the catalog; send base_url", ["base_url"], [named[0]])
    raise SourceUnresolved("cannot tell which provider this key is from", ["source"], [])


# Cloudflare account ids are 32 lowercase hex chars. The owner pastes them bare or inside a
# dashboard url (https://dash.cloudflare.com/<id>/ai), so one search over the whole string
# covers both. The boundaries stop it biting a longer alphanumeric run, e.g. part of a token.
ACCOUNT_ID_RE = re.compile(r"(?<![0-9a-z])([0-9a-f]{32})(?![0-9a-z])", re.IGNORECASE)


def account_id_in(text: str | None) -> str | None:
    found = ACCOUNT_ID_RE.search(text or "")
    return found.group(1).lower() if found else None


def fields_from_texts(needed: list[str], given: dict[str, str], texts: list[str | None]) -> dict[str, str]:
    """Fill the template placeholders: what the request said first, then what the text carries."""
    values = {name: str(given[name]).strip() for name in given if str(given[name]).strip()}
    for name in needed:
        if values.get(name) or name != "account_id":
            continue
        for text in texts:
            found = account_id_in(text)
            if found:
                values[name] = found
                break
    return values


def env_file_path(settings: Settings, provider: str) -> Path:
    return settings.env_dir / f"{provider}.env"


def _line_name(raw: str) -> str | None:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    name, _, _ = line.partition("=")
    return name.strip() or None


def write_env_value(path: Path, name: str, value: str) -> str:
    """Create the env file 0600 with NAME=value, or replace that one line in place.

    Returns 'created', 'replaced' or 'appended'. The value is never logged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new_line = f"{name}={value}"
    if not path.exists():
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(new_line + "\n")
        log.info("wrote %s (new file, mode 600) with %s", path, name)
        return "created"

    kept: list[str] = []
    replaced = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if _line_name(raw) == name:
            if not replaced:
                kept.append(new_line)
                replaced = True
            continue
        kept.append(raw)
    if not replaced:
        kept.append(new_line)
    path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    log.info("%s %s in %s", "replaced" if replaced else "appended", name, path)
    return "replaced" if replaced else "appended"


def remove_env_value(path: Path, name: str) -> bool:
    if not path.exists():
        return False
    kept = [raw for raw in path.read_text(encoding="utf-8").splitlines() if _line_name(raw) != name]
    text = "\n".join(kept)
    path.write_text(text.rstrip("\n") + "\n" if text.strip() else "", encoding="utf-8")
    os.chmod(path, 0o600)
    log.info("purged %s from %s", name, path)
    return True


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AccountError(f"registry {path} is not a mapping", 500)
    return data


def save_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def model_entry(spec: dict[str, Any]) -> dict[str, Any]:
    model_id = str(spec.get("id") or "").strip()
    if not model_id:
        raise AccountError("every model needs an id")
    entry: dict[str, Any] = {"id": model_id, "caps": list(spec.get("caps") or ["text"])}
    for field in ("context", "reset_tz", "concurrency", "activated_at", "notes", "max_ai_credits"):
        if spec.get(field) is not None:
            entry[field] = spec[field]
    entry["free"] = spec["free"] if "free" in spec else {}
    if spec.get("extra_body"):
        entry["extra_body"] = spec["extra_body"]
    return entry


def merge_model(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold an incoming model spec into the registered one: request wins, dicts merge.

    `context` is the one field where the owner's value always wins: a rediscovery run must
    not clobber a context window the owner set (or corrected) by hand.
    """
    for field, value in incoming.items():
        if field == "id":
            continue
        if field == "context" and current.get("context") is not None:
            continue
        if isinstance(value, dict) and isinstance(current.get(field), dict):
            merged = dict(current[field])
            merged.update(value)
            current[field] = merged
        else:
            current[field] = value
    return current


def provider_block(
    *,
    provider: str,
    kind: str | None,
    base_url: str | None,
    template_id: str | None,
    docs_url: str | None,
    fields: dict[str, str],
    models: list[dict[str, Any]] | None,
    command: str | None = None,
) -> dict[str, Any]:
    known = template(template_id or provider)
    block: dict[str, Any] = {"kind": kind or (known["kind"] if known else "openai")}
    if block["kind"] == CLI_KIND:
        # a cli provider is reached by running a program, not by calling a url
        chosen = (command or (str(known.get("command") or "") if known else "")).strip()
        if not chosen:
            raise AccountError(f"provider {provider} is new: kind cli needs a command")
        block["command"] = chosen
        for name in ("extra_args", "concurrency", "timeout_s", "workdir", "env_passthrough", "env_deny"):
            if known and known.get(name) is not None:
                block[name] = known[name]
    else:
        block["base_url"] = render_base_url(base_url or (known["base_url"] if known else ""), fields)
        if not block["base_url"]:
            raise AccountError(f"provider {provider} is new: base_url is required")
        still_missing = missing_fields(block["base_url"])
        if still_missing:
            raise AccountError(f"base_url still needs {sorted(set(still_missing))}")
    docs = docs_url or (known.get("docs_url") if known else None)
    if docs:
        block["docs_url"] = docs
    if fields:
        # kept next to the rendered base_url: discovery may need the same values later
        block["fields"] = {name: str(value).strip() for name, value in fields.items()}
    if known:
        block["template"] = known["id"]
    block["accounts"] = []
    source = models if models is not None else (known["models"] if known else [])
    block["models"] = [model_entry(item) for item in source]
    return block


class RegistryWriter:
    """Reads ~/.llmhub/providers.yaml, edits the mapping, writes it back.

    Round trip is content-preserving, not comment-preserving: every key the file already has
    survives, YAML comments do not.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.registry_path

    def read(self) -> dict[str, Any]:
        data = load_raw(self.path)
        data.setdefault("providers", {})
        if not isinstance(data["providers"], dict):
            raise AccountError("registry 'providers' must be a mapping", 500)
        return data

    def provider(self, data: dict[str, Any], provider: str) -> dict[str, Any]:
        block = data["providers"].get(provider)
        if block is None:
            raise AccountError(f"unknown provider {provider}", 404)
        block.setdefault("accounts", [])
        block.setdefault("models", [])
        return block

    def account(self, block: dict[str, Any], account_id: str) -> dict[str, Any]:
        for account in block["accounts"]:
            if str(account.get("id")) == account_id:
                return account
        raise AccountError(f"unknown account {account_id}", 404)

    def add_account(
        self,
        *,
        provider: str,
        account_id: str,
        api_key: str | None,
        api_key_env: str | None = None,
        base_url: str | None = None,
        kind: str | None = None,
        template_id: str | None = None,
        docs_url: str | None = None,
        fields: dict[str, str] | None = None,
        models: list[dict[str, Any]] | None = None,
        activated_at: str | None = None,
        command: str | None = None,
    ) -> dict[str, Any]:
        provider = check_name(provider, "provider")
        account_id = check_name(account_id, "account_id")
        data = self.read()
        created_provider = provider not in data["providers"]
        if created_provider:
            data["providers"][provider] = provider_block(
                provider=provider,
                kind=kind,
                base_url=base_url,
                template_id=template_id,
                docs_url=docs_url,
                fields=fields or {},
                models=models,
                command=command,
            )
        block = self.provider(data, provider)
        if any(str(account.get("id")) == account_id for account in block["accounts"]):
            raise AccountError(f"account {provider}/{account_id} already exists", 409)

        known = template(template_id or provider)
        env_name: str | None
        if api_key is None:
            env_name = check_env_name(api_key_env) if api_key_env else None
        elif api_key_env:
            env_name = check_env_name(api_key_env)
        elif known and known.get("api_key_env"):
            env_name = str(known["api_key_env"])
        else:
            env_name = default_env_name(provider)

        if not created_provider and models:
            existing = {str(model.get("id")) for model in block["models"]}
            for spec in models:
                if str(spec.get("id")) not in existing:
                    block["models"].append(model_entry(spec))

        account: dict[str, Any] = {"id": account_id, "api_key_env": env_name}
        if activated_at:
            account["activated_at"] = activated_at
        block["accounts"].append(account)

        written = None
        env_file = env_file_path(self.settings, provider)
        if api_key is not None and env_name:
            value = normalize_api_key(api_key)
            written = write_env_value(env_file, env_name, value)
            os.environ[env_name] = value
        save_raw(self.path, data)
        log.info(
            "registered account %s/%s (env %s, provider created=%s)",
            provider,
            account_id,
            env_name,
            created_provider,
        )
        return {
            "provider": provider,
            "account_id": account_id,
            "api_key_env": env_name,
            "env_file": str(env_file),
            "env_write": written,
            "key_present": bool(env_name is None or os.environ.get(env_name)),
            "created_provider": created_provider,
            "models": [str(model.get("id")) for model in block["models"]],
        }

    def add_models(self, *, provider: str, account_id: str, models: list[dict[str, Any]]) -> dict[str, Any]:
        """Append models to the provider (models are per provider, the account only gates it)."""
        if not models:
            raise AccountError("models list is empty")
        data = self.read()
        block = self.provider(data, provider)
        self.account(block, account_id)
        known = {str(model.get("id")): model for model in block["models"]}
        added: list[str] = []
        updated: list[str] = []
        for spec in models:
            entry = model_entry(spec)
            current = known.get(entry["id"])
            if current is None:
                block["models"].append(entry)
                known[entry["id"]] = entry
                added.append(entry["id"])
            else:
                merge_model(current, entry)
                updated.append(entry["id"])
        save_raw(self.path, data)
        log.info(
            "models on %s via %s: %s added, %s updated",
            provider,
            account_id,
            len(added),
            len(updated),
        )
        return {
            "provider": provider,
            "account_id": account_id,
            "added": added,
            "updated": updated,
            "models": [str(model.get("id")) for model in block["models"]],
        }

    def rotate_key(self, *, provider: str, account_id: str, api_key: str) -> dict[str, Any]:
        data = self.read()
        block = self.provider(data, provider)
        account = self.account(block, account_id)
        env_name = account.get("api_key_env")
        if not env_name:
            env_name = default_env_name(provider)
            account["api_key_env"] = env_name
            save_raw(self.path, data)
        env_name = check_env_name(str(env_name))
        value = normalize_api_key(api_key)
        env_file = env_file_path(self.settings, provider)
        written = write_env_value(env_file, env_name, value)
        os.environ[env_name] = value
        log.info("rotated key for %s/%s (env %s)", provider, account_id, env_name)
        return {
            "provider": provider,
            "account_id": account_id,
            "api_key_env": env_name,
            "env_file": str(env_file),
            "env_write": written,
            "key_present": True,
        }

    def delete_account(self, *, provider: str, account_id: str, purge_key: bool = False) -> dict[str, Any]:
        data = self.read()
        block = self.provider(data, provider)
        account = self.account(block, account_id)
        env_name = account.get("api_key_env")
        block["accounts"] = [item for item in block["accounts"] if str(item.get("id")) != account_id]
        save_raw(self.path, data)
        env_file = env_file_path(self.settings, provider)
        purged = False
        if purge_key and env_name:
            still_used = any(
                str(item.get("api_key_env")) == str(env_name)
                for other in data["providers"].values()
                for item in (other.get("accounts") or [])
            )
            if not still_used:
                purged = remove_env_value(env_file, str(env_name))
                os.environ.pop(str(env_name), None)
        log.info("removed account %s/%s (purge_key=%s)", provider, account_id, purge_key)
        return {
            "provider": provider,
            "account_id": account_id,
            "api_key_env": env_name,
            "env_file": str(env_file),
            "removed": True,
            "purged_key": purged,
            "accounts_left": len(block["accounts"]),
        }


# Field names vendors use for a model's context window, first match wins. Order follows how
# often each shape turns up across the catalog's discover sources.
CONTEXT_FIELD_NAMES: tuple[str, ...] = (
    "context_length",  # OpenRouter, Cohere compat, DeepInfra
    "context_window",  # groq
    "inputTokenLimit",  # gemini
    "max_context_length",  # Mistral
    "context_size",  # Novita
    "max_model_len",  # vLLM-style servers
    "max_input_tokens",
)


def _as_context_int(value: Any) -> int | None:
    """A positive int out of whatever shape the vendor sent it in; 0/None/junk -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            parsed = int(text)
            return parsed if parsed > 0 else None
    return None


def _context_from_row(row: dict[str, Any]) -> int | None:
    """The context window a discovery row carries, checked in the field order above plus the
    nested shapes: Cloudflare's `properties` list, OpenRouter's `top_provider` fallback, and a
    `limits` object some gateways nest it under."""
    for name in CONTEXT_FIELD_NAMES:
        found = _as_context_int(row.get(name))
        if found is not None:
            return found
    properties = row.get("properties")
    if isinstance(properties, list):
        for item in properties:
            if isinstance(item, dict) and item.get("property_id") == "context_window":
                found = _as_context_int(item.get("value"))
                if found is not None:
                    return found
    top_provider = row.get("top_provider")
    if isinstance(top_provider, dict):
        found = _as_context_int(top_provider.get("context_length"))
        if found is not None:
            return found
    limits = row.get("limits")
    if isinstance(limits, dict):
        for name in ("context", "input"):
            found = _as_context_int(limits.get(name))
            if found is not None:
                return found
    return None


def parse_model_rows(payload: Any, id_field: str | None = None) -> list[dict[str, Any]]:
    """Model rows out of any of the usual list shapes: `{"id": ..., "context": int | None}`.

    `id_field` pins the key to read: Cloudflare's rows carry both a uuid `id` and the `@cf/...`
    `name` the API actually accepts, so guessing would register unusable ids. Context is best
    effort - most vendor listings carry it, a few do not, and this returns None rather than
    guess when nothing matches.
    """
    rows: Any = payload
    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("models")
        if rows is None:
            rows = payload.get("result")
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            if row and row not in seen:
                seen.add(row)
                out.append({"id": row, "context": None})
        elif isinstance(row, dict):
            if id_field:
                value = row.get(id_field)
            else:
                value = row.get("id") or row.get("name") or row.get("model")
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                out.append({"id": value, "context": _context_from_row(row)})
    return out


def parse_model_ids(payload: Any, id_field: str | None = None) -> list[str]:
    """Model ids out of any of the usual list shapes. Thin wrapper over `parse_model_rows`."""
    return sorted(row["id"] for row in parse_model_rows(payload, id_field))


VISION_HINTS = ("vision", "-vl", "llava")


def caps_for_model_id(model_id: str) -> list[str]:
    """Caps for a model nobody hand-curated: the name is all discovery has to go on."""
    name = model_id.lower()
    return ["text", "vision"] if any(hint in name for hint in VISION_HINTS) else ["text"]


async def _fetch_listing(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str | None,
    headers: dict[str, str] | None,
    timeout: float,
    spec: dict[str, Any] | None,
    fields: dict[str, str] | None,
) -> tuple[Any, str | None, httpx.Response]:
    """One GET at the vendor's listing endpoint. Shared by `discover_models` and
    `discover_model_rows` so there is exactly one place that builds the request."""
    id_field: str | None = None
    if spec and spec.get("url"):
        url = render_base_url(str(spec["url"]), fields or {})
        still_missing = missing_fields(url)
        if still_missing:
            raise AccountError(f"discover url still needs {sorted(set(still_missing))}")
        id_field = str(spec.get("id_field") or "") or None
    else:
        url = f"{base_url.rstrip('/')}/models"
    request_headers = {"accept": "application/json", **(headers or {})}
    if api_key:
        # gemini's native listing wants the key on its own header, not a bearer token
        auth_header = str((spec or {}).get("auth_header") or "").strip()
        if auth_header:
            request_headers[auth_header] = api_key
        else:
            request_headers["authorization"] = f"Bearer {api_key}"
    response = await client.get(url, headers=request_headers, timeout=timeout)
    if response.status_code >= 400:
        return None, id_field, response
    try:
        payload = response.json()
    except ValueError:
        return None, id_field, response
    return payload, id_field, response


async def discover_models(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    timeout: float = DISCOVER_TIMEOUT,
    spec: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    template_id: str | None = None,
) -> tuple[list[str], httpx.Response]:
    """Ask the vendor what it serves: `GET {base_url}/models`, or the template's own endpoint.

    Workers AI has no /models under its OpenAI-compatible base_url, so its template ships a
    `discover` spec pointing at the account-scoped model search instead.

    The list comes back with the vendor's whole catalogue on it; `template_id` picks the id
    shape (gemini lists `models/<id>`) and the non-chat markers to drop.
    """
    payload, id_field, response = await _fetch_listing(
        client, base_url=base_url, api_key=api_key, headers=headers, timeout=timeout, spec=spec, fields=fields
    )
    if payload is None:
        return [], response
    return chat_model_ids(parse_model_ids(payload, id_field), template_id), response


async def discover_model_rows(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    timeout: float = DISCOVER_TIMEOUT,
    spec: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    template_id: str | None = None,
) -> tuple[list[dict[str, Any]], httpx.Response]:
    """Like `discover_models`, but keeps each row's context window next to its normalized id.

    A separate function rather than a third return value on `discover_models`: that one is
    called from two places outside this module and neither needs context, so widening its
    return shape would break them for no benefit. This is the one to call where the context
    window matters (refresh-context, and any future registration path that wants it).
    """
    payload, id_field, response = await _fetch_listing(
        client, base_url=base_url, api_key=api_key, headers=headers, timeout=timeout, spec=spec, fields=fields
    )
    if payload is None:
        return [], response
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parse_model_rows(payload, id_field):
        name = normalize_model_id(str(row["id"]), template_id)
        if name and is_chat_model_id(name, template_id) and name not in seen:
            seen.add(name)
            kept.append({"id": name, "context": row.get("context")})
    return kept, response


def model_specs_from_rows(
    rows: list[dict[str, Any]],
    spec: dict[str, Any] | None = None,
    *,
    notes: str | None = None,
) -> list[dict[str, Any]]:
    """Discovered rows -> model specs ready for `RegistryWriter.add_account`/`add_models`.

    Same shape as the id-only spec building discovery did before rows carried a context window,
    with one addition: `context` is set on the spec when the row had one, so it survives
    `model_entry`/`merge_model` into the registry. Not wired into the quick-add or
    discover-on-account HTTP handlers (api.py) yet - those build specs from bare ids today and
    would need to switch to `discover_model_rows` first to have anything to pass here.
    """
    final_notes = notes or str((spec or {}).get("notes") or "limits unknown, discovered")
    use_caps = caps_for_model_id if spec else (lambda _model_id: ["text"])
    out: list[dict[str, Any]] = []
    for row in rows:
        model_id = str(row["id"])
        entry: dict[str, Any] = {"id": model_id, "caps": use_caps(model_id), "free": {}, "notes": final_notes}
        context = row.get("context")
        if isinstance(context, int) and context > 0:
            entry["context"] = context
        out.append(entry)
    return out


# --- quick add: an endpoint for a vendor the catalog has no template for -------------------

GUESS_TIMEOUT = 5.0
MAX_GUESSES = 6

# Labels a promo link lands on. The api host is derived from the bare domain, so a blog or a
# dashboard url has to lose its first label before anything can be built from it.
HOST_PREFIXES = ("www.", "blog.", "docs.", "platform.", "console.", "dash.", "app.", "api.")

# The layouts an OpenAI-compatible vendor serves, in the order they are worth trying.
CANDIDATE_PATTERNS = (
    "https://api.{domain}/v1",
    "https://api.{domain}/openai/v1",
    "https://{domain}/v1",
    "https://{domain}/api/v1",
    "https://api.{domain}/v1beta/openai",
)

# Vendors no pattern reaches, keyed by the bare domain a url reduces to. Templates cover most
# of these already; the map is what is left when the source text named nothing the catalog knows.
KNOWN_ENDPOINTS: dict[str, str] = {
    "ai21.com": "https://api.ai21.com/studio/v1",
    "chutes.ai": "https://llm.chutes.ai/v1",
    "cohere.com": "https://api.cohere.com/compatibility/v1",
    "deepinfra.com": "https://api.deepinfra.com/v1/openai",
    "fireworks.ai": "https://api.fireworks.ai/inference/v1",
    "glhf.chat": "https://glhf.chat/api/openai/v1",
    "nebius.com": "https://api.studio.nebius.com/v1",
    "z.ai": "https://api.z.ai/api/paas/v4",
}


def bare_domain(host: str) -> str:
    """dash.cloudflare.com -> cloudflare.com. Never strips down to a bare TLD."""
    domain = (host or "").strip().lower().rstrip(".")
    stripped = True
    while stripped:
        stripped = False
        for prefix in HOST_PREFIXES:
            if domain.startswith(prefix) and domain.count(".") > 1:
                domain = domain[len(prefix) :]
                stripped = True
    return domain


def candidate_base_urls(texts: list[str | None]) -> list[str]:
    """Endpoints worth probing, built from every hostname the texts carry."""
    domains: list[str] = []
    for text in texts:
        for host in urls_in(text):
            domain = bare_domain(host)
            if not domain or "." not in domain or domain in GENERIC_HOSTS or domain in domains:
                continue
            domains.append(domain)
    urls: list[str] = []
    for domain in domains:
        known = KNOWN_ENDPOINTS.get(domain)
        shapes = ((known,) if known else ()) + tuple(
            pattern.format(domain=domain) for pattern in CANDIDATE_PATTERNS
        )
        for url in shapes:
            if url not in urls:
                urls.append(url)
    return urls[:MAX_GUESSES]


async def probe_base_url(
    client: httpx.AsyncClient, base_url: str, api_key: str | None, timeout: float = GUESS_TIMEOUT
) -> dict[str, str]:
    """One `GET {base_url}/models`. `status` reports the endpoint, never the key value."""
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    try:
        response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return {"url": base_url, "status": "no_response"}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code == 200:
        return {"url": base_url, "status": "ok" if parse_model_ids(payload) else "no_models"}
    if response.status_code in (401, 403) and isinstance(payload, dict):
        # the endpoint is real and only turned the key down: the best thing to prefill
        return {"url": base_url, "status": "exists_key_rejected"}
    if response.status_code == 404:
        return {"url": base_url, "status": "not_found"}
    return {"url": base_url, "status": f"http_{response.status_code}"}


async def guess_base_url(
    client: httpx.AsyncClient,
    *,
    texts: list[str | None],
    api_key: str | None,
    timeout: float = GUESS_TIMEOUT,
) -> tuple[str | None, list[dict[str, str]]]:
    """Ask the candidate endpoints which one is real, all at once.

    Returns the first candidate that served a model list, plus every candidate tried with what
    it answered. 'exists_key_rejected' sorts first in that list: the endpoint is real and only
    turned the key down, so it is the one worth prefilling when nothing outright won.
    """
    candidates = candidate_base_urls(texts)
    if not candidates:
        return None, []
    results = list(
        await asyncio.gather(*(probe_base_url(client, url, api_key, timeout) for url in candidates))
    )
    winner = next((row["url"] for row in results if row["status"] == "ok"), None)
    return winner, sorted(results, key=lambda row: 0 if row["status"] == "exists_key_rejected" else 1)


# --- refresh-context: fill `model.context` from a provider's own listing --------------------


def _template_id_of(block: Any, provider_name: str) -> str:
    """Catalog template a registered provider block came from, the provider name when it says
    nothing - same rule as `Entry.template_id` in config.py, restated here because this walks
    `Registry.providers` directly instead of going through an `Entry`."""
    value = (block.model_extra or {}).get("template")
    return str(value) if value else provider_name


def _discover_fields_of(block: Any, spec: dict[str, Any] | None) -> dict[str, str]:
    """Placeholder values the provider's discover url needs, stored on the block at quick-add
    time or, failing that, read back out of base_url (Cloudflare's account id is the only
    template field seen in practice)."""
    stored = (block.model_extra or {}).get("fields")
    fields = {str(name): str(value) for name, value in stored.items()} if isinstance(stored, dict) else {}
    if spec and not fields.get("account_id"):
        found = account_id_in(block.base_url)
        if found:
            fields["account_id"] = found
    return fields


async def refresh_context_report(
    client: httpx.AsyncClient,
    registry: Registry,
    *,
    provider_filter: str | None = None,
) -> list[dict[str, Any]]:
    """One discovery pass per eligible provider, matched against its registered models.

    Eligible = not a `cli` provider (nothing to list over HTTP) and at least one account whose
    key is present, or that needs none. Read-only: this only says what would change, so a dry
    run and a real run go through the exact same matching logic - only the caller decides
    whether to write the result.
    """
    reports: list[dict[str, Any]] = []
    for name, block in registry.providers.items():
        if provider_filter and name != provider_filter:
            continue
        if block.kind == CLI_KIND:
            continue
        report: dict[str, Any] = {
            "provider": name,
            "models": len(block.models),
            "filled": [],
            "from_catalog": [],
            "already_set": 0,
            "unknown": [],
            "status": "ok",
        }
        account = next(
            (item for item in block.accounts if not item.api_key_env or os.environ.get(item.api_key_env)),
            None,
        )
        if account is None:
            report["status"] = "no_account_key"
            reports.append(report)
            continue
        template_id = _template_id_of(block, name)
        spec = discover_spec(template_id)
        fields = _discover_fields_of(block, spec)
        api_key = os.environ.get(account.api_key_env) if account.api_key_env else None
        try:
            rows, response = await discover_model_rows(
                client,
                base_url=block.base_url,
                api_key=api_key,
                headers=dict(block.headers),
                spec=spec,
                fields=fields,
                template_id=template_id,
            )
        except (httpx.HTTPError, AccountError) as exc:
            report["status"] = f"error: {exc}"
            reports.append(report)
            continue
        if response.status_code >= 400:
            report["status"] = f"http_{response.status_code}"
            reports.append(report)
            continue
        if not rows:
            report["status"] = "no_models"
            reports.append(report)
            continue
        context_by_id = {row["id"]: row["context"] for row in rows if row.get("context")}
        still_missing = {
            model.id for model in block.models if model.context is None and model.id not in context_by_id
        }
        context_spec = context_discover_spec(template_id)
        if still_missing and context_spec:
            context_fields = _discover_fields_of(block, context_spec)
            try:
                context_rows, context_response = await discover_model_rows(
                    client,
                    base_url=block.base_url,
                    api_key=api_key,
                    headers=dict(block.headers),
                    spec=context_spec,
                    fields=context_fields,
                    template_id=template_id,
                )
            except (httpx.HTTPError, AccountError):
                context_rows, context_response = [], None
            if context_response is not None and context_response.status_code < 400:
                for row in context_rows:
                    if row.get("context") and row["id"] not in context_by_id:
                        context_by_id[row["id"]] = row["context"]
        catalog_context_by_id = catalog_context(template_id)
        for model in block.models:
            if model.context is not None:
                report["already_set"] += 1
                continue
            found = context_by_id.get(model.id)
            if found:
                report["filled"].append((model.id, found))
                continue
            from_catalog = catalog_context_by_id.get(model.id)
            if from_catalog:
                report["filled"].append((model.id, from_catalog))
                report["from_catalog"].append(model.id)
            else:
                report["unknown"].append(model.id)
        reports.append(report)
    return reports


def backup_registry_file(path: Path, directory: Path) -> Path:
    """Copy providers.yaml aside before refresh-context rewrites it.

    Same shape as `promo_identity.backup_promos`: a timestamped file under `directory`, written
    before anything else touches the registry.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = directory / f"providers-{stamp}.yaml"
    backup_path.write_text(path.read_text(encoding="utf-8") if path.exists() else "", encoding="utf-8")
    return backup_path


def apply_context_fills(path: Path, reports: list[dict[str, Any]]) -> int:
    """Write the `filled` entries from `refresh_context_report` into providers.yaml.

    Skips a model that already carries a context - the report only ever lists a model under
    `filled` when it had none, but the owner may have added one by hand between the report and
    the write, and that value must win. Returns how many fields were actually written.
    """
    data = load_raw(path)
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return 0
    written = 0
    for report in reports:
        if not report["filled"]:
            continue
        block = providers.get(report["provider"])
        if not isinstance(block, dict):
            continue
        by_id = {str(model.get("id")): model for model in block.get("models", []) if isinstance(model, dict)}
        for model_id, context in report["filled"]:
            entry = by_id.get(model_id)
            if isinstance(entry, dict) and entry.get("context") is None:
                entry["context"] = context
                written += 1
    if written:
        save_raw(path, data)
    return written
