# 19 — Command line, API tokens and holding dispatch

People and programs (scripts, AI agents) should be able to run the farm from a terminal and use the same operations as
the dashboard, with the same permission checks and the same audit trail. This document covers three things that make
that possible: holding dispatch for the whole farm, personal API tokens, and the `comfyfarm-ctl` command.

The `comfyfarm` command that already exists is a different tool. It runs on the control-plane host and talks to Postgres
directly (`add-user`, `backup`, `doctor`, …). That is right for installing and repairing the farm. It is wrong for running
it: it bypasses the API's checks, needs the database's address and password, and cannot run from an artist's machine or
an agent's sandbox.

## Holding dispatch (D-56)

"Pause everything" means **the farm stops handing jobs to workers**:

- Jobs already on a worker run to the end. ComfyUI cannot pause an execution; stopping one means cancelling it.
- New jobs are still accepted and wait in the queue as `pending`.
- Per-job states are not touched. Releasing the hold starts dispatch again.

Holding every job one by one (`pending → paused` for all) was rejected. "Resume everything" would also resume the jobs
people had paused on purpose, and the two meanings of `paused` could not be told apart afterwards.

The hold is one row in `settings` (key `dispatch_hold`) with `since`, `by`, `reason` and an optional `until`. Both the
dispatcher's tick and its claim transaction (`LOCK_WORKER`) check the row, so every router of a farm (HA) sees the same
hold. A claim that was already under way when the hold was set may still go out. The API reports the jobs that are
running, and `comfyfarm-ctl hold --wait` waits for them to finish.

`until` makes a hold end by itself. A forgotten hold must not silently keep a farm idle. Once `until` has passed, the
dispatcher deletes the row (one router wins the `DELETE … RETURNING`) and writes `dispatch hold expired` to the audit log.

What shows the hold:

- **Dashboard:** every user sees a banner on every page with who held dispatch, since when and why. Administrators get
  a Release button there; holding is under System → Settings.
- **Monitoring:** `dispatch_held_since` is a timestamp, not a boolean flag, for the reason given for
  `rollout_started_at` in [06](06-health-and-monitoring.md). It is exported as
  `comfyfarm_dispatch_held_timestamp_seconds` (no labels; absent when there is no hold). Alert rules suppress the stall
  alert on it and can alert on a hold that has lasted too long. The dashboard's group cards show "held" in place of the
  stall time.

API:

| Method | Path | Who | |
|---|---|---|---|
| `GET` | `/api/farm/hold` | any user | `{held, since, by, reason, until, running, pending}` |
| `PUT` | `/api/admin/hold` | admin | `{held: true, reason?, minutes?}` holds (again: reason and `until` change, `since` stays) · `{held: false}` releases |

## API tokens (D-57)

A token belongs to a user and acts as that user. It gets **at most** that user's rights, read when it is used, so
disabling a user or taking away their admin role reaches every token they have.

| Scope | Allows |
|---|---|
| `read` | `GET`/`HEAD` only, with the user's rights |
| `user` | everything the user may do, **without** the admin role |
| `admin` | everything, admin included. Only an admin can create one, and it stops working if they stop being an admin |

How it works:

- **Format:** `cft_` followed by 43 random URL-safe characters (32 bytes). Only the SHA-256 is stored, together with a
  short hint (the first 10 characters) so a person can recognise the token in a list. The token itself is shown once,
  when it is created.
- **Checking:** the gateway reads `Authorization: Bearer cft_…` and asks the router (`POST /api/internal/token`,
  gateway-only) who it belongs to. The answer is cached for 30 s, like a session (D-35), so a revoked token stops within
  30 s. The gateway passes `x-comfyfarm-token: <id>:<name>` to the router, which keeps the header only when the gateway
  vouched for the request (`identity.py`) and records it in every audit entry made by that request.
- **Reach:** a token reaches only the farm API (the router's paths). It cannot reach the UI worker (ComfyUI's own
  routes, user files) or the WebSocket.
- **Limits:** a token cannot create or revoke tokens, so a leaked token cannot make more of itself. Each user has at
  most 20 live tokens. Expiry is 1–365 days; the default is 90 and there is no "never".
- **Passwords:** changing a password does not end a user's tokens. A token is a separate credential with its own expiry
  and revocation, as on code hosting sites; the settings page says so.
- **Managing:** a user creates, lists and revokes their own tokens under Settings → API tokens
  (`/api/farm/me/tokens`). An administrator sees and revokes everyone's under System → Users (`/api/admin/tokens`).

Not in this version: tokens on the trusted-header provider's own path (they work with every provider, since the gateway
checks them itself), scoping a token to one group, and rate limits.

## `comfyfarm-ctl` (D-58)

A client of the HTTP API. It is one module that uses only the Python standard library (3.10+), so it runs on any machine
and in any agent sandbox without installing the package. It is installed with the package as the `comfyfarm-ctl`
command, and the file can also be copied and run on its own with `python3 ctl.py`.

**Connection.** The farm is given by `--url` or `COMFYFARM_URL`, the token by `COMFYFARM_TOKEN`. `comfyfarm-ctl login`
saves both in `~/.config/comfyfarm/ctl.json` with mode 0600; the token is read from stdin, never taken as an argument
(which would leave it in shell history and process lists).

**For programs:**

- `--json` prints the API's answer as it came, one JSON document per command.
- Exit codes: `0` ok · `1` refused or failed (400/422) · `2` usage · `3` not authenticated or not allowed (401/403) ·
  `4` conflict (409) · `5` not found (404) · `6` farm unavailable (5xx, connection).
- `comfyfarm-ctl commands --json` lists every command with its arguments, so an agent can discover the tool without
  reading the documentation.
- Destructive commands (cancel) ask for confirmation on a terminal and require `--yes` otherwise.

**Commands:**

| Command | API |
|---|---|
| `login`, `logout`, `whoami` | `/api/farm/me` |
| `status` | hold, own queue, farm totals |
| `hold [--reason R] [--for 2h] [--wait]`, `release`, `hold-status` | `/api/admin/hold`, `/api/farm/hold` |
| `jobs list [--all] [--status …] [--history]`, `jobs show ID` | `/api/farm/queue`, `/api/farm/history`, `/api/farm/jobs/{id}` |
| `jobs pause|resume|cancel|retry ID…`, `jobs move ID --top|--bottom|--before X|--after X` | the per-job routes |
| `workers list|show`, `workers maintenance on|off W…`, `workers enable|disable W…` | `/api/admin/workers` |
| `workers reset W` (shows the preview, then asks) | `/api/admin/workers/{id}/force-reset` |
| `profiles list` | `/api/admin/profiles` |
| `rollouts list|show`, `rollouts start --profile P (--worker W… | --group G)`, `rollouts abort|resume ID` | `/api/admin/rollouts` |
| `builds recipes|list|show`, `builds start RECIPE [--roles] [--no-packs]` | `/api/admin/builder`, `/api/admin/builds` |
| `packs list|add|update|remove` (add/update ask the builder which commit a branch or tag names now) | `/api/admin/node-packs` |
| `commands` | — |

Workers, profiles, groups and node packs are named by name or id. `--wait` (rollouts, builds, `hold`) follows the long
operations to the end and sets the exit code from how they ended: a rollout that did not succeed (or, for `abort`, was
not aborted), a failed build or a hold whose jobs did not drain in time exits 1. Starting a rollout, aborting one, a
force reset and removing a node pack ask first (`--yes` off a terminal).

**Later:** users, groups, models and uploads, settings, profiles from a recipe. Every command maps to an existing route,
so the CLI never gets a rule the dashboard does not have.

## Audit (D-59)

An agent acting with a token leaves a trace like a person does. Every change to accounts, groups, workers, the hold and
queue order is audited, and so is every pause, resume, retry and farm-API cancel of a job. (Cancelling from ComfyUI's own
queue panel, the stock `/interrupt` and `/queue` routes, is not audited yet.) All of it goes through
`rollout.engine.audit`, which adds the token when the request came with one.
