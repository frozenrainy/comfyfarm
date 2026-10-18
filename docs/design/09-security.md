# 09 — Security

The design this project learned from ran in a closed network and trusted it. A public project cannot.

## Threats and defaults

| Threat | Default in ComfyFarm |
|---|---|
| Anyone reaching the router submits jobs or reads queues | **Implemented.** Identity required on every API and WS (D-28); the `dev` provider only on loopback; sessions are confirmed with the router, not trusted from the cookie (D-35) |
| Guessing another user's `clientId` to receive their previews | **Implemented.** A browser `clientId` is bound to the identity that opened it; events are relayed only to sockets of the job's owner |
| Users bypass the router and talk to a worker directly (stock `--multi-user` has no authentication: list users, log in as anyone, interrupt others' jobs) | **Implemented.** The worker extension refuses every request (HTTP and WebSocket, all routes) whose TCP peer is not loopback or in `COMFYFARM_ALLOW` (router and gateway addresses/CIDRs); the decision never reads a header. It reports its state on `GET /comfyfarm/info`; the router records it and the dashboard warns about workers that are open, have a malformed list, or lack the extension. A firewall rule is a fine second layer, but not a substitute: the farm cannot see it |
| A compromised router runs commands on workers via rollout | **Implemented.** The `comfy-upgrade/1` launcher validates every render — allowed binaries, options, path prefixes, env keys, env values (never paths — several allowed prefixes name plugin libraries, tool binaries or kernel caches — each key has a value format: `COMFYFARM_ALLOW` addresses/CIDRs, `TZ` a zone name, every other key a plain token; contract §4), user-writable directories (never overlapping `<ART>` or the code under `<LOCAL>`), and what the render *means* (D-37) — and refuses otherwise. Residual: a bare name in such a variable is still resolved through the worker's own search paths (system libraries), which the router cannot write to |
| A compromised router replaces the worker tools (and with them the launcher's checks) | **Implemented** (D-40). Worker setup installs the launcher, agent, host check, unit and instance configuration root-owned; the router's key reaches only the agent's forced command (`restart`, `status`, read-only `check`) in a root-owned `authorized_keys`. Upgrading the tools is a new, operator-approved setup run |
| Worker setup: a tampered script runs as root, or the router keeps root on workers | **Implemented** (D-41). Default path: the operator runs one line that checks the downloaded script against the checksum the dashboard shows (the token is one-time-ish: 2 hours, until finished; only its hash is stored). Optional path: the router logs in with an admin account the operator types in — password via askpass/stdin for that run only, never stored, logged or audited, masked in the run's log; the dashboard states that the router holds root for those minutes. Host keys come with the report and are pinned |
| Unauthenticated monitoring endpoint leaks data | **Implemented.** `/api/health/dispatch` and `/metrics` expose counts and seconds only — no users, paths, prompts |
| CSRF / cross-origin abuse | **Implemented.** No `CORS: *`; the gateway owns the cross-site check (D-30); session cookies `HttpOnly`, `SameSite=Lax`, `Secure` when TLS |
| Secrets in the repository | None committed; configuration via environment or a file outside the repo. *Planned:* CI secret scanning |
| Secrets in the ledger | **Implemented.** Comfy.org credentials are never stored (D-29); restart backends store only references (`key` path, `token_file` path or `token_env` name) — a token value is refused at the API (a ledger backup or replica must not carry it). The references are confined: files must lie in the keys directory (`COMFYFARM_KEYS_DIR`), `token_env` must be named `COMFYFARM_AGENT_TOKEN…` (the token is sent to the agent's URL, so any other file or variable would be too); ssh `user`/`host` are checked and passed so ssh never reads them as options |

## Things we deliberately do not do

- No arbitrary command execution features (no "run shell on worker").
- No storage of user passwords except the `local` provider (scrypt, standard library).
- No telemetry.
- No server-side logout of a single browser. *Log out* removes the cookie from that browser; a copy of the cookie
  stays valid until it expires (7 days) unless the account's sessions are ended — a password change, disabling the
  account, or deleting it end every session within `SESSION_TTL` (D-35), open WebSockets included.

## Worker allow-list — operation

- Set `COMFYFARM_ALLOW=<router>,<gateway>[,<cidr>…]` in the worker's environment (with the launcher: in the profile's
  `env`, which the launcher permits for the `COMFYFARM_` prefix — addresses and CIDR blocks only). Loopback is always allowed for local administration.
- Unset or malformed → the worker stays **open** and says so; the dashboard shows *unsecured*. A malformed list is not
  partially applied: a typo must not look like protection.
- Verified on ComfyUI 0.41.0: from a host outside the list `/system_stats`, `/api/userdata` and `/ws` answer 403
  (a forged `X-Forwarded-For` changes nothing); inside the list they answer as before.

## Reporting

`SECURITY.md` (to be added before the repository is public) gives a private reporting address.
