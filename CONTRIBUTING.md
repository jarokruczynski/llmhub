# Contributing

This is a single-maintainer tool. Contributions are welcome but kept small.

- Use `uv` for everything: `uv sync`, `uv run <command>`. Do not use bare `pip`.
- Run `uv run pytest -q` before opening a PR. Upstream calls are mocked with `respx`; no
  network access is required or allowed in tests.
- Run `uv run ruff check .` and `uv run ruff format --check .`; fix what they flag.
- `docs/DESIGN.md` is the contract for the registry schema and the API. Change the code
  and the doc in the same PR when behavior or schema changes.
- Code has no comments. Prefer clear names and small functions over inline explanation.
- Identifiers, error messages and docs are English. Commit messages are English, short,
  and technical: what changed and why, not a narration of the diff.
- Keep changes scoped: one behavior or fix per PR. Do not mix registry schema changes
  with unrelated dashboard changes.
