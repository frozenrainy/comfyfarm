# 06 — Health and monitoring

## Worker health (D-11, D-12)

Every 5 s all workers are probed **concurrently** (`GET /queue`, 3 s timeout); results are applied serially.

| Probe result | Class | Meaning |
|---|---|---|
| 2xx | ok | clear `failing_since`, `failure_kind`; `state=online` |
| read timeout, write timeout, pool timeout, read error | busy | TCP connected → the process exists, its loop is blocked (model load, P14) |
| connect refused, connect timeout, DNS | dead | nothing is listening |
| anything else | busy | a false "busy" is cheaper for users than a false "dead" |

`failing_since` is set on the first failure of a streak and **cleared on every success** (forgetting to clear it made
the next single failure exceed the threshold immediately). Thresholds use elapsed time since `failing_since`:

- `dead` for ≥ 15 s → `offline`: no new jobs; running jobs are left to reconcile/lost-job judgement.
- `busy` for ≥ 90 s → `busy_unresponsive`: no new jobs; shown amber, not red.
- Recovery is immediate on the next success.

Capabilities (`/system_stats`: versions, devices, VRAM, backend) are refreshed when a worker comes back online and when
the version badge changes (07).

Optional **worker agent** (per host): explains *why* a worker is offline — OS down / booting / service stopped /
"zombie" (service active but HTTP dead, typically a hung GPU). For zombies the kernel log (NVIDIA Xid, amdgpu reset
messages) is the fastest evidence; the job that was running is usually the victim, not the cause.

## Monitoring contract

`GET /api/health/dispatch` — unauthenticated, numbers only (no names, paths or users), **per group**: a stall in one
group must not be averaged away by another group that is still busy.

| Field (per group) | Definition | Pitfall it avoids |
|---|---|---|
| `pending` | count of dispatchable pending jobs (same rule as the dispatcher, D-03) | metric and dispatcher disagreeing about ungrouped jobs |
| `idle_workers` | online, not in maintenance, no current job | — |
| `dispatch_idle_seconds` | `min(now - last_dispatch_at, oldest_dispatchable_pending_age)`, only when `idle_workers > 0` and `pending > 0`; otherwise `null` | "oldest pending age" alone measures backlog, not stall; `updated_at` would act as a heartbeat |
| `running_longest_seconds` | `now - started_at` of the oldest running job | measuring from `created_at` reports lifetime incl. queueing, not execution |
| `rollout_started_at` | start of an active rollout, else `null` | a boolean "suppress alerts" flag that gets stuck silences monitoring forever |

Rules learned the hard way:

- **Never coerce unknown to 0** in a metric whose 0 means "healthy": a later `min()` picks the 0 and turns a full stall
  into "fine". Unknown is `null`, and alert rules treat `null` with a fallback.
- Alert on **duration** (continuous seconds above a threshold), not on single samples or counts. Judge thresholds by
  "fraction of time the alert would be on + longest episode", not by number of alerts.
- Prometheus exposition (`/metrics`) mirrors the same fields; the JSON endpoint stays for simple checkers.

## Logs

- Rotate by size; separate `error.log` (real failures only) from the main log — a level split is a stronger filter than
  regular expressions in the monitoring system.
- Every exception log line includes the exception **type** (`httpx` exceptions often have an empty `str()`).
- Long-running operations (a drain can take hours) emit a heartbeat line every 4 minutes so external log-based
  suppression windows do not expire mid-operation.
