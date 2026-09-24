"""Read a pooled CLI's own quota report and park the groups it says are empty.

agy answers `agy -p /quota --output-format json` without spending anything: per model group
(`command.data.groups`), a weekly and a five-hour bucket with `remaining_fraction` and
`reset_time`. A refusal names only the window of the model that was refused; the report names
every group. Reading it at startup and after each refusal lets the hub park a spent group
before any reader burns a call to learn it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .cli_backend import cli_env, parse_json_object, prepare_workdir, resolve_command, run_cli, workdir_of
from .config import Entry
from .providers_catalog import quota_groups, quota_shared
from .store import parse_iso

if TYPE_CHECKING:
    from .runtime import Hub

log = logging.getLogger(__name__)

QUOTA_ARGS = ("-p", "/quota", "--output-format", "json")
# measured about 5 s; a hung report must not hold anything
PROBE_TIMEOUT_S = 30.0
# a burst of refusals on one login reads the report once
PROBE_MIN_INTERVAL_S = 60.0

Runner = Callable[[Entry], Awaitable[dict[str, Any] | None]]


def empty_groups(payload: dict[str, Any] | None, now: datetime) -> dict[str, datetime]:
    """Group name -> reset instant, for each group with a live bucket at zero.

    A `disabled` bucket does not bind: agy disables the five-hour bucket while the weekly one
    is spent, and reports it at 100%. With both at zero the later reset is the one that frees
    the group.
    """
    data = ((payload or {}).get("command") or {}).get("data") or {}
    out: dict[str, datetime] = {}
    for group in data.get("groups") or []:
        name = group.get("name")
        if not name:
            continue
        for bucket in group.get("buckets") or []:
            if bucket.get("disabled") or bucket.get("reset_time") is None:
                continue
            try:
                fraction = float(bucket.get("remaining_fraction"))
                until = parse_iso(str(bucket["reset_time"]))
            except (TypeError, ValueError):
                continue
            if fraction > 0 or until <= now:
                continue
            out[name] = max(out.get(name, until), until)
    return out


async def run_quota(entry: Entry) -> dict[str, Any] | None:
    """The report as the provider's own login sees it: same env, HOME and workdir as a call."""
    provider = entry.provider
    env = cli_env(provider)
    command = resolve_command(entry.cli_command, env)
    cwd = prepare_workdir(workdir_of(entry.provider_name, provider))
    run = await run_cli([command, *QUOTA_ARGS], cwd=cwd, env=env, timeout_s=PROBE_TIMEOUT_S)
    if run.returncode != 0 or run.timed_out:
        log.warning("%s /quota failed (%s): %s", entry.provider_name, run.returncode, run.stderr[:200])
        return None
    return parse_json_object(run.stdout)


class PoolProbe:
    def __init__(self, hub: Hub, runner: Runner | None = None) -> None:
        self.hub = hub
        self.runner = runner or run_quota
        self._tasks: dict[str, asyncio.Task[int]] = {}
        self._last: dict[str, datetime] = {}

    def anchors(self) -> list[Entry]:
        """One entry per pool (template, command): the pool is shared, one login reads it."""
        seen: dict[tuple[str, str], Entry] = {}
        for entry in self.hub.registry.entries():
            if not entry.is_cli or not quota_shared(entry.template_id) or not quota_groups(entry.template_id):
                continue
            seen.setdefault((entry.template_id, entry.cli_command), entry)
        return list(seen.values())

    async def probe(self, entry: Entry, now: datetime | None = None) -> int:
        """Read the report through `entry`'s login and park its empty groups. Pairs parked."""
        payload = await self.runner(entry)
        now = (now or datetime.now(UTC)).astimezone(UTC)
        router = self.hub.router
        parked = 0
        for name, until in empty_groups(payload, now).items():
            if name not in quota_groups(entry.template_id):
                log.warning("%s /quota names group %r the template does not map", entry.provider_name, name)
                continue
            members = router.pool_members(entry, name)
            note = f"pool group {name} at 0% per /quota via {entry.provider_name}"
            parked += len(router.park_entries(members, until, note))
        return parked

    def schedule(self, entry: Entry) -> None:
        """Probe in the background; at most one run per provider, and one per minute."""
        name = entry.provider_name
        now = datetime.now(UTC)
        running = self._tasks.get(name)
        if running is not None and not running.done():
            return
        last = self._last.get(name)
        if last is not None and now - last < timedelta(seconds=PROBE_MIN_INTERVAL_S):
            return
        self._last[name] = now
        task = asyncio.get_running_loop().create_task(self._guarded(entry), name=f"llmhub-quota-{name}")
        self._tasks[name] = task

    async def _guarded(self, entry: Entry) -> int:
        try:
            return await self.probe(entry)
        except Exception:
            log.exception("%s /quota probe failed", entry.provider_name)
            return 0

    async def start(self) -> None:
        self.hub.router.pool_probe = self.schedule
        for entry in self.anchors():
            self.schedule(entry)

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
