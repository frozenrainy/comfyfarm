# Decision log

Each entry: **decision** — why — consequence for the code. Entries marked *(prod)* come from incidents in a production
farm of about ten GPU workers whose operators shared their design rationale with this project. Entries marked *(probe)* were verified against stock ComfyUI
with `spikes/protocol_probe.py`. Entries marked *(new)* are choices made for this rewrite.

## Queue and dispatch

**D-01 Payload and state live in different tables.** *(prod)* — A dispatch query sorted rows that carried a 416 KB
workflow; MySQL rejects the whole sort when one row exceeds the sort buffer, and one group stopped dispatching entirely
while workers sat idle. Postgres would not fail, it would silently spill to disk and get slow. → `job_payloads` holds the
workflow; every hot query (dispatch, health, progress) touches only `jobs`.

**D-02 Dispatch claims rows with `FOR UPDATE SKIP LOCKED` inside one transaction.** *(new, replaces a prod workaround)* —
The old design consumed an "idle workers" snapshot built by another loop, which could be stale; a worker that had just
been put into maintenance received a new job and the rolling restart killed it. → Lock the worker row and the job row,
re-check `maintenance` and `status` under the lock, update both, commit, then POST. Contention is decided by the
database, not by timing.

**D-03 Group matching is explicit and the metrics use the same rule.** *(prod)* — Jobs without a group were only picked
after every group's backlog was empty; under saturation one waited 5.5 hours. Ungrouped workers silently receive
nothing (QA lost four rounds to this). → The policy for ungrouped jobs is a setting (`lowest_priority` |
`least_loaded_group`), the dispatcher and the monitoring query share one SQL function, and a worker without a group is
reported as a configuration warning, not hidden.

**D-04 `running` and the router-chosen `prompt_id` are committed in the same statement, before the POST.** *(prod)* — If
the router dies between committing `running` and POSTing, a row without an identifier cannot be checked later; an old
fallback ("worker idle ⇒ completed") marked a never-executed job as completed. → The identifier exists before the job
leaves the router. This trades *duplicate execution* risk for *lost job* risk, which is detectable (D-06).

**D-05 A timeout is not "not delivered".** *(prod, probe)* — A job marked failed after a read timeout ran for 16 minutes
on the worker and produced output; the user resubmitted it three times. ComfyUI accepts a client-chosen `prompt_id`
*(probe S1)*, so after any ambiguous error the router asks the worker (`/api/jobs/{id}`, `/queue`) instead of guessing.
**Resubmitting the same `prompt_id` is accepted and executes again** *(probe S1d)* — the worker does not deduplicate, so
"just retry" is never safe. → Branches: connect error ⇒ back to pending; read error ⇒ ask: accepted ⇒ running,
absent ⇒ pending, cannot tell ⇒ stay `dispatching` (the lost-job check resolves it). **"Absent" needs settling time:**
when the connection breaks quickly (reset right after sending), the worker may still be parsing the body and answer
"absent"; re-queueing then runs the job twice (P3). "Absent" counts only if seen twice, the second time at least 5 s
after the POST ended; before that it is "cannot tell". (The old system asked only after a 10 s timeout, so this window
never opened.)

**D-06 A lost job becomes `failed` with a reason, never `pending`.** *(prod)* — If the job is what crashed the worker,
re-queueing it would walk through the farm crashing workers. → After a grace period (dispatch timeout + check period +
margin, default 30 s) a running job the worker does not know is failed with "lost — rerun if needed".

**D-06b A job whose worker is gone is failed too.** *(prod)* — The lost-job judgement needs an answer from the worker; a
worker that is permanently dead, deleted or disabled never answers, and its jobs would stay `running` forever. → If the
worker has been offline longer than N minutes (default 10), or is deleted/disabled, its `dispatching|running|cancelling`
jobs fail with kind `worker_lost`.

**D-07 4xx from a worker is final.** *(prod, probe)* — A 400 is a workflow validation error and is identical on every
worker; returning it to pending produced an infinite re-dispatch loop every few seconds (the worker stayed healthy, so
nothing stopped it). Uppercase UUIDs are rejected with 400 `invalid_prompt_id` *(probe S1c)* — the router must emit
canonical lowercase. → 4xx ⇒ `failed` with the worker's `node_errors` attached.

**D-08 Completion is read from the worker, WS events are an accelerator.** *(prod, new)* — WebSockets drop and lag.
`/api/jobs/{id}` exists in current ComfyUI and reports `pending|in_progress|completed|failed|cancelled` *(probe S2, S6)*.
→ `execution_success` / `execution_error` / `execution_interrupted` events finish a job immediately; a periodic
reconciliation against `/api/jobs` is the source of truth. Interrupted jobs are `cancelled`, not `completed` (this was an
open question in the old design; history shows `status_str: "error"` + an `execution_interrupted` message *(probe S6d)*).

**D-09 Interrupt is cooperative; cancellation is a state, not an event.** *(probe)* — `/interrupt` with a `prompt_id`
only affects that job *(probe S6c)*, but it takes effect between nodes; a single long node kept running 78–260 s after the request.
→ `cancelling` state until the worker reports the outcome; the worker stays busy meanwhile.

**D-10 Display paths: estimate at submit, correct from the worker at completion.** *(prod)* — Users build output paths
from string nodes, so static extraction stored node links like `["73",0]` for most rows. → Static resolution walks links;
completion overwrites with real outputs, merging path components in both directions (history gives the parent folder,
the submitted path may carry a more specific leaf). Columns are `text`, never length-limited (a strict length overflow
once rejected the whole submit).

## Worker health

**D-11 busy ≠ dead, judged by elapsed time, not by counts.** *(prod)* — Large model loads block a worker's event loop for
minutes (226 s measured for a 33 GB model). "Two failed checks = offline" marked healthy workers dead; counting checks
also made the threshold depend on loop timing. → Read/write/pool timeouts and read errors = busy (the TCP connection
succeeded); connect errors = dead; unknown exceptions = busy. Thresholds: dead after 15 s, busy-but-unresponsive after
90 s, measured from the first failure; the first-failure timestamp is cleared on success.

**D-12 Health checks are concurrent; slow collectors run in their own loops.** *(prod)* — A sequential check loop let one
hung worker stretch the cycle past 12 s, which made other decisions stale. → `gather` the probes, process results
serially, isolate optional collectors (VRAM, logs).

**D-13 The router's worker WebSocket uses a deterministic `clientId`.** *(prod)* — With a random id kept in memory, a
router restart froze the progress of every running job forever (the worker sends events to the id the job was
submitted with). → `client_id = uuid5(router_instance, worker_id)`. This survives restarts; it does **not** create the
reconnect race of D-14 — that race happens with *any* id scheme whenever the same id reconnects, including a reconnect
inside one router process after a ping timeout. Without the worker fix there is no id scheme that is both race-free and
restart-safe; the only mitigation is a long ping timeout so reconnects are rare.

**D-14 Fix the reconnect race at the worker, and upstream — it is needed whatever the id scheme.** *(prod, probe)* — ComfyUI's WS handler pops `sockets[sid]`
in `finally` without checking it is still the same socket; the old connection's cleanup deletes the new registration
and every later event for that client is dropped without an error. Reproduced on 0.41.0: the second connection received
0 job events while a single-connection control received them *(probe S3/S3b)*. → (1) a one-line upstream PR
(`if self.sockets.get(sid) is ws:` around **both** pops); (2) until it lands, the optional worker extension swaps the
`/ws` route handler for a guarded copy before routes are registered; (3) the router keeps a 300 s ping timeout so a
loop-blocked worker does not drop the socket in the first place. Never open a diagnostic WebSocket with the router's
`clientId` — it steals the router's registration.

## Events

**D-15 Progress writes are throttled and bounded.** *(prod)* — One UPDATE per progress event exhausted a 10-connection
pool; `acquire()` had no timeout, so WS loops stopped reading and went quiet. → Per-worker 2 s throttle, write only on
change, pool acquire timeout (5 s) everywhere, and a failed progress write never breaks the WS loop.

**D-16 Relay targets are captured when the event is received.** *(prod)* — Relaying in a background task that looked up
the target later lost every `execution_error`: the error branch removed the mapping first. → Tasks receive values, not
lookups; per-browser-socket lock keeps text/binary order.

**D-17 In-memory state is a cache that can be rebuilt from the ledger.** *(prod)* — After a failover or restart the
worker→browser mapping was empty and events for running jobs were dropped. → On a miss, rebuild from
`jobs.browser_client_id`; also verify on a hit when no browser socket exists (stale mapping).

**D-18 The router broadcasts queue `status`.** *(prod)* — The frontend refreshes its queue UI only on `status` events;
without them a submitted job was invisible until reload. → Push on submit/dispatch/finish/cancel.

**D-19 Reconnect catch-up.** *(prod)* — Events emitted during a reconnect gap are lost; the browser shows "running"
forever. → On browser reconnect, resend final events for that client's jobs that finished in the last 60 s, plus jobs
the ledger still has as running but the worker reports finished.

## Node definitions

**D-20 `/object_info` snapshot without TTL, persisted, stale-if-error.** *(prod)* — It is synchronous (26 s measured on a
heavily customised worker; 0.1 s on a bare one *(probe S5)*). A 10-minute TTL made one worker unresponsive for about an
hour per day, and refreshing in the middle of a rolling upgrade served a mixed-version snapshot. → Refresh manually,
after a rollout completes, or when a group's node set changes; keep the snapshot per group in Postgres; never replace a
good snapshot with an error; show freshness in the UI.

## Submission

**D-21 Bounded waits everywhere: pool acquire 5 s ⇒ 503 + Retry-After.** *(prod)* — Unbounded waits turned congestion into
infinite spinners. `asyncpg`'s `pool.acquire()` has no timeout by default (the same shape as the original incident with
another driver) → one wrapper (`db.conn()`) is the only way to get a connection and always passes `timeout=5`.

**D-22 Unknown group ids degrade, they do not reject.** *(prod)* — A deleted group id cached in a browser made every
submit fail with an FK error for that user. → Treat as "no group" and say so in the response.

**D-23 Idempotency key chosen by the client, not content comparison.** *(prod)* — A fixed seed with batch N legitimately
submits identical bodies N times. → One UUID per click, retried on 503/connection errors with the same key; the server
returns the existing job for (user, key). The key doubles as the `prompt_id`, which is globally unique: a key that
belongs to **another user's** job is answered with 409 (no 500, and no hint whether it exists).

**D-24 Validate before queueing, on a worker of the target group.** *(prod)* — Without it, a broken workflow is accepted,
waits in the queue, and fails at dispatch time with no node highlighted. A CPU-only UI worker cannot validate because
some nodes only import with a GPU. → Ask up to 3 idle workers of the target group, 4 s each, skip validation if none
answers (the worker validates again at dispatch).

## Platform

**D-25 Postgres, UTC, dedicated timestamps.** *(new)* — The previous system leaned on MySQL quirks (same-value UPDATE does
not touch `updated_at`, so `updated_at` silently meant "completion time"), and two clocks (DB session TZ vs host TZ) made
timestamps jump by the timezone offset at failover. → `timestamptz` everywhere, UTC, `created_at/dispatched_at/started_at/finished_at` columns with one
meaning each; `updated_at` is for humans only.

**D-26 Single node by default; HA is a module.** *(new)* — Small sites do not need two database nodes. The core asks one
question, "may I write?", through an interface; the HA module answers it (Postgres `pg_is_in_recovery()`).
**HA requirements (not optional for no-duplicate guarantees):** zero duplicates in the original system came from the
pair "commit `running` before dispatch" (D-04) **and** semi-synchronous replication — the standby had the row before the
router saw the commit. With asynchronous streaming replication a freshly promoted primary can miss a `dispatching` row,
see the job as pending and dispatch it again, and P3 makes the worker run it twice. The HA module therefore requires
`synchronous_commit = on` (or `remote_apply`) with `synchronous_standby_names`, plus **fencing** so the old primary's
router cannot write (the original system checked for a virtual-IP conflict with `arping -D` right before promotion).
**Complement (not a substitute):** because `prompt_id` is fixed at submit time, a newly promoted node first asks the
workers (`/api/jobs/{id}`) about pending jobs from the failover window before re-dispatching them, and skips any a worker
already knows. This cannot catch a worker that lost its records in a restart, so synchronous replication stays required.

**D-27 Schema before code; migrations are automatic and idempotent.** *(prod)* — Deploying code that referenced a table
before the table existed broke the whole dashboard. → Versioned migrations applied at start under an advisory lock.

**D-28 Authentication is on by default.** *(new, prod debt)* — The old system trusted its closed network: browser `/ws`
and submit were unauthenticated, so a guessed `clientId` could read someone else's previews, and stock
`--multi-user` ComfyUI has no authentication. → Identity is required, a browser's `clientId` is bound to its identity,
workers accept only router/gateway traffic ([09](09-security.md)).

**D-29 Credentials in `extra_data` are forwarded, never stored.** *(new)* — The stock frontend sends Comfy.org API
credentials (`auth_token_comfy_org`, `api_key_comfy_org`) inside `extra_data` with every prompt. Persisting payloads
would put them in the ledger, its backups and its replicas. → The router strips them at submit, keeps them in memory
keyed by job, and adds them back only in the POST to the worker. A router restart before dispatch drops them; API nodes
in that job then fail with an authentication error instead of credentials lingering in a database.

**D-30 The gateway owns the cross-site check.** *(new, probe)* — Stock ComfyUI refuses a request whose `Origin` host differs
from its own `Host` on loopback, and any `Sec-Fetch-Site: cross-site` fetch. Behind a proxy the browser's `Origin` is the
gateway, so every proxied asset request got 403 and the frontend never started (seen in the first browser E2E). → The
gateway refuses cross-site fetches and foreign origins itself, then forwards without `Origin`/`Sec-Fetch-*`. The CSRF
protection moves to the boundary instead of being lost.

**D-31 Outputs are recorded from `executed` before the event is relayed.** *(new, probe)* — The stock frontend requests
`/api/view` the moment it sees `executed`; the gateway finds the worker holding a temp file through the ledger. Recording
outputs only at completion (reconcile) made the first preview 404 in the browser E2E. → The event hub writes the node's
outputs, then relays the event.

**D-32 Dashboard: no framework, no build step; reordering is admin-only.** *(new)* — A farm may sit on a network without
a package registry, and a dashboard that needs `npm install` to be patched is a dashboard nobody patches. The page is
one HTML, one CSS and one JS module served by the router; it polls one overview document and updates table rows in
place by key (re-rendering the table wiped a note being typed and moved the row under the pointer — seen in the browser
E2E). The queue order is one shared order, so "run mine next" moves everyone else's work back: users may pause, resume,
cancel, retry and annotate their own jobs; only admins reorder. Users see their own jobs with their place in line;
other users' jobs appear only as counts.

**D-33 Monitoring numbers follow the dispatcher, not the table.** *(new)* — The first `/api/health/dispatch` counted
"pending jobs of the group": a job no worker could ever take (wrong requirements) then read as a dispatch stall, a
resumed week-old job read as a week of stall, and ungrouped jobs — which every group takes — counted nowhere. →
`pending` is what a worker of the group can take; the stall clock runs only while an *idle* worker could take a waiting
job, from when the job last became waiting (`job_events`), bounded by the group's last dispatch; jobs no worker meets
are reported separately (`unsatisfiable_pending`, `ungrouped_unsatisfiable_pending`). `/metrics` is the same computation
in Prometheus text; a ledger it cannot read is `comfyfarm_ledger_up 0`, never a page of missing-but-healthy-looking
numbers.
