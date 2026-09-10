from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import httpx

from ..store import Store, parse_iso
from .sources import Source

log = logging.getLogger(__name__)

USER_AGENT = "llmhub-scout/0.1 (+local)"
TIMEOUT = 10.0
CONCURRENCY = 3
MAX_TEXT = 24000
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
# `Source.polite=True`: one fetch per this interval, honoured off the scout_pages cache
# timestamp - for a source that blocks or throttles a frequent scripted GET
POLITE_INTERVAL = timedelta(days=7)


class _TextExtractor(HTMLParser):
    """Fallback html -> text: drop script/style, keep block boundaries as newlines."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)


def strip_tags(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is still worth what parsed so far
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def html_to_text(html: str) -> str:
    """trafilatura when it is installed (optional extra `scout`), the built-in stripper otherwise."""
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return strip_tags(html)
    try:
        extracted = trafilatura.extract(html) or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura failed, falling back to the tag stripper: %s", exc)
        extracted = ""
    return extracted.strip() or strip_tags(html)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def openrouter_free_text(payload: Any) -> str:
    """OpenRouter model list filtered to $0 prompt and completion."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    lines: list[str] = ["OpenRouter models priced at $0 prompt and $0 completion:"]
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        pricing = item.get("pricing") or {}
        try:
            free = float(pricing.get("prompt", 1)) == 0.0 and float(pricing.get("completion", 1)) == 0.0
        except (TypeError, ValueError):
            free = False
        if not free:
            continue
        lines.append(
            f"- {item.get('id')} | {item.get('name') or ''} | context {item.get('context_length') or '?'}"
        )
    return "\n".join(lines)


def search_results_text(engine: str, query: str, payload: Any) -> str:
    if engine == "brave":
        rows = ((payload or {}).get("web") or {}).get("results") or []
        fields = ("title", "url", "description")
    else:
        rows = (payload or {}).get("results") or []
        fields = ("title", "url", "content")
    lines = [f"{engine} search results for: {query}"]
    for item in rows:
        if not isinstance(item, dict):
            continue
        title, url, snippet = (str(item.get(name) or "") for name in fields)
        lines.append(f"- {title} | {url} | {snippet}")
    return "\n".join(lines)


@dataclass
class Page:
    source_id: str
    url: str
    text: str = ""
    status: str = "changed"
    content_hash: str = ""
    error: str | None = None


@dataclass
class CollectResult:
    pages: list[Page] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sources: int = 0

    @property
    def fetched(self) -> int:
        return sum(1 for page in self.pages if page.status in ("changed", "unchanged"))

    @property
    def changed(self) -> list[Page]:
        return [page for page in self.pages if page.status == "changed"]


def _fetched_recently(fetched_at: Any, interval: timedelta = POLITE_INTERVAL) -> bool:
    if not fetched_at:
        return False
    try:
        return datetime.now(UTC) - parse_iso(str(fetched_at)) < interval
    except ValueError:
        return False


async def fetch_text(client: httpx.AsyncClient, source: Source, results: int = 5) -> tuple[str, str]:
    """Fetch one source and return (url used, text). Raises on transport/HTTP failure."""
    headers = {"user-agent": USER_AGENT, "accept": "*/*"}
    if source.kind == "search" and source.engine == "brave":
        url = BRAVE_URL
        response = await client.get(
            url,
            params={"q": source.query, "count": results},
            headers={**headers, "accept": "application/json", "x-subscription-token": source.secret or ""},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return f"{url}?q={source.query}", search_results_text("brave", source.query or "", response.json())
    if source.kind == "search":
        url = f"{source.url}/search"
        response = await client.get(
            url,
            params={"q": source.query, "format": "json"},
            headers={**headers, "accept": "application/json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return f"{url}?q={source.query}", search_results_text("searxng", source.query or "", response.json())

    url = source.url or ""
    response = await client.get(
        url, headers=headers, timeout=source.timeout or TIMEOUT, follow_redirects=True
    )
    response.raise_for_status()
    if source.kind == "openrouter_free":
        return f"{url}#free", openrouter_free_text(response.json())
    body = response.text
    if "json" in (response.headers.get("content-type") or ""):
        try:
            return url, json.dumps(response.json(), ensure_ascii=False)[:MAX_TEXT]
        except ValueError:
            pass
    return url, html_to_text(body)


async def collect(
    store: Store,
    client: httpx.AsyncClient,
    sources: list[Source],
    *,
    results: int = 5,
) -> CollectResult:
    """Fetch every source, cache the text by url and mark unchanged pages.

    One bad source is an error line in the report, never the end of the run.
    """
    outcome = CollectResult(sources=len(sources))
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(source: Source) -> Page:
        if source.polite and source.url:
            previous = store.scout_page(source.url)
            if previous and previous.get("text") and _fetched_recently(previous.get("fetched_at")):
                return Page(
                    source_id=source.id,
                    url=source.url,
                    text=str(previous.get("text") or ""),
                    status="unchanged",
                    content_hash=str(previous.get("content_hash") or ""),
                )

        async with semaphore:
            try:
                url, text = await fetch_text(client, source, results=results)
            except Exception as exc:  # noqa: BLE001 - a dead source must not end the run
                url = source.url or source.id
                message = f"{source.id}: {type(exc).__name__}: {exc}"[:300]
                log.info("scout source failed %s", message)
                previous = store.scout_page(url)
                store.save_scout_page(
                    url,
                    (previous or {}).get("content_hash", ""),
                    (previous or {}).get("text", ""),
                    status="error",
                )
                return Page(source_id=source.id, url=url, status="error", error=message)

        text = text[:MAX_TEXT]
        digest = content_hash(text)
        previous = store.scout_page(url)
        unchanged = bool(previous) and previous.get("content_hash") == digest and previous.get("text")
        store.save_scout_page(url, digest, text, status="ok")
        return Page(
            source_id=source.id,
            url=url,
            text=text,
            status="unchanged" if unchanged else "changed",
            content_hash=digest,
        )

    pages = await asyncio.gather(*(one(source) for source in sources))
    outcome.pages = list(pages)
    outcome.errors = [page.error for page in pages if page.error]
    return outcome
