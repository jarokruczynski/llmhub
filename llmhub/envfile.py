from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ENV_DIR = Path.home() / ".llmhub" / "env"


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def source_env_dir(
    directory: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    env = os.environ if environ is None else environ
    if directory is None:
        directory = Path(env.get("LLMHUB_ENV_DIR", str(DEFAULT_ENV_DIR)))
    if not directory.is_dir():
        log.info("env dir %s not found, skipping", directory)
        return []
    loaded: list[str] = []
    for path in sorted(directory.glob("*.env")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cannot read env file %s: %s", path, exc)
            continue
        for key, value in parse_env_text(text).items():
            if key in env:
                continue
            env[key] = value
            loaded.append(key)
    log.info("sourced %d env vars from %s", len(loaded), directory)
    return loaded
