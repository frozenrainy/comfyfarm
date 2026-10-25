# 05 — Events: progress and preview relay

## Router ↔ worker WebSocket

- One connection per worker, `clientId = uuid5(router_instance_name, worker_id)` (D-13), so after a router restart the
  worker keeps delivering events of jobs that were submitted before the restart.
- Every reconnect with the same id — after a router restart **or** inside one process after a ping timeout — can hit the
  stock reconnect race (P11), whatever the id scheme. The worker fix (D-14) is therefore needed for reliable progress in
  general, not as a precondition of deterministic ids. Without it the router warns on the dashboard ("progress may
  freeze after reconnects"), keeps the long ping timeout to make reconnects rare, and relies on reconcile for job
  outcomes (outcomes stay correct; only live progress can freeze).
- Jobs are POSTed with that `client_id`, so their events arrive on this socket only (P10).
- First message: `feature_flags` with `supports_preview_metadata` (P12) so binary preview frames carry the `prompt_id`.
- `ping_interval=20 s`, `ping_timeout=300 s` (P14): a worker that is loading a model must not be disconnected. Not
  `None`: half-open sockets must still be detected eventually.
- Reconnect with backoff (1, 2, 5, 10 s); on reconnect the worker re-sends the currently executing node.

## Processing a worker event

```
recv(event)
  target := relay_map.get(worker) or rebuild_from_ledger(worker)      # D-17, at most once per 2 s per worker
  match event.type:
    progress / progress_state / executing → progress.offer(worker, job, value)  # throttled write, D-15
    execution_success                     → core.complete(job)              # reconcile confirms later
    execution_error                       → core.fail(job, event.data)
    execution_interrupted                 → core.cancelled(job)
    binary preview                        → (relay only)
  spawn relay(target_socket, event)        # target captured NOW, passed by value — D-16
```

- `progress.offer` writes at most once per 2 s per worker and only if the value changed; acquire timeout 5 s; a failed
  write is dropped (the next one carries the newer value) and **never** breaks the receive loop.
- Relay tasks are held in a set (no GC of running tasks) and serialised per browser socket by a lock, so text and binary
  frames keep their order.
- Progress shown to the user is per node (`progress_state`), not a single percentage guessed from sampler node names —
  two-stage samplers made the old 0–100 % bar run twice. The nodes are **weighed** (dev.21): the router reads a plan
  off the API prompt (`core.jobs.job_plan` — a sampler counts its `steps`, a VAE encode/decode 2, anything else 0.1)
  and sends it with running jobs; the browser (`dashboard/js/progress.js`) sums finished and partial weights. Counting
  nodes alike put a 20-node video workflow at 5 % through its one KSampler. The ETA is the steps left at the pace of
  the sampler at work (two of its reports) — never `elapsed × (1 − p) / p`, which the minutes of model loading before
  the first step blow up — and a row with no report for 20 s says so rather than show a stale number. Not solved:
  loops that run the same nodes N times (a finished node going back to running lowers the bar); the next step is the
  per-node times of the same workflow's earlier runs, a better clock than any guess from the prompt.

## Browser WebSocket

- Browsers connect to the gateway's `/ws`; the gateway authenticates, binds the browser's `clientId` to the identity
  (09-security) and forwards to the router.
- On connect the router sends `status` and then **catch-up** (D-19): final events for this client's jobs finished in the
  last 60 s, and for jobs the ledger has as running but the worker reports finished.
- `status` (pending + running counts for the user's view) is pushed on submit / dispatch / finish / cancel (D-18).
- In HA, a node that is not writable closes new browser sockets with code **1013 (try again later)**; the stock frontend
  reconnects within a second and lands on the active node. Sockets on a node that loses the active role are closed by
  the drain procedure; a 10 s heartbeat lets the frontend extension detect a silently dead node within 25 s.
