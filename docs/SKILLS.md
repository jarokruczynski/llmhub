# Claude Code skills

This repo ships two Claude Code skills under `.claude/skills/`. Anyone who clones
the repo and runs Claude Code from inside it gets them automatically, no install step.

## promo-hunt

Manual reconnaissance for free LLM/VLM API access: free tiers, $0 promo models,
trial credits, time-window promos, open-weight releases that fit a 48 GB Mac.
Posts new finds to the hub's Promos tab (`/api/promos`).

Trigger: `/promo-hunt`, "promo hunt", "szukaj darmowych api", "risercz promocji".
File: `.claude/skills/promo-hunt/SKILL.md`

## llmhub-client

Routes any LLM/VLM call from a script or app through the local hub
(`http://127.0.0.1:8800/v1`, OpenAI-compatible) instead of a vendor SDK. Covers
auth, headers (`X-Hub-App`, `X-Hub-Require`, `X-Hub-Prefer`), error handling
(400/401/429/502/503), and the async job queue for batch work.

Trigger: "hub", "llmhub", "free LLM", "batch LLM job", or code that would
otherwise call a vendor API directly for a task a free endpoint can handle.
File: `.claude/skills/llmhub-client/SKILL.md`

## How skills load

Claude Code discovers project-level skills under `.claude/skills/<name>/SKILL.md`
automatically for any session started inside this repo. No config file needed.

## Making a skill global (repo owner, own machine)

Project skills only load inside the repo. To use one from any directory, replace
the user-level copy with a symlink into the repo (back up first if it has local
edits worth keeping):

```bash
ln -s /path/to/llmhub/.claude/skills/promo-hunt ~/.claude/skills/promo-hunt
ln -s /path/to/llmhub/.claude/skills/llmhub-client ~/.claude/skills/llmhub-client
```
