# 06 — Health and monitoring

## Worker health (D-11, D-12)

Every 5 s all workers are probed **concurrently** (`GET /queue`, 3 s timeout); results are applied serially.

| Probe result | Class | Meaning |
|---|---|---|
| 2xx | ok | clear `failing_since`, `failure_kind`; `state=online` |
| read timeout, write timeout, pool timeout, read error | busy | TCP connected → the process exists, its loop is blocked (model load, P14) |
| connect refused, connect timeout, DNS | dead | nothing is listening |
| anything else | busy | a false "busy" is cheaper for users than a false "dead" |
| 2xx, but its vitals fail | broken | it answers and runs nothing: the prompt executor thread died, or the GPU context is broken (dev.22) |

**Vitals** (dev.22, field dev.21: a GPU fault killed ComfyUI's executor thread; `/queue` answered 200 for hours and
the next job waited in its queue forever): a worker that answers `/queue` is asked one more thing — the extension's
`GET /comfyfarm/health` from 0.9 (`executor`: alive/dead, `device`: ok/cpu/error), else `GET /system_stats`, which
ComfyUI answers 500 when the GPU context is broken. No answer there is no evidence; the probe judges reachability.

`failing_since` is set on the first failure of a streak and **cleared on every success** (forgetting to clear it made
the next single failure exceed the threshold immediately). Thresholds use elapsed time since `failing_since`:

- `dead` (or `broken`) for ≥ 15 s → `offline`: no new jobs; running jobs are left to reconcile/lost-job judgement.
  A broken worker's reason is kept as `capabilities.broken_as` and shown among its warnings. From extension 0.9 the
  worker restarts itself: a watchdog thread exits the process (code 75) once the executor it saw is gone — from 0.10
  also when the GPU context reports an error on two looks in a row (an illegal address is sticky: every later call in
  the process fails) — and the service manager (`Restart=on-failure`) starts ComfyUI again; the jobs that waited in its
  queue go back in line (D-43), the one that faulted is queued again once (D-44).
- A worker whose extension is older than the router's (`bundled_extension`, read from its share directory) is told to
  Build and roll out: upgrading the router changes nothing on a worker until then (field, dev.22).
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

"Worker of the group" below means enabled, not deleted and meeting the group's requirements; "idle" additionally
means online, not in maintenance and without a current job — exactly the dispatcher's conditions (D-33).

| Field (per group) | Definition | Pitfall it avoids |
|---|---|---|
| `pending` | pending jobs of the group **or ungrouped**, that some worker of the group meets (job requirements) and whose owner may use the group (D-34) | metric and dispatcher disagreeing about ungrouped jobs (D-03) |
| `paused` | paused jobs of the group | — |
| `running` | jobs dispatching/running/cancelling on workers of the group | — |
| `idle_workers` | idle workers of the group | — |
| `dispatch_idle_seconds` | while an idle worker meets some waiting job: `min(now - last dispatch to a worker of the group, oldest wait of such a job)`, wait counted from the job's last transition to `pending`; otherwise `null` | "oldest pending age" alone measures backlog, not stall; `created_at` makes a resumed job look stalled for its whole age; a job only a busy worker can take is not a stall; `updated_at` would act as a heartbeat |
| `running_longest_seconds` | oldest of: `now - started_at` for `running`/`cancelling` (dispatch time until the worker reports a start), `now - dispatched_at` for `dispatching` | measuring from `created_at` reports lifetime incl. queueing; looking at `started_at` only hides jobs stuck in `dispatching` |
| `unsatisfiable_pending` | pending jobs of the group no worker of the group can run (requirements, or the owner lost the permission) | such jobs wait forever without anyone noticing, or (counted as pending) fake a dispatch stall |
| `rollout_started_at` | start of an active rollout, else `null` | a boolean "suppress alerts" flag that gets stuck silences monitoring forever |
| `inconsistent_workers` | online workers whose node set or model files differ from the rest of their group (majority = reference; see *Group consistency*) | a job validated on one worker and executed on another with different nodes fails late and confusingly — the original farm had exactly this incident |

Top level: `ungrouped_unsatisfiable_pending` — ungrouped jobs no group can take (they belong to no group's numbers).

`GET /metrics` — the same computation (`health/dispatch.py`) as Prometheus gauges, label `group="<id>"`:
`comfyfarm_pending_jobs`, `comfyfarm_paused_jobs`, `comfyfarm_running_jobs`, `comfyfarm_idle_workers`,
`comfyfarm_dispatch_idle_seconds`, `comfyfarm_running_longest_seconds`, `comfyfarm_unsatisfiable_pending_jobs`,
`comfyfarm_rollout_started_timestamp_seconds`, `comfyfarm_group_inconsistent_workers`, plus `comfyfarm_ungrouped_unsatisfiable_pending_jobs` and
`comfyfarm_ledger_up`. A `null` is an **absent series**, never 0. When the ledger cannot be read the scrape still
succeeds with only `comfyfarm_ledger_up 0`: "not observed" must not look like "nothing to report".

Example alert (stall for 5 minutes, unless a rollout is running):

```
comfyfarm_dispatch_idle_seconds > 300 unless on(group) comfyfarm_rollout_started_timestamp_seconds
```

Rules learned the hard way:

- **Never coerce unknown to 0** in a metric whose 0 means "healthy": a later `min()` picks the 0 and turns a full stall
  into "fine". Unknown is `null`, and alert rules treat `null` with a fallback.
- Alert on **duration** (continuous seconds above a threshold), not on single samples or counts. Judge thresholds by
  "fraction of time the alert would be on + longest episode", not by number of alerts.
- `/metrics` mirrors the JSON fields; the JSON endpoint stays for simple checkers.

## Logs

- Implemented (`comfyfarm serve --log-dir`): stdout for the service manager, plus `router.log` rotated by size
  (20 MB × 5) and a separate `router-error.log` holding real failures only — a level split is a stronger filter than regular
  expressions in the monitoring system.
- Implemented: every exception log line includes the exception **type** (`httpx` exceptions often have an empty
  `str()`); a drain that keeps waiting logs a heartbeat line every 4 minutes so external log-based suppression windows
  do not expire mid-operation.

## Group consistency

A group promises "same nodes, same models" (D-03, 10) — and that promise is now checked. Whenever a worker's
capabilities are refreshed (it came online, a rollout finished) the router also reads its **inventory**: node class
names from `/object_info` and the file list of every `/models/{folder}` (core routes, no extension needed), stored
with two hashes. Models change on disk without a restart, so one stale inventory is re-read per health tick
(`inventory_period`, default 10 min). Within a group the most common (nodes, models) pair among workers in service
is the reference; every other worker gets a warning naming the missing/extra nodes and the missing/extra files per
folder, the group card shows "N worker(s) differ", and `inconsistent_workers` / the metric carry the count. A group
with a single inventoried worker has nothing to compare and reports 0.

