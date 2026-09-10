from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .accounts import (
    GENERIC_HOSTS,
    SourceUnresolved,
    bare_domain,
    provider_slug,
    resolve_source,
    urls_in,
)
from .config import Registry
from .store import Store

# Hosts that carry offers from many vendors at once. A url on one of them says who published
# the news, not whose free tier it is, so it must never become an identity or be learned as an
# alias: it would fold unrelated rows (three different local models on ollama.com) into one.
SHARED_HOSTS: frozenset[str] = frozenset(GENERIC_HOSTS) | frozenset(
    {
        "huggingface.co",
        "hf.co",
        "ollama.com",
        "pepper.pl",
        "producthunt.com",
        "youtube.com",
        "substack.com",
        "news.ycombinator.com",
    }
)

# a loopback base_url is where the model runs, not who it is from: every local runtime shares it
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "0.0.0.0", "localhost", "::1"})

# a one- or two-letter provider name is not evidence of anything; learning it as an alias
# would tie a whole vendor to a typo
MIN_ALIAS_LEN = 3


@dataclass(frozen=True)
class Identity:
    """Who a promo row is about. `key` is what rows are grouped and merged by."""

    key: str
    kind: str
    detail: str


def norm_name(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def name_alias(provider: str | None) -> str | None:
    name = norm_name(provider)
    return f"name:{name}" if len(name) >= MIN_ALIAS_LEN else None


def hosts_of(*texts: str | None) -> list[str]:
    """Bare domains worth identifying by, in the order the texts named them."""
    found: list[str] = []
    for text in texts:
        for host in urls_in(text):
            domain = bare_domain(host)
            if not domain or "." not in domain or domain in found:
                continue
            if domain in SHARED_HOSTS or domain in LOOPBACK_HOSTS:
                continue
            found.append(domain)
    return found


def _from_registry(registry: Registry, name: str, hosts: list[str]) -> tuple[str, str] | None:
    """A provider already in providers.yaml, by its id or by the host it calls."""
    slug = provider_slug(name)
    for provider_id in registry.providers:
        if name == provider_id or (slug and slug == provider_id):
            return provider_id, f"registry id {provider_id}"
    by_host = [
        provider_id
        for provider_id, block in registry.providers.items()
        if any(host in hosts for host in hosts_of(block.base_url))
    ]
    # two providers on one host is the ambiguity this whole module exists to remove; guessing
    # between them here would pick the wrong one silently
    if len(set(by_host)) == 1:
        return by_host[0], f"registry base_url host {by_host[0]}"
    return None


def identify(
    provider: str,
    url: str | None,
    base_url: str | None,
    store: Store,
    registry: Registry | None = None,
    *,
    learn: bool = True,
) -> Identity:
    """Name the vendor a promo row is about.

    Learned aliases first, then the provider catalog, then the live registry, then the host
    the row points at, then the provider name itself. Every answer but the last is written
    back into `promo_aliases`, so the name and the host that produced it short-circuit the
    next time - the hub gets faster and more certain the more the owner posts.
    """
    name = norm_name(provider)
    hosts = hosts_of(url, base_url)
    aliases = [alias for alias in (name_alias(provider), *(f"host:{host}" for host in hosts)) if alias]

    for alias in aliases:
        learned = store.promo_alias(alias)
        if learned:
            return Identity(learned, "alias", alias)

    # a link to a shared host is dropped before the catalog sees it: matching a template on
    # github.com would file every project hosted there under the same vendor
    own_urls = " ".join(text for text in (url, base_url) if text and hosts_of(text))

    found: tuple[str, str] | None = None
    try:
        template_id, _, reason = resolve_source(
            text=provider,
            promo_row={"provider": provider, "url": own_urls},
        )
        found = (template_id, f"template {template_id} ({reason})")
    except SourceUnresolved:
        found = None
    kind = "template"

    if found is None and registry is not None:
        found = _from_registry(registry, name, hosts)
        kind = "registry"

    if found is None and hosts:
        found = (hosts[0], f"url host {hosts[0]}")
        kind = "host"

    if found is None:
        # nothing but the name the poster typed: usable as a key, never as an alias - it would
        # freeze a guess into the table and pull later rows onto it
        return Identity(provider_slug(name) or "unknown", "fallback", "provider name")

    key, detail = found
    if learn:
        for alias in aliases:
            store.learn_promo_alias(alias, key, kind)
    return Identity(key, kind, detail)


def registry_account(registry: Registry | None, key: str) -> str | None:
    """`<provider>/<account_id>` when this identity is already a registered account.

    An account is proof the offer was taken up, so a fresh post about it is not a new lead.
    """
    if registry is None:
        return None
    for provider_id, block in registry.providers.items():
        template = (block.model_extra or {}).get("template")
        if key not in (provider_id, provider_slug(provider_id), template):
            continue
        if block.accounts:
            return f"{provider_id}/{block.accounts[0].id}"
    return None


def provider_for_base_url(registry: Registry | None, base_url: str | None) -> str | None:
    """The registered provider that already calls this host, if exactly one does.

    Quick add names a new provider after the endpoint it guessed, which is how one vendor ends
    up in the registry twice under two spellings. Reusing the id keeps the key, the models and
    the routing history in one place.
    """
    if registry is None:
        return None
    hosts = hosts_of(base_url)
    if not hosts:
        return None
    matched = {
        provider_id
        for provider_id, block in registry.providers.items()
        if any(host in hosts for host in hosts_of(block.base_url))
    }
    return matched.pop() if len(matched) == 1 else None


def duplicate_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identities held by more than one row: what the dedupe migration would collapse."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("identity") or "")
        if key:
            groups.setdefault(key, []).append(row)
    return [
        {
            "identity": key,
            "count": len(members),
            "ids": [int(item["id"]) for item in members],
            "providers": [str(item.get("provider") or "") for item in members],
        }
        for key, members in sorted(groups.items())
        if len(members) > 1
    ]


def backup_promos(store: Store, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = directory / f"promos-{stamp}.json"
    path.write_text(json.dumps(store.promos(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("found_at") or ""), int(row["id"]))


def dedupe_promos(store: Store, registry: Registry | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Give every promo row an identity and fold each group onto its oldest row.

    Rows are walked oldest first so the merged notes read in the order the offers were found,
    and so the row that keeps the id is the one the owner has seen the longest.
    """
    rows = sorted(store.promos(), key=_sort_key)
    by_identity: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        identity = identify(
            str(row.get("provider") or ""),
            row.get("url"),
            row.get("base_url"),
            store,
            registry,
            learn=not dry_run,
        )
        by_identity.setdefault(identity.key, []).append({**row, "identity_kind": identity.kind})

    merges: list[dict[str, Any]] = []
    for key, members in by_identity.items():
        canonical = members[0]
        if not dry_run:
            store.set_promo_identity(int(canonical["id"]), key)
        if len(members) == 1:
            continue
        merges.append(
            {
                "identity": key,
                "kind": canonical["identity_kind"],
                "canonical_id": int(canonical["id"]),
                "canonical_provider": str(canonical.get("provider") or ""),
                "merged_ids": [int(item["id"]) for item in members[1:]],
                "merged_providers": [str(item.get("provider") or "") for item in members[1:]],
            }
        )
        if dry_run:
            continue
        for item in members[1:]:
            store.upsert_promo(
                identity=key,
                provider=str(item.get("provider") or ""),
                url=item.get("url"),
                note=item.get("note"),
                status=str(item.get("status") or "new"),
                source=item.get("source"),
                expires_at=item.get("expires_at"),
                base_url=item.get("base_url"),
                api_key_env=item.get("api_key_env"),
                account_key=item.get("account_key"),
                seen_at=item.get("found_at"),
            )
            store.delete_promo(int(item["id"]))

    return {
        "before": len(rows),
        "after": len(by_identity),
        "groups": len(by_identity),
        "merges": sorted(merges, key=lambda item: item["identity"]),
        "dry_run": dry_run,
    }


def dedupe_table(outcome: dict[str, Any]) -> str:
    """The migration summary: which ids were folded into which row, per identity."""
    lines = [
        f"promos {outcome['before']} -> {outcome['after']} rows "
        f"({len(outcome['merges'])} groups merged)" + (" [dry run]" if outcome["dry_run"] else ""),
        f"{'identity':<28} {'kind':<9} {'into':>5}  merged ids",
    ]
    for merge in outcome["merges"]:
        ids = ", ".join(str(item) for item in merge["merged_ids"])
        lines.append(f"{merge['identity'][:28]:<28} {merge['kind']:<9} {merge['canonical_id']:>5}  {ids}")
        lines.append(
            f"{'':<28} {'':<9} {'':>5}  {merge['canonical_provider']} <- {'; '.join(merge['merged_providers'])}"
        )
    if not outcome["merges"]:
        lines.append("no duplicate identities")
    return "\n".join(lines)
