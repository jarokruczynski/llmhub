from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from llmhub.app import create_app
from llmhub.config import Settings
from llmhub.runtime import Hub
from llmhub.vendor_errors import Classification

REGISTRY_YAML = """
providers:
  alpha:
    kind: openai
    base_url: https://alpha.test/v1
    accounts:
      - {id: alpha-1, api_key_env: ALPHA_KEY_1}
      - {id: alpha-2, api_key_env: ALPHA_KEY_2}
    models:
      - id: m1
        caps: [text, tools]
        context: 100000
        reset_tz: UTC
        free:
          hourly: {out_tokens: 1000}
          daily: {in_tokens: 10000, out_tokens: 5000}
        extra_body: {thinking: {type: disabled}}
      - id: paid1
        caps: [text]
  beta:
    kind: openai
    base_url: https://beta.test/v1
    accounts:
      - {id: beta-1, api_key_env: BETA_KEY}
    models:
      - id: m2
        caps: [text, vision]
        free: {}
        reset_tz: Europe/Warsaw
  gamma:
    kind: openai
    base_url: https://gamma.test/v1
    accounts:
      - {id: gamma-1, api_key_env: GAMMA_KEY, activated_at: 2026-09-01}
      - {id: gamma-2, api_key_env: GAMMA_KEY_2}
    models:
      - id: fixed
        caps: [text]
        free:
          allowance: {total_tokens: 1000, expires_at: 2026-12-01}
      - id: openended
        caps: [text]
        free:
          allowance: {total_tokens: 1000}
aliases:
  auto:
    prefer: [alpha/m1, beta/m2]
  vision:
    require: [vision]
    prefer: [beta/m2]
"""


@pytest.fixture
def env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_KEY_1", "test-key-1")
    monkeypatch.setenv("ALPHA_KEY_2", "test-key-2")
    monkeypatch.setenv("BETA_KEY", "test-key-beta")
    monkeypatch.setenv("GAMMA_KEY", "test-key-gamma")
    monkeypatch.setenv("GAMMA_KEY_2", "test-key-gamma-2")
    for name in (
        "LLMHUB_JOB_WORKERS",
        "LLMHUB_JOBS_PER_APP",
        "LLMHUB_JOBS_PER_APP_MIN",
        "LLMHUB_JOB_TTL_S",
        "LLMHUB_JOB_TTL_MAX_S",
        "LLMHUB_JOB_LEASE_MARGIN_S",
        "LLMHUB_QUEUE_MAX_PER_APP",
        "LLMHUB_JOB_RETENTION_DAYS",
        "LLMHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_keys: None) -> Settings:
    home = tmp_path / "home"
    home.mkdir()
    registry_path = home / "providers.yaml"
    registry_path.write_text(REGISTRY_YAML, encoding="utf-8")
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("LLMHUB_HOME", str(home))
    monkeypatch.setenv("LLMHUB_REGISTRY", str(registry_path))
    monkeypatch.setenv("LLMHUB_DB", str(home / "hub.db"))
    monkeypatch.setenv("LLMHUB_ENV_DIR", str(env_dir))
    return Settings.from_env()


@pytest.fixture
def hub(settings: Settings) -> Iterator[Hub]:
    instance = Hub.create(settings)
    instance.router.retry_delays = ()
    yield instance
    instance.store.close()


@pytest.fixture
async def client(hub: Hub) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(hub=hub)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8800") as test_client:
        yield test_client


def token_quota(scope: str | None = None, message: str = "quota exceeded, out of tokens") -> Classification:
    """A quota hit whose body names tokens but no metric, window or ceiling of its own.

    The shape most free tiers actually answer with, and the only one the used-so-far
    heuristic still applies to.
    """
    return Classification("quota", None, message, "test-quota", scope=scope)


def entry_of(hub: Hub, key: str, account: str | None = None):
    entry = hub.registry.entry(key, account)
    assert entry is not None
    return entry
