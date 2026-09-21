# Docs index

- [ARCHITECTURE.md](ARCHITECTURE.md) - current system diagrams: overview, request lifecycle, scout pipeline.
- [DESIGN.md](DESIGN.md) - registry schema, gateway/queue API contract, dashboard API.
- [CLIENT_PROMPT.md](CLIENT_PROMPT.md) - paste-in brief for a coding agent calling the hub.
- [SKILLS.md](SKILLS.md) - the two Claude Code skills shipped in `.claude/skills/` (promo-hunt, llmhub-client).
- [providers_probe.md](providers_probe.md) - read-only recon notes on vendor quota behavior,
  used to sanity-check the registry against what each vendor actually returns.
- [jobs_park_defects.md](jobs_park_defects.md) - why the queue failed 20926 jobs it had
  promised to retry, and why parked jobs were re-attempted every two seconds. Measured from
  `hub.db` and its 09-16 snapshots; both fixed.
- [ui/](ui) - dashboard screenshots referenced from the top-level README.
