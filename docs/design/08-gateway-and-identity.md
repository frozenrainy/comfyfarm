# 08 — Gateway and identity

## Why a gateway

The stock frontend calls one origin (`location.host`) for everything. To keep frontend and ComfyUI unmodified, that
origin must be something that can split traffic:

| Path | Goes to | Why |
|---|---|---|
| `/ws`, `POST /api/prompt`, `/api/object_info`, `/api/jobs*`, `/api/queue`, `/api/history*`, `/api/interrupt` | router | the router owns jobs; it synthesises queue/history from the ledger for this user |
| `/api/users`, `/api/userdata*`, `/api/settings*`, `/`, `/assets`, `/extensions`, `/api/view` (input/output) | UI worker | user data and the screen must not freeze while a GPU worker loads a model |
| `/api/view?type=temp…` | the GPU worker that executed that job | temp files exist only on that worker |
| `/api/view?type=output…` | UI worker; on 404 the GPU worker that executed the job (found through the ledger, owner-scoped) | outputs normally sit on shared storage (01); without it the executing worker still has the file |
| everything else | UI worker | default |

Without a UI worker (topology A), user-data paths go to a designated GPU worker; the screen then freezes during model
loads on that worker — acceptable for a homelab, documented.

The `Comfy-User` header sent to any worker is always set by the gateway from the identity; a browser-supplied value is
discarded.

**Several UI workers sharing one user directory** (failover candidates) need care: stock ComfyUI reads `users.json` only
at start and rewrites the whole file from memory when a user is added, so users created on another UI worker get
deleted, unknown users produce 500 and the same name gets a second id. Sharing requires locking + merge-on-write +
change detection (the worker extension provides it); with a single UI worker this does not apply.

## Node definitions per group

`/object_info` snapshots are per group (D-20). The gateway answers `/api/object_info` with the snapshot of the user's
**current group** (chosen in the frontend extension, sent as a header, remembered per user). Switching groups makes the
extension reload node definitions (a page reload in the first version). Users who submit without a group see the
snapshot of the site's configured default group; ungrouped jobs are still dispatched by the group policy (D-03), so the
default group should be the most complete one.

## Identity providers

Interface: `identify(request) -> Identity{user, roles}` or reject. Roles matter as much as the user: rollout, profiles,
restart and worker administration are admin-only; dropping roles when replacing a corporate SSO would leave those
operations open.

| Provider | Use |
|---|---|
| `trusted-header` | behind an authenticating reverse proxy (`X-Forwarded-User`, roles from a header or a mapping file); the gateway must only accept connections from the proxy **and** must strip any identity header the client sent itself before applying the proxy's value |
| `oidc` | direct login (authorization code flow, session cookie) |
| `local` | users in the database with scrypt password hashes (stdlib, no extra dependency); small sites |
| `dev-none` | single user `dev`, refuses to start unless bound to 127.0.0.1 |

## Accounts and permissions

Every identity has a row in `users` — local accounts with a scrypt password, external ones (trusted-header, later OIDC)
created on first sight without a password. Admins manage accounts in the dashboard (13-web-ui.md).

**Login page.** Served by the gateway (it must work before any session), styled like the dashboard, landing on the
dashboard (`next` accepts only `/` and the dashboard: a free target would be an open redirect). An admin setting can
offer the active local user names as a drop-down; the gateway asks the router for them (`/api/internal/login-options`,
shared secret) and falls back to a text field when the router does not answer.

**Group permissions (D-34).** A user may use the worker groups granted to them (`user_groups`, any number); admins may
use all. Submitting to another group is refused (403, stock error shape); a user with no group cannot submit. A job
carries `allowed_groups` — the owner's permissions, kept in step: a grant or revocation re-stamps the owner's queued
jobs in the same transaction, so a revoked group stops taking them at once. The dispatcher and the monitoring numbers
apply that column; neither needs to know about users. Ungrouped jobs run only in groups the owner may use.

**Sessions (D-35).** The cookie carries the user and the account's `session_epoch`, not roles. The gateway asks the
router (`/api/internal/session`, shared secret) whether the account is still active, the epoch still current, and which
roles it has now; the answer is cached for 30 s per session. So:

| Change | Takes effect |
|---|---|
| password changed (by the user or an admin), account disabled | epoch bumped → every session of the account ends within 30 s |
| admin role granted or removed | the next confirmation (≤ 30 s) carries the new roles |
| group permissions changed | at once (router-side, no session involved) |
| router cannot confirm a session | gateway answers 503 — an unconfirmed session is never let through |

The epoch is a counter, not a timestamp, so gateway and database clocks need not agree. Lock-out guards: nobody
removes their own admin role, disables or deletes themselves, and the last active admin cannot be demoted, disabled or
deleted.

## Submit path through the gateway

1. Identify the user; reject unauthenticated requests (D-28).
2. The frontend extension generates a UUID per click (idempotency key = `prompt_id`, D-23) and retries on 503 or
   connection errors with 1, 2, 3, 4, 5, 5… s spacing, blocking new clicks meanwhile; each attempt has a 20 s timeout.
3. Pre-submit validation on a GPU worker of the target group (D-24); a 400 returns `node_errors` in the stock shape so
   the stock frontend highlights the broken nodes.
4. Insert; respond in the stock `/prompt` response shape (`prompt_id`, `number`, `node_errors`).

## Static assets

Stock ComfyUI sends `no-store` even for hashed assets, so every reload transfers ~25 MB. The gateway serves hashed
assets with `immutable` caching and gzip (measured elsewhere: 6.1 MB first load, 0.6 MB afterwards).

## Streaming

Uploads and large responses are streamed. Upstream connections of a streamed response are released in the generator's
`finally`, not in a background task (background tasks do not run when the client disconnects → pool leak).
