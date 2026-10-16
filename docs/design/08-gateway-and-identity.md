# 08 — Gateway and identity

## Why a gateway

The stock frontend calls one origin (`location.host`) for everything. To keep frontend and ComfyUI unmodified, that
origin must be something that can split traffic:

| Path | Goes to | Why |
|---|---|---|
| `/ws`, `POST /api/prompt`, `/api/object_info`, `/api/jobs*`, `/api/queue`, `/api/history*`, `/api/interrupt` | router | the router owns jobs; it synthesises queue/history from the ledger for this user |
| `/api/users`, `/api/userdata*`, `/api/settings*`, `/`, `/assets`, `/extensions`, `/api/view` (input/output) | UI worker | user data and the screen must not freeze while a GPU worker loads a model |
| `/api/view?type=temp…` | the GPU worker that executed that job | temp files exist only on that worker |
| everything else | UI worker | default |

Without a UI worker (topology A), user-data paths go to a designated GPU worker; the screen then freezes during model
loads on that worker — acceptable for a homelab, documented.

## Identity providers

Interface: `identify(request) -> Identity{user, roles}` or reject. Roles matter as much as the user: rollout, profiles,
restart and worker administration are admin-only; dropping roles when replacing a corporate SSO would leave those
operations open.

| Provider | Use |
|---|---|
| `trusted-header` | behind an authenticating reverse proxy (`X-Forwarded-User`, roles from a header or a mapping file); the gateway must only accept connections from the proxy |
| `oidc` | direct login (authorization code flow, session cookie) |
| `local` | users in the database with bcrypt passwords; small sites |
| `dev-none` | single user `dev`, refuses to start unless bound to 127.0.0.1 |

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
