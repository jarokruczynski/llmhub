# Security

## Threat model

This is a single-user, local-first gateway. Assumptions below are load-bearing; do not
deploy this outside them without changes.

- Provider API keys live only as `KEY=value` lines in `~/.llmhub/env/<provider>.env`, files
  created chmod 600. Keys are never written to the registry, the database, or logs.
- Loopback (`127.0.0.1`) is fully open: no auth on the gateway or the dashboard from the
  Mac itself. Anything that can reach `127.0.0.1:8800` can spend quota and read state.
- Non-loopback (LAN) access requires `Authorization: Bearer $LLMHUB_TOKEN` on the gateway
  and on every mutating `api/*` route. Dashboard read routes stay open on the LAN.
- Transport on the LAN is plain HTTP, not TLS. A key submitted through `api/accounts` or
  `api/accounts/quick` from a LAN client crosses the network in the clear; the response
  carries an explicit `warning` when that happens.
- There is no multi-user model: one bearer token, one set of accounts, no per-caller
  isolation. Anyone holding the token has full control (add/rotate/remove keys, disable
  models, drain the queue).
- The database and env files are not encrypted at rest; filesystem permissions are the
  only protection.

## Reporting a vulnerability

Open a private security advisory on this repository (Security tab -> Report a
vulnerability), or open an issue without exploit details and ask for a private channel.
Do not include real API keys, tokens, or account identifiers in a public issue.
