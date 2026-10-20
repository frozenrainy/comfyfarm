# 01 — Architecture

## Modules

```
comfyfarm/
  core/        ledger schema + migrations, dispatcher, job state machine, lost-job judge, group policy
  adapter/     ComfyUI protocol only: /prompt (preassigned id), /api/jobs, /queue, /interrupt, /object_info,
               /system_stats, worker WebSocket. All ComfyUI quirks (04-worker-protocol) live here.
  events/      worker WS → progress aggregation (throttled) → browser relay (target captured on receipt,
               rebuildable mapping, catch-up) → queue status broadcast
  health/      busy/dead classification, capability detection, monitoring endpoints, optional worker-agent probes
  rollout/     drain → restart → verify → undrain, canary stop, comfy-upgrade/1 render + badge, restart backends
  launcher.py, agent.py, publish.py   worker-side tools (stdlib only, copied to hosts; see guide/rollouts.md)
  gateway/     single entry point for browsers: routes user data to the UI worker, jobs/ws/object_info to the
               router, temp previews to the executing worker; pre-submit validation; static asset caching
  identity.py  identity providers: local (scrypt), trusted-header, dev  (planned: OIDC)
  ha/          (planned) "may I write?" answered by Postgres recovery state; ops tooling lives outside the package
  api/         REST for the dashboard and the frontend extension
  dashboard/   web UI (13): dashboard, workflow tab, queue, history, settings, system (workers/groups/users/rollout/stats)
  plugins/     (planned) webhooks, VRAM collection, …  (notes are built in)
worker_ext/    comfyfarm_worker custom node: source allow-list (09), validation endpoint, /ws reconnect fix (04)
ui_ext/        ComfyUI frontend extension: group picker next to Run, idempotent submit with retry  (planned: stall watchdog)
```

**Boundary rules**

1. Only `adapter/` imports HTTP/WS clients for workers. Everyone else calls typed functions that return classified
   outcomes (`Accepted`, `Rejected(node_errors)`, `Unreachable`, `Ambiguous`), never raw exceptions.
2. Only `core/` writes job state. `events/` writes progress through one throttled function; it never changes status
   except via `core` transitions (`execution_error` → `core.fail(...)`).
3. In-memory state exists only in `events/` and `gateway/`, and each piece can be rebuilt from the ledger (D-17).
4. Every write goes through `core.writable()` — the HA hook (D-26). Single-node deployments always answer yes.
5. Every pool acquire, HTTP call and WS receive has an explicit timeout (D-21).

## Processes

One Python process (FastAPI + asyncio) runs the router: API, dispatcher loop, health loop, one WS listener per worker,
rollout tasks. The gateway is a second entrypoint of the same package (so a site can run it on another host or skip it).

| Loop | Period | Does |
|---|---|---|
| dispatcher | 1 s + wake-up on submit/finish | claim (worker, job) pairs with `SKIP LOCKED`, POST, classify outcome |
| health | 5 s | concurrent `/queue`-level probe of every worker, busy/dead classification, capability refresh on change |
| reconcile | 5 s | running jobs vs worker `/api/jobs/{id}` → finish / cancel / lost |
| worker WS ×N | continuous | receive events, throttle progress, relay to browsers |
| object_info | on demand | refresh per group snapshot (manual, after rollout, or node-set change); with no GPU worker of the group, the UI worker's stands in until one answers |
| rollout | on demand | one task per rollout |

Wake-ups: submit and finish notify the dispatcher via Postgres `LISTEN/NOTIFY` so dispatch does not wait for the next
poll. Notifications are only an accelerator — the 1 s poll is the source of truth: a NOTIFY sent while the listener
connection is down is lost, and NOTIFY is not delivered to (nor can it be listened on) a streaming-replication standby.
A standby router does not dispatch anyway.

## Deployment topologies

**Shared storage (required).** The input, output and user directories must be one filesystem seen by the UI worker
and every GPU worker (NFS/SMB on a NAS — the Thinkbox Deadline model): uploads land on the UI worker and are read by
whichever GPU worker runs the job; saved outputs are written by that worker and served by the UI worker. Models may be
shared too, but a local NVMe copy per worker is recommended: ComfyUI's disk-backed dynamic loading (`fast-disk`) is
enabled only for model files on an NVMe at PCIe Gen3 x4 or better — on a NAS it loads through pinned RAM instead
(slower, not broken). Temp files (previews) need no sharing: the gateway fetches them from the executing worker, and
does the same for a saved output the UI worker does not have.

**A. Single box (default, homelab).** Router + Postgres + gateway on one machine; workers on the same or other machines.

**B. With UI worker.** Add a CPU-only stock ComfyUI as UI worker; the gateway sends user data and static assets there,
so the screen never freezes while a GPU worker loads a model. Validation then *must* run on GPU workers (D-24) and temp
previews must be routed to the executing worker.

**C. HA (optional).** Two router nodes, Postgres streaming replication, a virtual IP. The router core only knows
`writable()`; promotion/fencing tooling is a separate operations package. **Synchronous replication and fencing are
requirements**, not tuning: without them a failover can dispatch the same job twice ([D-26](decisions.md)). After
promotion, pending jobs from the failover window are checked against the workers by `prompt_id` before re-dispatch
(a complement, not a substitute).

## Data flow of one job

```
browser ──submit(idempotency key K)──▶ gateway ──▶ api.submit
   api.submit: identity → group resolve (unknown ⇒ none) → (K seen? return job) → validate on group worker
               → INSERT jobs(pending, prompt_id=K) + job_payloads → NOTIFY
dispatcher: BEGIN; worker := idle in group FOR UPDATE SKIP LOCKED; job := best pending for worker FOR UPDATE SKIP LOCKED;
            UPDATE job → dispatching(worker, dispatched_at); UPDATE worker → assigned; COMMIT
            adapter.submit(worker, prompt_id, payload, client_id=router_ws_id(worker))
              Accepted → running · Rejected → failed(node_errors) · Unreachable → pending · Ambiguous → ask worker
events: progress → throttled UPDATE + relay to browser_client_id; execution_success/error/interrupted → core transition
reconcile: running/dispatching/cancelling jobs vs /api/jobs/{id} → authoritative end state or lost-job judgement
```

## Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.12, asyncio | Same ecosystem as ComfyUI; contributors from that community |
| Web | FastAPI + uvicorn (single worker process) | Many long-lived WS; one process keeps in-memory caches coherent |
| DB | Postgres ≥ 15, `asyncpg`, plain SQL migrations | `SKIP LOCKED`, `LISTEN/NOTIFY`, `timestamptz`, lz4 TOAST for big payloads; no ORM so the SQL that decides correctness is visible |
| Worker HTTP/WS | `httpx`, `websockets` | Explicit timeouts and exception classes the adapter can map |
| Dashboard | plain ES modules served by the router, no framework and no build step (D-32) | — |
| Packaging | `uv`, one wheel; container image later | — |
| License | Apache-2.0 | Patent grant; common for infrastructure projects |
