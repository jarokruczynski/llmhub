from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

import uvicorn

from .app import bootstrap_settings, create_app
from .runtime import Hub
from .status import check_table


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="llmhub", description="local LLM gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--log-level", default="info")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="print the registry state table and exit (no network calls)")
    scout = sub.add_parser("scout", help="run the promo scout once and print its report")
    scout.add_argument("--dry-run", action="store_true", help="print the decisions, post nothing")
    dedupe = sub.add_parser("promos-dedupe", help="give every promo row an identity and merge the duplicates")
    dedupe.add_argument("--dry-run", action="store_true", help="print the merge table, write nothing")
    refresh_context = sub.add_parser(
        "refresh-context", help="fill model.context from each provider's own model listing"
    )
    refresh_context.add_argument("--dry-run", action="store_true", help="print the table, write nothing")
    refresh_context.add_argument("--provider", default=None, help="limit the pass to one provider")
    return parser.parse_args(argv)


def run_check() -> str:
    hub = Hub.create(bootstrap_settings())
    try:
        return check_table(hub)
    finally:
        hub.store.close()


def run_scout(dry_run: bool = False) -> str:
    """Separate process: LLM calls go over the running hub's own /v1, the db is opened here."""
    from .scout import run as scout_run

    async def once(hub: Hub) -> dict:
        try:
            return await scout_run(hub, dry_run=dry_run)
        finally:
            await hub.client.aclose()

    hub = Hub.create(bootstrap_settings())
    try:
        outcome = asyncio.run(once(hub))
    finally:
        hub.store.close()
    lines = [str(outcome.get("report_md") or "")]
    if dry_run:
        lines.append("\ndecisions (nothing posted):")
        for decision in outcome.get("decisions") or []:
            lines.append(json.dumps(decision, ensure_ascii=False))
    return "\n".join(lines)


def run_promos_dedupe(dry_run: bool = False) -> str:
    """One-time migration for a watchlist filed before identities existed."""
    from .promo_identity import backup_promos, dedupe_promos, dedupe_table

    hub = Hub.create(bootstrap_settings())
    try:
        backup = backup_promos(hub.store, hub.settings.home / "backups")
        outcome = dedupe_promos(hub.store, hub.registry, dry_run=dry_run)
    finally:
        hub.store.close()
    return f"backup {backup}\n{dedupe_table(outcome)}"


def refresh_context_table(reports: list[dict[str, Any]]) -> str:
    header = (
        f"{'PROVIDER':<22} {'MODELS':>6} {'FILLED':>6} {'FROM_CATALOG':>12} "
        f"{'ALREADY_SET':>11} {'UNKNOWN':>7}  STATUS"
    )
    lines = [header, "-" * len(header)]
    for report in reports:
        lines.append(
            f"{report['provider']:<22} {report['models']:>6} {len(report['filled']):>6} "
            f"{len(report.get('from_catalog') or []):>12} "
            f"{report['already_set']:>11} {len(report['unknown']):>7}  {report['status']}"
        )
    return "\n".join(lines)


def run_refresh_context(dry_run: bool = False, provider: str | None = None) -> str:
    """One discovery pass per provider that can be listed over HTTP; fills `model.context`
    where the vendor's own listing named one and the registered model had none.

    Never overwrites a context the owner already set. Backs providers.yaml up before writing,
    same directory `promos-dedupe` uses, and writes only the fields that were actually missing.
    """
    from .accounts import apply_context_fills, backup_registry_file, refresh_context_report

    async def once(hub: Hub) -> list[dict[str, Any]]:
        try:
            return await refresh_context_report(hub.client, hub.registry, provider_filter=provider)
        finally:
            await hub.client.aclose()

    hub = Hub.create(bootstrap_settings())
    try:
        reports = asyncio.run(once(hub))
        lines = [refresh_context_table(reports)]
        filled_total = sum(len(report["filled"]) for report in reports)
        if dry_run:
            lines.append(f"\ndry run: {filled_total} field(s) would be written, nothing written")
        elif filled_total:
            backup = backup_registry_file(hub.settings.registry_path, hub.settings.home / "backups")
            written = apply_context_fills(hub.settings.registry_path, reports)
            lines.append(f"\nbackup {backup}")
            lines.append(f"wrote context on {written} model(s)")
            lines.append(
                "reload the running hub with: curl -X POST http://127.0.0.1:8800/api/registry/reload"
            )
            lines.append("(or restart it if it is not running)")
        else:
            lines.append("\nnothing to write")
    finally:
        hub.store.close()
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "promos-dedupe":
        logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s %(message)s")
        print(run_promos_dedupe(dry_run=args.dry_run))
        return
    if args.command == "refresh-context":
        logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s %(message)s")
        print(run_refresh_context(dry_run=args.dry_run, provider=args.provider))
        return
    if args.command == "check":
        logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s %(message)s")
        print(run_check())
        return
    if args.command == "scout":
        logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
        print(run_scout(dry_run=args.dry_run))
        return
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
