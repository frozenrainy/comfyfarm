# 03 — Job lifecycle

## States

```
                 submit
                   │
   pause ┌──────▶ pending ◀──────────────┐ connect error before the worker saw it
         │         │  ▲                  │ (Unreachable) / ask-worker: absent
       paused ◀────┘  │ resume           │
                   │  dispatcher claims  │
                   ▼                     │
              dispatching ───────────────┘
                   │ Accepted / ask-worker: present / cannot tell (stays, reconcile decides)
                   ▼
                running ──── cancel ───▶ cancelling ──▶ cancelled
                   │                          │
     success ┌─────┴─────┐ error / 4xx / lost │ worker finished anyway
             ▼           ▼                    ▼
         completed     failed         completed | failed
```

Terminal: `completed`, `failed`, `cancelled`. A user "retry" creates a **new job** (new `prompt_id`) copied from the old
payload, appended to the queue; the old row stays as history.

## Dispatch outcomes (adapter → core)

The adapter maps every POST to exactly one outcome. The mapping is the heart of D-05/D-07.

| Adapter outcome | Typical cause | Core transition | Why |
|---|---|---|---|
| `Accepted(number)` | 200 | dispatching → running | — |
| `Rejected(status, node_errors)` | 4xx | → failed (node_errors attached) | Same answer on every worker; retrying loops forever (D-07) |
| `Unreachable` | connect refused / connect timeout / DNS | → pending, `attempt+1`; worker marked dead-suspect | Request never reached the process |
| `Ambiguous` | read timeout, connection reset after send, 5xx | ask worker `/api/jobs/{id}` (bounded): present → running · absent **twice, the second ≥ 5 s after the POST ended** → pending · anything else → **stay `dispatching`** | Worker may be executing it (D-05); a quick reset can mean "still parsing the body"; a blind retry would run it twice (P3) |
| `Bug` | 400 `invalid_prompt_id` | → failed + alert | Router generated a non-canonical id (P2) |

`attempt` caps connect-failure re-queues (default 5) so a job whose target group is entirely unreachable eventually
fails visibly instead of cycling.

## Reconciliation and lost jobs

Every 5 s, for jobs in `dispatching | running | cancelling`, group by worker and ask `GET /api/jobs/{id}` (fallback:
`/queue` + `/history/{id}` for older ComfyUI):

| Worker says | Ledger action |
|---|---|
| `pending` / `in_progress` | keep (set `started_at` on first `in_progress`) |
| `completed` | → completed, fetch outputs, correct `output_hint` (D-10) |
| `failed` | → failed, keep the worker's error message |
| `cancelled` | → cancelled (P8) |
| 404 / unknown, and the job left the router more than *grace* ago | → **failed, kind=`lost`** ("worker restarted or never received it — rerun if needed"), never pending (D-06) |
| no answer (busy worker) | keep; busy is judged by health, not here |

*grace* = dispatch timeout (10 s) + reconcile period (5 s) + margin = 30 s by default.

**Worker gone (D-06b):** reconcile needs an answer from the worker. Jobs on a worker that has been offline for more than
10 min (configurable), or that was deleted or disabled, fail with kind `worker_lost` — otherwise they would stay
`running` forever.

WS `execution_success` / `execution_error` / `execution_interrupted` apply the same transitions immediately; the
reconcile loop then only confirms. Transitions are idempotent `UPDATE … WHERE status IN (…)` so the two paths cannot
fight.

## Cancel

- `pending` / `paused` → `cancelled` directly (nothing on a worker).
- `dispatching` → mark `cancelling`; when the POST resolves, send a targeted interrupt (or `queue delete` if the worker
  has it pending).
- `running` → `cancelling` + `POST /interrupt {"prompt_id"}` (P6). The worker keeps the job until the current node yields
  (P7); reconcile moves it to `cancelled` (or `completed` if it finished first). The worker is not idle until then.

## Pause / reorder

`paused` jobs are skipped by the dispatcher. Reorder changes `priority` only. Both are pure ledger operations.

## Priority and group policy

Dispatcher order for a worker in group G:
1. pending jobs with `group_id = G`, highest priority first;
2. ungrouped jobs, according to the site policy (D-03): `lowest_priority` (only when G has nothing) or
   `least_loaded_group` (assigned to the group with the shortest queue at submit time).

The same SQL function computes "what would be dispatched" for the monitoring endpoint, so metrics and behaviour cannot
drift apart (D-03).
