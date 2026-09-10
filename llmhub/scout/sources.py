from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..config import example_path
from ..providers_catalog import known_providers

log = logging.getLogger(__name__)

EXAMPLE_FILE = "scout_sources.example.yaml"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PEPPER_SEARCH_URL = "https://www.pepper.pl/search?q={query}"
# pepper listing pages are long and sometimes slow to answer; the default collect timeout
# (10s) was tight enough to occasionally lose them outright
PEPPER_FETCH_TIMEOUT = 20.0


class PepperConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    search_url: str = PEPPER_SEARCH_URL
    keywords: list[str] = Field(default_factory=list)


class FeedConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    url: str
    enabled: bool = False


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    api_key_env: str | None = None
    url_env: str | None = None


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    results: int = 5
    brave: EngineConfig = Field(default_factory=lambda: EngineConfig(api_key_env="BRAVE_SEARCH_API_KEY"))
    searxng: EngineConfig = Field(default_factory=lambda: EngineConfig(url_env="LLMHUB_SEARXNG_URL"))
    queries: list[str] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    pepper: PepperConfig = Field(default_factory=PepperConfig)
    catalog_docs: bool = True
    openrouter_free: bool = True
    pages: list[Any] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=SearchConfig)


@dataclass(frozen=True)
class Source:
    """One thing to fetch. `page` and `feed` carry a url; `openrouter_free` and `search`
    build their own request and turn the answer into a page.

    `polite=True` means: skip the fetch entirely when the cached page is less than
    `POLITE_INTERVAL` old, for a source that answers slowly or blocks a frequent scripted GET.
    `timeout` overrides the collector's default per-request timeout for this one source.
    """

    id: str
    kind: str
    url: str | None = None
    engine: str | None = None
    query: str | None = None
    secret: str | None = None
    polite: bool = False
    timeout: float | None = None


def example_sources_path() -> Path:
    return example_path(EXAMPLE_FILE)


def load_sources(path: Path) -> SourcesConfig:
    """Read the source list, copying the shipped example on first run like providers.yaml."""
    if not path.exists():
        example = example_sources_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, path)
        log.info("scout sources missing, copied example %s -> %s", example, path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"scout sources {path} must be a mapping")
    return SourcesConfig.model_validate(raw)


def _page_source(item: Any, index: int) -> Source | None:
    if isinstance(item, str):
        return Source(id=f"page-{index}", kind="page", url=item)
    if isinstance(item, dict):
        if not item.get("url") or item.get("enabled") is False:
            return None
        return Source(id=str(item.get("id") or f"page-{index}"), kind="page", url=str(item["url"]))
    return None


def catalog_docs_sources() -> list[Source]:
    seen: set[str] = set()
    sources: list[Source] = []
    for item in known_providers():
        url = item.get("docs_url")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            Source(id=f"docs-{item['id']}", kind="page", url=str(url), polite=bool(item.get("scout_polite")))
        )
    return sources


def resolve_sources(config: SourcesConfig, environ: dict[str, str] | None = None) -> list[Source]:
    """Expand the config into the concrete fetches for one run."""
    env = dict(os.environ if environ is None else environ)
    sources: list[Source] = []

    if config.pepper.enabled:
        for keyword in config.pepper.keywords:
            url = config.pepper.search_url.replace("{query}", quote_plus(keyword))
            sources.append(Source(id=f"pepper-{keyword}", kind="page", url=url, timeout=PEPPER_FETCH_TIMEOUT))

    if config.catalog_docs:
        sources.extend(catalog_docs_sources())

    if config.openrouter_free:
        sources.append(Source(id="openrouter-free", kind="openrouter_free", url=OPENROUTER_MODELS_URL))

    for index, item in enumerate(config.pages):
        page = _page_source(item, index)
        if page is not None:
            sources.append(page)

    for index, feed in enumerate(config.feeds):
        if feed.enabled:
            sources.append(Source(id=feed.id or f"feed-{index}", kind="feed", url=feed.url))

    if config.search.enabled and config.search.queries:
        brave_key = env.get(config.search.brave.api_key_env or "") if config.search.brave.enabled else None
        searxng_url = env.get(config.search.searxng.url_env or "") if config.search.searxng.enabled else None
        for query in config.search.queries:
            if brave_key:
                sources.append(
                    Source(id=f"brave-{query}", kind="search", engine="brave", query=query, secret=brave_key)
                )
            if searxng_url:
                sources.append(
                    Source(
                        id=f"searxng-{query}",
                        kind="search",
                        engine="searxng",
                        query=query,
                        url=searxng_url.rstrip("/"),
                    )
                )
    return sources
