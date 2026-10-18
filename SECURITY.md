# Security policy

## Reporting a vulnerability

Please report security problems **privately** through GitHub's *Report a vulnerability* button on this repository's
Security tab (private vulnerability reporting). Do not open a public issue for them.

Include what an attacker needs (network position, account, role), what they gain, and steps to reproduce. You should
get an answer within a week. Fixes are released before details are published; reporters are credited unless they
prefer otherwise.

## Scope

In scope: the router, the gateway, the dashboard, the worker-side tools (`launcher.py`, `agent.py`, `publish.py`),
and the two ComfyUI extensions in `worker_ext/` and `ui_ext/`.

Out of scope: vulnerabilities in ComfyUI itself, its frontend or custom nodes (report those upstream), and setups
that ignore the documented requirements — in particular workers without `COMFYFARM_ALLOW` (the dashboard flags them as
*unsecured*), or the router port exposed to users.

## What ComfyFarm defends against

The threat model is in [docs/design/09-security.md](docs/design/09-security.md): identity on every API and WebSocket,
sessions confirmed against the account on every request window, browser `clientId`s bound to their owner, workers that
accept only the farm's own addresses, a launcher that refuses any render that is not stock ComfyUI from published
artifacts, no secrets in the ledger. ComfyFarm is pre-release: there are no supported versions yet beyond the latest
commit.
