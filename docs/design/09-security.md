# 09 — Security

The design this project learned from ran in a closed network and trusted it. A public project cannot.

## Threats and defaults

| Threat | Default in ComfyFarm |
|---|---|
| Anyone reaching the router submits jobs or reads queues | Identity required on every API and WS (D-28); `dev-none` provider only on loopback |
| Guessing another user's `clientId` to receive their previews | A browser `clientId` is bound to the identity that opened it; events are relayed only to sockets of the job's owner |
| Users bypass the router and talk to a worker directly (stock `--multi-user` has no authentication: list users, log in as anyone, interrupt others' jobs) | Workers accept only router/gateway addresses: firewall rule **or** the worker extension's source allow-list. Setup refuses to mark a worker "secured" until one is configured; the dashboard shows unsecured workers |
| A compromised router runs commands on workers via rollout | The `comfy-upgrade/1` launcher validates every render (allowed binaries, options, path prefixes, env keys) and refuses otherwise |
| Unauthenticated monitoring endpoint leaks data | It exposes counts and seconds only — no users, paths, prompts |
| CSRF / cross-origin abuse | No `CORS: *`; same-origin through the gateway; session cookies `SameSite=Lax`, `Secure` when TLS |
| Secrets in the repository | None committed; configuration via environment or a file outside the repo; CI secret scanning |

## Things we deliberately do not do

- No arbitrary command execution features (no "run shell on worker").
- No storage of user passwords except the `local` provider (bcrypt).
- No telemetry.

## Reporting

`SECURITY.md` (to be added before the repository is public) gives a private reporting address.
