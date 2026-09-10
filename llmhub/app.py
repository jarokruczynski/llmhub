from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .config import Settings
from .envfile import source_env_dir
from .jobs import JobQueue
from .runtime import Hub
from .scout import ScoutService

log = logging.getLogger(__name__)


def bootstrap_settings(environ: dict[str, str] | None = None) -> Settings:
    settings = Settings.from_env(environ)
    source_env_dir(settings.env_dir)
    return Settings.from_env(environ)


def create_app(settings: Settings | None = None, hub: Hub | None = None) -> FastAPI:
    if hub is None:
        settings = settings or bootstrap_settings()
        hub = Hub.create(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.hub.jobs.start()
        await app.state.hub.scout.start()
        log.info("llmhub %s started, registry %s", __version__, app.state.hub.settings.registry_path)
        try:
            yield
        finally:
            await app.state.hub.scout.stop()
            await app.state.hub.jobs.stop()
            await app.state.hub.aclose()

    app = FastAPI(title="llmhub", version=__version__, lifespan=lifespan)
    app.state.hub = hub
    hub.jobs = JobQueue(hub)
    hub.scout = ScoutService(hub)

    from . import api, gateway, jobs

    app.include_router(gateway.router)
    app.include_router(api.router)
    app.include_router(jobs.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    try:
        from llmhub.dashboard.routes import router as dashboard_router

        app.include_router(dashboard_router)
    except ImportError:
        log.info("dashboard router not present")

    return app
