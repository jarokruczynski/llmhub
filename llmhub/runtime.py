from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Registry, Settings, load_registry
from .envfile import source_env_dir
from .quota import QuotaTracker
from .router import Router
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)


class Hub:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        store: Store,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.store = store
        self.quota = QuotaTracker(store)
        self.router = Router(
            registry,
            store,
            self.quota,
            not_found_ttl_s=settings.not_found_ttl_s,
            unavailable_ttl_s=settings.unavailable_ttl_s,
        )
        self.client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=False)
        self.jobs: Any = None
        self.scout: Any = None

    @classmethod
    def create(cls, settings: Settings, client: httpx.AsyncClient | None = None) -> Hub:
        settings.home.mkdir(parents=True, exist_ok=True)
        registry = load_registry(settings.registry_path)
        store = Store(settings.db_path)
        return cls(settings, registry, store, client)

    def reload(self) -> Registry:
        """Re-read the yaml and the env files, then swap in a fresh router.

        A request already running keeps the Router object it started on, so nothing it holds
        changes underneath it; the swap of `self.router` is a single attribute assignment.
        Cooldowns and the per (account, model) semaphores carry over - dropping them would let
        a reload bypass a live concurrency limit. So does the LRU clock the spread strategy
        reads, so a reload does not send every alias back to its first entry.
        """
        source_env_dir(self.settings.env_dir)
        registry = load_registry(self.settings.registry_path)
        router = Router(
            registry,
            self.store,
            self.quota,
            retry_delays=self.router.retry_delays,
            cooldown_seconds=self.router.cooldown_seconds,
            sleep=self.router.sleep,
            retry_wait_max_s=self.router.retry_wait_max_s,
            not_found_ttl_s=self.router.not_found_ttl_s,
            unavailable_ttl_s=self.router.unavailable_ttl_s,
            attempt_grace_s=self.router.attempt_grace_s,
        )
        router._semaphores = self.router._semaphores
        router._cooldowns = self.router._cooldowns
        # a reload is not a re-admission: the parked pairs and their table rows both survive it
        router._unavailable = self.router._unavailable
        router._last_used = self.router._last_used
        router._last_used_loaded = self.router._last_used_loaded
        # a stream registered on the old router is released through the new one, and the kill
        # switch has to keep reaching a call that started before the reload
        router._in_flight = self.router._in_flight
        router._tasks = self.router._tasks
        router._cancel_reason = self.router._cancel_reason
        router._waiters = self.router._waiters
        router._timeouts = self.router._timeouts
        router.cancelled_jobs = self.router.cancelled_jobs
        self.registry = registry
        self.router = router
        log.info(
            "registry reloaded: %d providers, %d (account, model) pairs",
            len(registry.providers),
            len(list(registry.entries())),
        )
        return registry

    def reload_registry(self) -> Registry:
        return self.reload()

    async def aclose(self) -> None:
        await self.client.aclose()
        self.store.close()
