from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .accounts import (
    AccountError,
    RegistryWriter,
    SourceUnresolved,
    account_id_in,
    default_account_id,
    discover_model_rows,
    env_file_path,
    fields_from_texts,
    guess_base_url,
    model_specs_from_rows,
    provider_name_from_url,
    provider_slug,
    resolve_source,
)
from .auth import is_loopback, require_token
from .cli_backend import PROBE_PROMPT, CliRequestError, discover_cli_models
from .config import CLI_KIND
from .promo_identity import (
    duplicate_groups,
    hosts_of,
    identify,
    name_alias,
    provider_for_base_url,
    registry_account,
)
from .providers_catalog import discover_spec, known_providers, template
from .router import AllCandidatesFailed, Router, UpstreamError
from .runtime import Hub
from .scout.runner import ScoutBusy, ScoutService, next_run_at, run_row
from .status import (
    LIVE_WINDOW_MIN,
    account_rows,
    alias_rows,
    app_shares,
    live_rows,
    model_rows,
    queue_summary,
)
from .store import now_iso, parse_iso, to_iso

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

TEST_APP = "hub-test"
LAN_WARNING = "plain HTTP from LAN; add keys from the Mac when possible"


def lan_warning(request: Request) -> dict[str, str]:
    return {} if is_loopback(request) else {"warning": LAN_WARNING}


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


def normalize_since(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return to_iso(parse_iso(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad since value: {value}") from exc


PROMO_STATUSES = ("new", "known", "used", "expired", "rejected")
REJECT_REASON_MIN = 8
REJECT_REASON_REQUIRED = (
    "a rejected promo needs rejected_reason (at least "
    f"{REJECT_REASON_MIN} characters): the reason is handed to the scout and the promo-hunt "
    "skill as a standing rule, so the same and similar offers are not proposed again"
)


def checked_reason(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) < REJECT_REASON_MIN:
        raise ValueError(REJECT_REASON_REQUIRED)
    return text


class PromoIn(BaseModel):
    provider: str
    url: str | None = None
    note: str | None = None
    status: str = "new"
    source: str | None = None
    expires_at: str | None = None
    account_key: str | None = None
    # a promo for a vendor off the catalog can carry its own endpoint; quick add uses it
    base_url: str | None = None
    api_key_env: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in PROMO_STATUSES:
            raise ValueError(f"status must be one of {list(PROMO_STATUSES)}")
        return value


class PromoPatch(BaseModel):
    status: str | None = None
    note: str | None = None
    account_key: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    rejected_reason: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in PROMO_STATUSES:
            raise ValueError(f"status must be one of {list(PROMO_STATUSES)}")
        return value

    @model_validator(mode="after")
    def _reason_with_rejection(self) -> PromoPatch:
        if self.status == "rejected":
            self.rejected_reason = checked_reason(self.rejected_reason)
        return self


class RejectIn(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _long_enough(cls, value: str) -> str:
        return checked_reason(value)


class DisableIn(BaseModel):
    disabled: bool = True
    note: str | None = None


BAN_REASON_MIN = 8
BAN_REASON_REQUIRED = (
    f"a ban needs a reason (at least {BAN_REASON_MIN} characters): the hub cannot measure why "
    "a model was unusable for this app, so the reason is the only record of it - what the "
    "model did wrong, not 'bad'"
)


def checked_ban_reason(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) < BAN_REASON_MIN:
        raise ValueError(BAN_REASON_REQUIRED)
    return text


class BanIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _long_enough(cls, value: str) -> str:
        return checked_ban_reason(value)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    hub = hub_of(request)
    now = datetime.now(UTC)
    return {
        "generated_at": to_iso(now),
        "models": model_rows(hub, now),
        "queue": queue_summary(hub),
        "aliases": alias_rows(hub),
        "accounts": account_rows(hub),
    }


@router.get("/live")
async def live(
    request: Request,
    window_min: int = Query(default=LIVE_WINDOW_MIN, ge=1, le=1440),
) -> dict[str, Any]:
    """Which app is on which model right now - one memory read and one windowed query."""
    hub = hub_of(request)
    now = datetime.now(UTC)
    rows = live_rows(hub, window_min, now)
    return {
        "generated_at": to_iso(now),
        "window_min": window_min,
        "apps": rows,
        "in_flight_total": sum(len(row["in_flight"]) for row in rows),
    }


@router.get("/usage")
async def usage(
    request: Request,
    since: str | None = None,
    group_by: Literal["app", "model", "account", "day", "provider"] = "app",
    app: str | None = None,
) -> dict[str, Any]:
    hub = hub_of(request)
    since_iso = normalize_since(since)
    rows = hub.store.usage_rows(since=since_iso, group_by=group_by, app=app)
    return {"since": since_iso, "group_by": group_by, "app": app, "rows": rows}


@router.get("/usage/timeseries")
async def usage_timeseries(
    request: Request,
    since: str | None = None,
    bucket: Literal["hour", "day"] = "hour",
    app: str | None = None,
) -> dict[str, Any]:
    hub = hub_of(request)
    since_iso = normalize_since(since)
    rows = hub.store.usage_timeseries(since=since_iso, bucket=bucket, app=app)
    return {"since": since_iso, "bucket": bucket, "app": app, "rows": rows}


@router.get("/events")
async def events(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    return {"events": hub_of(request).store.events(limit)}


@router.get("/jobs")
async def jobs(request: Request, state: str | None = None, app: str | None = None) -> dict[str, Any]:
    hub = hub_of(request)
    return {"jobs": hub.store.jobs(state=state, app=app), "queue": queue_summary(hub)}


@router.get("/apps")
async def apps(request: Request) -> dict[str, Any]:
    hub = hub_of(request)
    shares = app_shares(hub)
    rows = hub.store.apps()
    known = {row["app"] for row in rows}
    # an app that only ever queued jobs has no usage rows yet, but it does hold a share
    rows.extend(
        {"app": name, "requests": 0, "paused": share["paused"]}
        for name, share in shares.items()
        if name not in known
    )
    bans = hub.store.bans_all()
    for row in rows:
        share = shares.get(row["app"], {})
        row["queued"] = share.get("queued", 0)
        row["running"] = share.get("running", 0)
        row["cap"] = share.get("cap")
        # the count only; the rows themselves come from api/apps/{app}/bans when asked for
        row["bans"] = len(bans.get(row["app"], []))
    return {"apps": sorted(rows, key=lambda row: row["app"]), "workers": getattr(hub.jobs, "workers", None)}


@router.get("/promos")
async def promos(request: Request, duplicates: int = 0) -> dict[str, Any]:
    rows = hub_of(request).store.promos()
    body: dict[str, Any] = {"promos": rows}
    if duplicates:
        # debug aid: what identity resolution has folded together so far
        body["duplicates"] = duplicate_groups(rows)
    return body


def provider_extra(provider: Any, field: str) -> Any:
    return (provider.model_extra or {}).get(field)


@router.get("/registry")
async def registry(request: Request) -> dict[str, Any]:
    hub = hub_of(request)
    providers: dict[str, Any] = {}
    for name, provider in hub.registry.providers.items():
        env_file = str(env_file_path(hub.settings, name))
        providers[name] = {
            "kind": provider.kind,
            "base_url": provider.base_url,
            "command": provider.command,
            "docs_url": provider_extra(provider, "docs_url"),
            "template": provider_extra(provider, "template"),
            "env_file": env_file,
            "accounts": [
                {
                    "id": account.id,
                    "api_key_env": account.api_key_env,
                    "env_file": env_file,
                    "key_present": (not account.api_key_env) or bool(os.environ.get(account.api_key_env)),
                }
                for account in provider.accounts
            ],
            "models": [
                {
                    "id": model.id,
                    "caps": list(model.caps),
                    "context": model.context,
                    "free": model.is_free,
                    "reset_tz": model.reset_tz,
                    "concurrency": model.concurrency,
                    "windows": {window: spec.limits() for window, spec in model.windows.items()},
                    "notes": model.notes,
                }
                for model in provider.models
            ],
        }
    return {
        "registry_path": str(hub.settings.registry_path),
        "providers": providers,
        "aliases": alias_rows(hub),
    }


@router.post("/models/{key:path}/forgive")
async def forgive_model(request: Request, key: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.registry.entry(key) is None:
        raise HTTPException(status_code=404, detail=f"unknown model {key}")
    # every marker that keeps this model out of the pool, the parked-route rows included: the
    # owner clicking this says the model works again, whatever the hub last measured
    cleared = hub.quota.forgive(key) + hub.router.clear_unavailable(key)
    hub.router.clear_cooldown(key)
    hub.store.add_event(kind="forgive", message=f"forgive {key} ({cleared} markers)", model=key)
    return {"key": key, "cleared": cleared, "ts": now_iso()}


# forgive lifts the exhaustion marker but keeps what the vendor was measured at; this drops
# the measurement, for when the free tier itself changed
@router.delete("/models/{key:path}/observed")
async def drop_observed_limits(request: Request, key: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.registry.entry(key) is None:
        raise HTTPException(status_code=404, detail=f"unknown model {key}")
    cleared = hub.quota.forget_observed(key)
    hub.store.add_event(kind="observed_limit", message=f"dropped observed limits ({cleared})", model=key)
    return {"key": key, "cleared": cleared, "ts": now_iso()}


@router.post("/models/{key:path}/disable")
async def disable_model(request: Request, key: str, payload: DisableIn | None = None) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.registry.entry(key) is None:
        raise HTTPException(status_code=404, detail=f"unknown model {key}")
    disabled = True if payload is None else payload.disabled
    hub.store.set_model_disabled(key, disabled, payload.note if payload else None)
    hub.store.add_event(kind="override", message=f"{key} disabled={disabled}", model=key)
    return {"key": key, "disabled": disabled}


@router.post("/models/{key:path}/enable")
async def enable_model(request: Request, key: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.registry.entry(key) is None:
        raise HTTPException(status_code=404, detail=f"unknown model {key}")
    hub.store.set_model_disabled(key, False)
    hub.store.add_event(kind="override", message=f"{key} disabled=False", model=key)
    return {"key": key, "disabled": False}


@router.post("/apps/{app}/pause")
async def pause_app(request: Request, app: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    hub.store.set_app_paused(app, True)
    hub.store.add_event(kind="app", message=f"{app} paused", app=app)
    return {"app": app, "paused": True}


@router.post("/apps/{app}/resume")
async def resume_app(request: Request, app: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    hub.store.set_app_paused(app, False)
    hub.store.add_event(kind="app", message=f"{app} resumed", app=app)
    return {"app": app, "paused": False}


@router.get("/apps/{app}/bans")
async def app_bans(request: Request, app: str) -> dict[str, Any]:
    """Models this app has judged unusable, with the reason it gave."""
    return {"app": app, "bans": hub_of(request).store.bans_for(app)}


@router.post("/apps/{app}/bans", status_code=201)
async def ban_app_model(request: Request, app: str, payload: BanIn) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.registry.entry(payload.model) is None:
        raise HTTPException(status_code=404, detail=f"unknown model {payload.model}")
    row = hub.store.ban_model(app, payload.model, payload.reason)
    hub.store.add_event(
        kind="ban",
        message=f"{app} banned {payload.model}: {payload.reason}",
        app=app,
        model=payload.model,
    )
    return row


@router.delete("/apps/{app}/bans/{model:path}")
async def unban_app_model(request: Request, app: str, model: str) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if not hub.store.unban_model(app, model):
        raise HTTPException(status_code=404, detail=f"{app} has no ban on {model}")
    hub.store.add_event(kind="ban", message=f"{app} unbanned {model}", app=app, model=model)
    return {"app": app, "model": model, "banned": False}


@router.post("/promos")
async def add_promo(request: Request, payload: PromoIn) -> JSONResponse:
    """One row per vendor, whoever posts it.

    The provider is resolved to an identity first, so a second report of a known offer lands
    on the row that already carries it (200, `created: false`) instead of stacking a near
    duplicate under a slightly different name. A brand new vendor answers 201.
    """
    require_token(request)
    hub = hub_of(request)
    identity = identify(payload.provider, payload.url, payload.base_url, hub.store, hub.registry)

    status = payload.status
    account_key = payload.account_key
    registered = registry_account(hub.registry, identity.key)
    if registered and not account_key:
        # the key is already in the registry, so this is not a lead the owner still has to act on
        account_key = registered
        status = "used"

    row, created = hub.store.upsert_promo(
        identity=identity.key,
        provider=payload.provider,
        url=payload.url,
        note=payload.note,
        status=status,
        source=payload.source,
        expires_at=payload.expires_at,
        base_url=payload.base_url,
        api_key_env=payload.api_key_env,
        account_key=account_key,
    )
    hub.store.add_event(
        kind="promo",
        message=(
            f"new promo {payload.provider} as {identity.key} ({identity.kind})"
            if created
            else f"merged {payload.provider} into #{row['id']} as {identity.key} ({identity.kind})"
        ),
    )
    body = {**row, "created": created, "merged_into": None if created else int(row["id"])}
    return JSONResponse(status_code=201 if created else 200, content=body)


@router.get("/promos/rejections")
async def promo_rejections(request: Request, limit: int = 200) -> dict[str, Any]:
    """The reasons the owner threw leads out, newest first - what every writer must read
    before proposing an offer."""
    return {"rejections": hub_of(request).store.promo_rejections(limit=limit)}


def reject_row(hub: Hub, promo_id: int, reason: str) -> dict[str, Any]:
    row = hub.store.reject_promo(promo_id, reason)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown promo {promo_id}")
    hub.store.add_event(kind="promo", message=f"rejected {row.get('provider')}: {reason}")
    return row


@router.patch("/promos/{promo_id}")
async def patch_promo(request: Request, promo_id: int, payload: PromoPatch) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.store.promo(promo_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown promo {promo_id}")
    if payload.status == "rejected":
        # the reason is mandatory on this path too, so a client that patches instead of
        # calling /reject cannot land a rejection the scout will never learn from
        return reject_row(hub, promo_id, str(payload.rejected_reason))
    updated = hub.store.update_promo(
        promo_id,
        status=payload.status,
        note=payload.note,
        account_key=payload.account_key,
        base_url=payload.base_url,
        api_key_env=payload.api_key_env,
    )
    return updated or {}


@router.post("/promos/{promo_id}/reject")
async def reject_promo(request: Request, promo_id: int, payload: RejectIn) -> dict[str, Any]:
    require_token(request)
    return reject_row(hub_of(request), promo_id, payload.reason)


@router.post("/promos/{promo_id}/reopen")
async def reopen_promo(request: Request, promo_id: int) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    row = hub.store.reopen_promo(promo_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown promo {promo_id}")
    hub.store.add_event(kind="promo", message=f"reopened {row.get('provider')}")
    return row


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str
    caps: list[str] = Field(default_factory=lambda: ["text"])
    free: dict[str, Any] | None = Field(default_factory=dict)
    extra_body: dict[str, Any] | None = None
    context: int | None = None
    reset_tz: str | None = None
    concurrency: int | None = None
    activated_at: str | None = None
    notes: str | None = None

    def as_yaml(self) -> dict[str, Any]:
        spec: dict[str, Any] = {"id": self.id, "caps": list(self.caps), "free": self.free}
        for field in ("extra_body", "context", "reset_tz", "concurrency", "activated_at", "notes"):
            value = getattr(self, field)
            if value is not None:
                spec[field] = value
        return spec


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account_id: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    command: str | None = None
    kind: str | None = None
    template: str | None = None
    docs_url: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    models: list[ModelIn] | None = None
    activated_at: str | None = None


class QuickAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # optional: a `kind: cli` provider is logged in inside the CLI, there is nothing to paste
    api_key: str | None = None
    source: str | None = None
    promo_id: int | None = None
    base_url: str | None = None
    account_id: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


class ModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelIn] = Field(min_length=1)


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str


class AccountTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = None
    allow_paid: bool = False
    prompt: str = "ping"


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str | None = None


def account_error(exc: AccountError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def registry_provider_row(hub: Hub, provider: str) -> dict[str, Any]:
    block = hub.registry.providers.get(provider)
    if block is None:
        return {}
    env_file = str(env_file_path(hub.settings, provider))
    return {
        "kind": block.kind,
        "base_url": block.base_url,
        "command": block.command,
        "docs_url": provider_extra(block, "docs_url"),
        "template": provider_extra(block, "template"),
        "env_file": env_file,
        "accounts": [
            {
                "id": account.id,
                "api_key_env": account.api_key_env,
                "env_file": env_file,
                "key_present": (not account.api_key_env) or bool(os.environ.get(account.api_key_env)),
            }
            for account in block.accounts
        ],
        "models": [
            {"id": model.id, "caps": list(model.caps), "free": model.is_free} for model in block.models
        ],
    }


def reload_counts(hub: Hub) -> dict[str, Any]:
    hub.reload()
    entries = list(hub.registry.entries())
    return {
        "registry_path": str(hub.settings.registry_path),
        "providers": len(hub.registry.providers),
        "accounts": sum(len(item.accounts) for item in hub.registry.providers.values()),
        "models": len({entry.key for entry in entries}),
        "pairs": len(entries),
    }


@router.get("/providers/known")
async def providers_known(request: Request) -> dict[str, Any]:
    return {"providers": known_providers()}


@router.post("/accounts")
async def add_account(request: Request, payload: AccountIn) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    writer = RegistryWriter(hub.settings)
    try:
        result = writer.add_account(
            provider=payload.provider,
            account_id=payload.account_id,
            api_key=payload.api_key,
            api_key_env=payload.api_key_env,
            base_url=payload.base_url,
            command=payload.command,
            kind=payload.kind,
            template_id=payload.template,
            docs_url=payload.docs_url,
            fields=payload.fields,
            models=[item.as_yaml() for item in payload.models] if payload.models else None,
            activated_at=payload.activated_at,
        )
    except AccountError as exc:
        if exc.status_code == 409 and payload.models:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": str(exc),
                    "hint": f"use POST api/accounts/{payload.provider}/{payload.account_id}/models",
                },
            )
        raise account_error(exc) from exc
    reloaded = reload_counts(hub)
    hub.store.add_event(
        kind="account",
        message=f"account added {result['provider']}/{result['account_id']} (env {result['api_key_env']})",
        account=result["account_id"],
    )
    return {
        **result,
        "reloaded": reloaded,
        "registry": registry_provider_row(hub, result["provider"]),
        **lan_warning(request),
    }


def free_entry_for(hub: Hub, provider: str, account_id: str) -> Any:
    block = hub.registry.providers.get(provider)
    for model in block.models if block else []:
        if not model.is_free:
            continue
        entry = hub.registry.entry(f"{provider}/{model.id}", account_id)
        if entry is not None and entry.key_present:
            return entry
    return None


def discover_plan(block: Any) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The template's discovery endpoint plus the placeholder values it needs, if any.

    The values were stored on the provider when the account was added; a hand-written block
    keeps them only inside base_url, so fall back to reading the account id back out of it.
    """
    spec = discover_spec(provider_extra(block, "template"))
    if spec is None:
        return None, {}
    stored = provider_extra(block, "fields")
    fields = {str(name): str(value) for name, value in stored.items()} if isinstance(stored, dict) else {}
    if not fields.get("account_id"):
        found = account_id_in(block.base_url)
        if found:
            fields["account_id"] = found
    return spec, fields


async def discover_and_register(
    hub: Hub, writer: RegistryWriter, provider: str, account_id: str
) -> list[str]:
    """Ask the vendor what it serves and register the lot as free with unknown limits.

    Only reached when the template shipped no model list. A failure here is not fatal: the
    account is already written, the owner can tick models by hand afterwards.
    """
    block = hub.registry.providers.get(provider)
    if block is None:
        return []
    account = next((item for item in block.accounts if item.id == account_id), None)
    if account is None:
        return []
    api_key = os.environ.get(account.api_key_env) if account.api_key_env else None
    spec, fields = discover_plan(block)
    try:
        rows, response = await discover_model_rows(
            hub.client,
            base_url=block.base_url,
            api_key=api_key,
            headers=dict(block.headers),
            spec=spec,
            fields=fields,
            template_id=provider_extra(block, "template"),
        )
    except (httpx.HTTPError, AccountError) as exc:
        hub.store.add_event(kind="discover", message=f"{provider} discover failed: {exc}")
        return []
    rows = sorted(rows, key=lambda row: str(row["id"]))
    if response.status_code >= 400 or not rows:
        hub.store.add_event(
            kind="discover",
            message=f"{provider} discover returned {response.status_code}, {len(rows)} models",
        )
        return []
    writer.add_models(provider=provider, account_id=account_id, models=model_specs_from_rows(rows, spec))
    return [str(row["id"]) for row in rows]


def quick_add_texts(payload: QuickAddIn, promo_row: dict[str, Any] | None) -> list[str | None]:
    """Everything the request carries that might hold the vendor's url."""
    texts: list[str | None] = [payload.source]
    if promo_row:
        texts += [str(promo_row.get(field) or "") or None for field in ("url", "note", "source")]
    return texts


def learn_quick_add_alias(
    hub: Hub, base_url: str | None, promo_row: dict[str, Any] | None, provider: str
) -> None:
    """Tie the name and the endpoint the owner just used to the provider they landed on.

    Quick add is the one moment the hub knows for certain who a vendor is - the owner named
    them and the key worked - so it is worth remembering for the next promo row that spells
    the vendor differently.
    """
    aliases = [f"host:{host}" for host in hosts_of(base_url)]
    if promo_row:
        alias = name_alias(str(promo_row.get("provider") or ""))
        if alias:
            aliases.append(alias)
        aliases += [f"host:{host}" for host in hosts_of(str(promo_row.get("url") or ""))]
    for alias in aliases:
        hub.store.learn_promo_alias(alias, provider, "quick_add")


def unresolved_response(exc: SourceUnresolved, guesses: list[dict[str, str]]) -> JSONResponse:
    """422 the form can act on: what is missing, and which endpoints were tried for it."""
    detail = str(exc)
    needs = exc.needs
    if guesses:
        # candidates were built and none served a model list, so the endpoint is what is
        # missing now, whatever the resolver was originally short of
        needs = ["base_url"]
        detail = f"{detail}; guessed {len(guesses)} endpoints from the url, none served a model list"
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "needs": needs, "guess": exc.guess, "guesses": guesses},
    )


@router.post("/accounts/quick")
async def quick_add_account(request: Request, payload: QuickAddIn) -> Any:
    """Key plus a hint of where it came from; the hub works out the rest.

    Resolution -> create (or rotate, when the account is already there) -> discover if the
    template knows no models -> one test call. The test is reported, never fatal: a key that
    lands in the registry stays there even when the vendor answers the probe with a 429.

    No template for the vendor leaves three ways to an endpoint, in this order: a base_url on
    the request, a base_url carried by the promo row, and probing the shapes built from the
    hostnames in the text. Only when all three come up empty is it a 422.
    """
    require_token(request)
    hub = hub_of(request)
    writer = RegistryWriter(hub.settings)

    promo_row: dict[str, Any] | None = None
    if payload.promo_id is not None:
        promo_row = hub.store.promo(payload.promo_id)
        if promo_row is None:
            raise HTTPException(status_code=404, detail=f"unknown promo {payload.promo_id}")

    template_id: str | None = None
    provider = ""
    reason = "base_url given"
    confidence = 0.0
    base_url = payload.base_url
    api_key_env: str | None = None
    provider_reused = False
    try:
        template_id, confidence, reason = resolve_source(payload.source, promo_row, payload.api_key)
    except SourceUnresolved as exc:
        guesses: list[dict[str, str]] = []
        if not base_url and promo_row and str(promo_row.get("base_url") or "").strip():
            base_url = str(promo_row["base_url"]).strip()
            api_key_env = str(promo_row.get("api_key_env") or "").strip() or None
            reason = f"promo {payload.promo_id} carries base_url"
        if not base_url and len(exc.guess) < 2:
            # a tie between two templates is a naming question, not a missing endpoint:
            # probing would register a custom provider next to a catalog one
            winner, guesses = await guess_base_url(
                hub.client, texts=quick_add_texts(payload, promo_row), api_key=payload.api_key
            )
            if winner:
                base_url = winner
                reason = f"guessed base_url {winner}"
        if not base_url:
            return unresolved_response(exc, guesses)
        # base_url turns "who is this from" into a plain custom provider: name it after the
        # guess the resolver did have, else after the host it points at
        provider = provider_slug(exc.guess[0]) if exc.guess else provider_name_from_url(base_url)
        if not provider:
            return unresolved_response(exc, guesses)
        already = provider_for_base_url(hub.registry, base_url)
        if already and already != provider:
            # this endpoint is already registered under another name: a second provider block
            # for it would split the key, the models and the quota history in two
            provider = already
            provider_reused = True
            reason = f"{reason}; endpoint already registered as {already}"
        learn_quick_add_alias(hub, base_url, promo_row, provider)
    known = template(template_id) if template_id else None
    fields: dict[str, str] = {}
    if known:
        provider = str(known["id"])
        needed = [str(name) for name in known.get("fields") or ()]
        if needed and not base_url:
            # cloudflare-workers-ai and friends carry a {placeholder} in base_url. The owner
            # pastes the dashboard url or the bare id into the source, or names it in `fields`;
            # a base_url filled in by hand still works as the way out.
            promo_text = (
                " ".join(str(promo_row.get(field) or "") for field in ("url", "note")) if promo_row else None
            )
            fields = fields_from_texts(needed, payload.fields, [payload.source, promo_text])
            missing = [name for name in needed if not fields.get(name)]
            if missing:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": f"{provider} needs {', '.join(missing)} for its base_url; "
                        f"paste it in source, send it in fields, or send base_url",
                        "needs": missing,
                        "guess": [provider],
                    },
                )
    if not provider:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "no provider name to register under",
                "needs": ["source"],
                "guess": [],
            },
        )
    account_id = payload.account_id or default_account_id(provider)

    block = hub.registry.providers.get(provider)
    # a cli provider carries its login inside the CLI: no key on the request, no env file, and
    # nothing to rotate when the account is already there
    is_cli = (block.kind if block else str((known or {}).get("kind") or "")) == CLI_KIND
    if not is_cli and not payload.api_key:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"{provider} needs an api_key",
                "needs": ["api_key"],
                "guess": [provider],
            },
        )
    exists = bool(block and any(item.id == account_id for item in block.accounts))
    rotated = exists and not is_cli
    try:
        if rotated:
            writer.rotate_key(provider=provider, account_id=account_id, api_key=payload.api_key)
            created_provider = False
        elif exists:
            created_provider = False
        else:
            created = writer.add_account(
                provider=provider,
                account_id=account_id,
                api_key=None if is_cli else payload.api_key,
                api_key_env=api_key_env,
                base_url=base_url,
                template_id=template_id,
                fields=fields,
            )
            created_provider = bool(created["created_provider"])
    except AccountError as exc:
        raise account_error(exc) from exc
    reload_counts(hub)

    block = hub.registry.providers.get(provider)
    model_ids = [model.id for model in block.models] if block else []
    discovered = False
    if not model_ids and not is_cli:
        found = await discover_and_register(hub, writer, provider, account_id)
        if found:
            reload_counts(hub)
            block = hub.registry.providers.get(provider)
            model_ids = [model.id for model in block.models] if block else []
            discovered = True

    entry = free_entry_for(hub, provider, account_id)
    if entry is None:
        test: dict[str, Any] = {
            "ok": False,
            "status": "no_free_model",
            "model": None,
            "latency_ms": 0,
        }
    else:
        try:
            probe = await probe_entry(hub, entry)
        except Exception as exc:  # a broken probe must not undo a written key
            log.warning("quick add test call for %s/%s failed: %s", provider, account_id, exc)
            probe = {"ok": False, "status": "error", "latency_ms": 0, "error": str(exc)}
        test = {
            "ok": bool(probe.get("ok")),
            "status": probe.get("status"),
            "model": entry.key,
            "latency_ms": probe.get("latency_ms") or 0,
        }
        if probe.get("error") is not None:
            test["error"] = probe["error"]

    account_key = f"{provider}/{account_id}"
    if payload.promo_id is not None:
        hub.store.update_promo(payload.promo_id, status="used", account_key=account_key)
    hub.store.add_event(
        kind="account",
        message=f"quick add {account_key} ({reason}, confidence {confidence}), "
        f"{len(model_ids)} models, test {test['status']}",
        account=account_id,
    )

    body: dict[str, Any] = {
        "provider": provider,
        "account_id": account_id,
        "created_provider": created_provider,
        "models": model_ids,
        "discovered": discovered,
        "test": test,
    }
    if rotated:
        body["rotated"] = True
    if provider_reused:
        body["provider_reused"] = True
    if payload.promo_id is not None:
        body["promo_id"] = payload.promo_id
    return {**body, **lan_warning(request)}


@router.post("/accounts/{provider}/{account_id}/models")
async def add_account_models(
    request: Request, provider: str, account_id: str, payload: ModelsIn
) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    writer = RegistryWriter(hub.settings)
    try:
        result = writer.add_models(
            provider=provider,
            account_id=account_id,
            models=[item.as_yaml() for item in payload.models],
        )
    except AccountError as exc:
        raise account_error(exc) from exc
    reloaded = reload_counts(hub)
    hub.store.add_event(
        kind="account",
        message=f"models on {provider}/{account_id}: "
        f"{len(result['added'])} added, {len(result['updated'])} updated",
        account=account_id,
    )
    return {**result, "reloaded": reloaded, **lan_warning(request)}


@router.put("/accounts/{provider}/{account_id}/key")
async def rotate_account_key(
    request: Request, provider: str, account_id: str, payload: KeyIn
) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    writer = RegistryWriter(hub.settings)
    try:
        result = writer.rotate_key(provider=provider, account_id=account_id, api_key=payload.api_key)
    except AccountError as exc:
        raise account_error(exc) from exc
    reloaded = reload_counts(hub)
    hub.store.add_event(
        kind="account",
        message=f"key rotated {provider}/{account_id} (env {result['api_key_env']})",
        account=account_id,
    )
    return {**result, "rotated": True, "reloaded": reloaded, **lan_warning(request)}


@router.delete("/accounts/{provider}/{account_id}")
async def delete_account(
    request: Request, provider: str, account_id: str, purge_key: bool = False
) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    writer = RegistryWriter(hub.settings)
    try:
        result = writer.delete_account(provider=provider, account_id=account_id, purge_key=purge_key)
    except AccountError as exc:
        raise account_error(exc) from exc
    reloaded = reload_counts(hub)
    hub.store.add_event(
        kind="account",
        message=f"account removed {provider}/{account_id} (purge_key={purge_key})",
        account=account_id,
    )
    return {**result, "reloaded": reloaded, **lan_warning(request)}


@router.post("/registry/reload")
async def reload_registry(request: Request) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    reloaded = reload_counts(hub)
    hub.store.add_event(kind="registry", message="registry reloaded")
    return {"reloaded": True, **reloaded, **lan_warning(request)}


def vendor_body(raw: Any) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:2000]


async def probe_entry(hub: Hub, entry: Any, prompt: str = "ping") -> dict[str, Any]:
    """One `max_tokens: 1` call through the normal gateway path, no retry ladder.

    Returns the result shape of the test endpoint minus the provider/account fields, so quick
    add and the per-account test button run the exact same code. A cli backend ignores
    max_tokens and answers a bare "ping" with a question, so it gets an instruction instead.
    """
    hub.store.ensure_app(TEST_APP)
    if entry.kind == CLI_KIND and prompt == "ping":
        prompt = PROBE_PROMPT
    body = {
        "model": entry.model.id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1,
    }
    # a throwaway router: same store/quota/cooldowns, but one candidate and no retry backoff,
    # so a click on "test" cannot sit for a minute waiting out the retry ladder
    probe = Router(
        hub.registry,
        hub.store,
        hub.quota,
        retry_delays=(),
        cooldown_seconds=hub.router.cooldown_seconds,
        sleep=hub.router.sleep,
    )
    probe._semaphores = hub.router._semaphores
    probe._cooldowns = hub.router._cooldowns
    # a probe is a real call on a real account: it belongs in the live view like any other
    probe._in_flight = hub.router._in_flight

    from .gateway import call_model

    async def call(target: Any, attempt_no: int) -> Any:
        return await call_model(hub, target, body, TEST_APP, attempt_no)

    try:
        result = await probe.run([entry], call, app=TEST_APP, model_request=entry.key)
    except HTTPException as exc:
        return {
            "ok": False,
            "status": "error",
            "error_code": "bad_request",
            "http_status": exc.status_code,
            "latency_ms": 0,
            "attempts": [],
            "attempt_count": 1,
            "error": exc.detail,
        }
    except UpstreamError as exc:
        return {
            "ok": False,
            "status": exc.classification.kind,
            "error_code": exc.classification.code,
            "http_status": exc.status_code,
            "latency_ms": exc.latency_ms,
            "attempts": [],
            "attempt_count": 1,
            "error": vendor_body(exc.body),
        }
    except AllCandidatesFailed as exc:
        last = exc.last
        return {
            "ok": False,
            "status": last.classification.kind if last else "error",
            "error_code": last.classification.code if last else None,
            "http_status": last.status_code if last else None,
            "latency_ms": last.latency_ms if last else 0,
            "attempts": exc.attempts,
            "attempt_count": len(exc.attempts),
            "error": vendor_body(last.body) if last else None,
        }
    call_result = result.result
    payload_json = call_result.payload or {}
    choices = payload_json.get("choices") or []
    sample = ""
    if choices and isinstance(choices[0], dict):
        sample = str((choices[0].get("message") or {}).get("content") or "")[:200]
    return {
        "ok": True,
        "status": "ok",
        "http_status": call_result.status_code,
        "latency_ms": call_result.latency_ms,
        "attempts": result.attempts,
        "attempt_count": len(result.attempts),
        "usage": payload_json.get("usage"),
        "sample": sample,
    }


@router.post("/accounts/{provider}/{account_id}/test")
async def test_account(
    request: Request, provider: str, account_id: str, payload: AccountTestIn | None = None
) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    payload = payload or AccountTestIn()
    block = hub.registry.providers.get(provider)
    if block is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider}")
    if not any(account.id == account_id for account in block.accounts):
        raise HTTPException(status_code=404, detail=f"unknown account {provider}/{account_id}")
    model_id = payload.model or (block.models[0].id if block.models else None)
    if not model_id:
        raise HTTPException(status_code=400, detail=f"provider {provider} has no registered model")
    # a model id may itself contain a slash (openrouter/free), so try it as given first and
    # only then as a full provider/model key
    entry = hub.registry.entry(f"{provider}/{model_id}", account_id)
    if entry is None and model_id.startswith(f"{provider}/"):
        model_id = model_id.split("/", 1)[1]
        entry = hub.registry.entry(f"{provider}/{model_id}", account_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown model {provider}/{model_id}")
    if not entry.model.is_free and not payload.allow_paid:
        raise HTTPException(
            status_code=409,
            detail=f'{entry.key} is not marked free; resend with {{"allow_paid": true}}',
        )
    base = {
        "provider": provider,
        "account_id": account_id,
        "model": entry.key,
        "model_id": entry.model.id,
        **lan_warning(request),
    }
    if not entry.key_present:
        return {**base, "ok": False, "status": "no_key", "api_key_env": entry.account.api_key_env}
    return {**base, **await probe_entry(hub, entry, payload.prompt)}


async def discover_cli(
    hub: Hub, provider: str, block: Any, account_id: str, warning: dict[str, str]
) -> dict[str, Any]:
    """`<command> models` instead of `GET {base_url}/models`; first column is the model id."""
    base = {
        "provider": provider,
        "account_id": account_id,
        "command": block.command,
        **warning,
    }
    try:
        ids, run = await discover_cli_models(provider, block)
    except CliRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run.returncode != 0 or not ids:
        hub.store.add_event(
            kind="discover",
            message=f"{provider} `{block.command} models` exit {run.returncode}, {len(ids)} models",
        )
        return {
            **base,
            "ok": False,
            "exit_code": run.returncode,
            "models": [],
            "count": 0,
            "error": (run.stderr or run.stdout)[:2000],
        }
    return {
        **base,
        "ok": True,
        "exit_code": run.returncode,
        "models": ids,
        "count": len(ids),
        "registered": sorted({model.id for model in block.models}),
        "note": "limits unknown",
    }


@router.post("/providers/{provider}/discover")
async def discover_provider_models(
    request: Request, provider: str, payload: DiscoverIn | None = None
) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    payload = payload or DiscoverIn()
    block = hub.registry.providers.get(provider)
    if block is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider}")
    account = None
    for candidate in block.accounts:
        if payload.account_id is None or candidate.id == payload.account_id:
            account = candidate
            break
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account {provider}/{payload.account_id}")
    if block.kind == CLI_KIND:
        return await discover_cli(hub, provider, block, account.id, lan_warning(request))
    api_key = os.environ.get(account.api_key_env) if account.api_key_env else None
    if account.api_key_env and not api_key:
        raise HTTPException(status_code=400, detail=f"no key in env {account.api_key_env} for {provider}")
    base = {
        "provider": provider,
        "account_id": account.id,
        "base_url": block.base_url,
        **lan_warning(request),
    }
    spec, fields = discover_plan(block)
    try:
        rows, response = await discover_model_rows(
            hub.client,
            base_url=block.base_url,
            api_key=api_key,
            headers=dict(block.headers),
            spec=spec,
            fields=fields,
            template_id=provider_extra(block, "template"),
        )
    except AccountError as exc:
        raise account_error(exc) from exc
    except httpx.HTTPError as exc:
        hub.store.add_event(kind="discover", message=f"{provider} discover failed: {exc}")
        raise HTTPException(status_code=502, detail=f"{provider} /models failed: {exc}") from exc
    if response.status_code >= 400:
        return {
            **base,
            "ok": False,
            "http_status": response.status_code,
            "models": [],
            "count": 0,
            "error": vendor_body(response.content),
        }
    ids = sorted(str(row["id"]) for row in rows)
    registered = {model.id for model in block.models}
    return {
        **base,
        "ok": True,
        "http_status": response.status_code,
        "models": ids,
        "count": len(ids),
        "registered": sorted(registered),
        "note": "limits unknown",
    }


@router.post("/scout/run", status_code=202)
async def scout_run(request: Request, dry_run: bool = False) -> dict[str, Any]:
    require_token(request)
    hub = hub_of(request)
    if hub.scout is None:
        hub.scout = ScoutService(hub)
    try:
        run_id = hub.scout.start_run(dry_run=dry_run)
    except ScoutBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    hub.store.add_event(kind="scout", message=f"scout run {run_id} started")
    return {"run_id": run_id}


@router.get("/scout/runs")
async def scout_runs(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    rows = hub_of(request).store.scout_runs(limit)
    return {"runs": [run_row(row) for row in rows]}


@router.get("/scout/runs/{run_id}")
async def scout_run_detail(request: Request, run_id: int) -> dict[str, Any]:
    row = hub_of(request).store.scout_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown scout run {run_id}")
    return {
        **run_row(row),
        "report_md": row["report_md"] or "",
        "decisions": json.loads(row["decisions"] or "[]"),
        "error_details": json.loads(row["errors"] or "[]"),
    }


@router.get("/scout/status")
async def scout_status(request: Request) -> dict[str, Any]:
    hub = hub_of(request)
    service = hub.scout
    last = hub.store.last_scout_run()
    schedule = hub.settings.scout_at
    return {
        "active": bool(service and service.active),
        "next_run_at": service.next_run_at() if service else next_run_at(schedule),
        "last_run": run_row(last) if last else None,
        "schedule": schedule,
    }
