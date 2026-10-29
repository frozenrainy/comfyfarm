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

**D-34 Group permissions are stamped on the job.** *(new)* — Users may use only the worker groups granted to them.
Checking the owner's permissions in the dispatcher would tie the hot path to the user model (and to identity providers
that have no user rows). → Every job carries `allowed_groups` (NULL = any); submit stamps it, a permission change
re-stamps the owner's queued jobs in the same transaction, and the dispatcher, `/api/health/dispatch` and the
dashboard's *blocked* mark all read that one column. A job nobody may take is reported as unsatisfiable, not as a
dispatch stall.

**D-35 Sessions are confirmed, not trusted.** *(new)* — A signed cookie that carried roles kept a demoted or disabled
user an admin until it expired (7 days). → The cookie carries the user and a per-account `session_epoch`; the gateway
confirms it with the router (active? epoch current? which roles now?) and caches the answer 30 s. Password change and
disabling bump the epoch, ending every session of the account. A counter rather than a timestamp, so clocks need not
agree. If the router cannot confirm, the gateway answers 503 instead of letting the request through.

**D-36 A profile in use is immutable.** *(new)* — Editing a profile that workers are assigned to would change what
they run at their next restart — any restart, not a controlled one — and the badge would flip to `differs` on every
worker at once. → Profiles assigned to an instance or part of an active rollout (its target, or the profile a step reverts to) accept only note changes; a change
is a copy rolled out like any other (drained, verified, revertible). Unused profiles stay editable.

**D-37 The launcher validates what the render *means*, not only its shape.** *(new)* — An allow-list of ComfyUI
options is not enough: `--extra-model-paths-config` can point `custom_nodes` at the input directory (whatever a user
uploaded would be imported as code), `--front-end-root` at any directory (served to every browser), and path options
into the artifact store (which must stay read-only). → The launcher accepts only the render's own yaml and only with
exactly the synced node artifacts as content, a frontend only as a published artifact, no directory inside `<ART>`,
the core in argv only if it is the one synced, and every READY the render depends on. The router runs the same
validator (minus READY) before any assignment, so a render the launcher would refuse is refused at the door.

**D-38 An interrupted rollout waits for a human.** *(new)* — After a router restart, resuming a rollout on its own
would restart workers nobody is watching any more, and a step caught mid-restart has an unknown outcome. → The engine
cancels its step tasks with itself (they were left running unattended), marks the rollout `interrupted` at the next
start and leaves its workers in maintenance. *Resume* re-runs unfinished steps from the top (a worker already on the
target is not restarted again); *Abort* returns draining workers to service and marks workers caught mid-restart
failed for a person to check.

**D-39 Two licences: Apache-2.0 for ComfyFarm, GPL-3.0 for what runs inside ComfyUI.** *(review)* — ComfyUI and
its frontend are GPL-3.0. The worker extension runs in the ComfyUI process and carries a modified copy of its
WebSocket handler; the UI extension extends the GPL frontend. Publishing them under Apache-2.0 would invite a licence
violation. → `worker_ext/` and `ui_ext/` are GPL-3.0-or-later (LICENSE + SPDX headers in each, attribution for the
derived handler); the router, gateway, dashboard and the worker-side tools under `src/` stay Apache-2.0 — they only
speak HTTP/WebSocket to ComfyUI and never import the extensions. (Risk assessment by review, not legal advice.)

**D-40 Worker tools are installed by root during setup; the router's everyday key cannot replace them.** *(new)* —
A router that could rewrite `launcher.py` on a worker could make it accept anything, which would empty D-37. → Worker
setup (docs/design/14) installs the launcher, agent, host check, unit and instance configuration as root-owned files
the service account cannot change, and puts the router's key into a root-owned `authorized_keys` with a forced command
that allows `restart`, `status` and the read-only `check` only. Upgrading the worker tools is a new setup run.

**D-41 How setup gets root is the operator's choice, shown with its cost.** *(new, operator decision)* — Setup needs
root for minutes, once per host. Running a one-line command per host keeps root away from the router entirely; letting
the router log in with an admin account is faster for many hosts but gives the router root for those minutes. → The
dashboard offers both, side by side with what each means. The command path checks the downloaded script against the
checksum the dashboard shows; the ssh path takes the password for that run only (askpass for ssh, stdin for sudo —
never a command line, the ledger, a log or the audit trail). Both run the same script and report the same way; the host
keys come with the report and are pinned (no trust on first use afterwards).

**D-42 A published venv carries its interpreter.** *(new, found preparing a second host)* — `uv venv` links
`bin/python` to uv's interpreter store in the builder's home: the published venv ran only on the build host, against
contract §2 ("references nothing outside itself"). → Publishing copies the base interpreter (a relocatable
python-build-standalone, as uv installs it) into `<venv>/.python/` — or uses the operating system's own Python under
`/usr` — and refuses a venv whose `bin/python` or `pyvenv.cfg` `home` lies anywhere else. The host check runs every
venv's interpreter on each host.

**D-43 A job that waited in a worker's queue and never ran goes back in line when the worker forgets it.** *(field,
dev.21)* — D-06 fails a running job the worker no longer knows, because the job may be what crashed it. A job ComfyUI
had only queued — never started: no `execution_start`, never seen in progress (`started_at` empty) — cannot have
crashed anything, and failing it made a user rerun a job that never ran (a GPU fault killed the executor; the next job
sat in the dead process's queue until the restart, then was "lost"). → After the same grace, such a job returns to
`pending` ("the worker restarted before running it"). One that started stays D-06. The residual risk: a job that ran
start to end while the router heard nothing (event stream down, reconcile between polls) and then lost its record —
it runs a second time.

**D-44 A job failed by a GPU fault is queued again, once.** *(field, dev.22)* — On an R9700 under ROCm 7.2 the GPU's
copy engine faulted three times in two days ("illegal memory access", a `MAPPING_ERROR` page fault on a host address)
while ComfyUI moved model weights between RAM and VRAM — at a part boundary of a long video, or as a job started after
hours idle; never in a sampler step. The workflow is not what fails, and the process's GPU context is lost for good
(the watchdog restarts it, 0.10). D-06 keeps a job that broke its worker out of the queue; this one would run fine on
a fresh process. → The reconciler copies a job whose error names that fault (`core.jobs.GPU_FAULT`) into a new job at
the front of the queue (`retry_of`, reason "automatic retry of job N after a GPU fault"), at most
`COMFYFARM_RETRY_GPU_FAULTS` times (default 1) along one chain of automatic retries, only within 10 minutes of the
failure, and never when it was retried already. A retry that faults again stops the chain: a second fault in a row
is more likely the job's own than bad luck.

**D-45 A rollout goes back to what the worker ran, not to what it was assigned.** *(field, dev.23)* — A new profile
was assigned (no restart), then rolled out; its node pack failed to import. The step had taken the assignment as
where the worker came from, so the revert restarted the worker into the same broken profile, and the badge said
`match` — the worker did run what it was assigned. → At rollout start each step's `from` is the profile whose render
the worker's argv matches (its assignment, the targets of its successful rollouts, then any profile), else the
target of its last successful rollout, else its assignment. A step whose `from` is its target says it has no way
back instead of "reverted"; *Assign* on an online worker warns that it changes nothing until a restart.

**D-46 Node definitions come from workers in maintenance too; a list served out of date says so.** *(field, dev.24)*
— Two failed rollouts left the group's only GPU worker and the UI worker in maintenance. The rollouts after them
succeeded and, restoring maintenance as they found it, left both there; the read of the group's node definitions after
each asked workers in service only — none — and the editor kept those from before three node packs: a pack that was
installed showed as "missing", with nothing to say why. → A worker in maintenance takes no jobs but runs the group's
node sets: the read asks the workers the rollout just verified first (maintenance or not), then workers in service,
then — only when there is none — workers in maintenance. `/api/object_info` served from a list that could not be read
again carries `X-ComfyFarm-Nodes-Stale: <when it was read>`, and the editor's extension shows it once per list. A
rollout still restores maintenance as it was (an admin's maintenance is an admin's), and a step that leaves its worker
in maintenance says so: "running …; still in maintenance as before the rollout: it takes no jobs until maintenance is
turned off".

**D-47 Deleting a profile archives it; only a profile in use stays.** *(field, dev.24)* — 26 profiles in five days,
two assigned, and none of the others could be deleted: every one was named by a rollout's history (target, or a step's
from/to), the database refused, and the dashboard showed "the profile is assigned or part of a rollout's history" —
read as "assigned" next to an *Assigned* count of 0. → *Delete* sets `deleted_at`: the profile leaves lists,
assignments and new rollouts; the history still names it, D-45 still finds it on a worker whose command line matches
it, and a partial unique index frees its name. It is refused, with the reason, while it is assigned, a worker runs it
(D-45's `running_profile`: an assignment without a restart is not what runs) or an active rollout uses it.
`POST /api/admin/profiles/delete {ids}` deletes several at once and says why each other one stays; the dashboard
selects them, or all those not in use.

**D-48 The gateway passes header bytes, closes every upstream, and keeps the router's connections apart.** *(field,
dev.25)* — Every page answered `router unavailable (PoolTimeout)` a moment after a job was queued. Saved outputs named in
Korean: ComfyUI sends `Content-Disposition: filename="…"` as raw UTF-8; the gateway decoded the headers to text and
raised re-encoding them (latin-1), after opening the upstream stream and before anything would close it. Each view of
such a file kept one connection; after 100 in 39 hours the single pool (httpx default 100) was full, and the router's
requests waited in it too. → Headers go through as received bytes (hop-by-hop ones dropped); a response object owns
the upstream and closes it when it ends, fails or is cancelled — also when the browser is gone before the first byte,
where a `finally` inside the body generator never runs; the router and the workers have separate pools
(`upstream_max_connections` for the workers). Router paths match segment-wise (`/api/prompt`, `/api/prompt/…`, not
`/api/prompt_json`).

**D-49 A node set holds one copy of a pack; a worker that dies as it starts fails its step at once.** *(field, dev.25)*
— A pack's next version was uploaded as a second pack instead of *Update*. Both unpacked from `stage_carry/` and
registered the same route; ComfyUI stopped at start ("method HEAD is already registered"), the service manager
restarted it 48 times, and the rollout waited out its 900 s before going back — twice. → The build refuses two packs
from the same repository or two archives with the same top-level folder, naming both and saying to *Update* the first.
While a step waits for the worker, three automatic restarts (the service manager's `NRestarts`, read with the status)
fail the step at once, pointing at `logs/<instance>.log` in the service account's home. Reading that log for the
error is left out: the agent answers restart, status and check only (contract §6), and widening it is its own decision.

**D-50 Worker memory is shown from the health check, VRAM as the driver counts it.** *(field, dev.26)* — A two-hour
video run that no longer fit in RAM swapped (150 MB/s in) and ran at half speed, and nothing on the farm's pages said
so: the dashboard knew a worker's VRAM size only. → From extension 0.11 `GET /comfyfarm/health` carries host RAM
(available, not free), swap and its in/out counters, ComfyUI's RSS, and per device the VRAM torch reserved and the
VRAM in use from the driver; the router keeps the last reading per worker in memory (a write every few seconds is no
history; /metrics is) and computes swap rates from two readings. The device's use is not taken from
`torch.cuda.mem_get_info`: on ROCm it said 14.1 GiB where the driver (sysfs, by the device's PCI address) said 23.5 and
torch alone had reserved 22.8; on CUDA it is the driver's figure. Older workers show RAM from `/system_stats` and the
VRAM ComfyUI reserved. Admins see it in System → Workers, users in the cards of the groups they may use. No alert yet:
its threshold comes from the readings.
