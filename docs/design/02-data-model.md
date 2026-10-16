# 02 — Data model (Postgres)

Rules that shape the schema:

- **Hot rows are small** (D-01). Workflows can exceed 400 KB; they live in `job_payloads`. Dispatch, health, progress
  and dashboards never read that table.
- **One timestamp, one meaning** (D-25). No logic reads `updated_at`.
- **UTC `timestamptz`** everywhere; formatting is the client's job.
- **Text, not length-limited varchar**, for anything user-shaped (paths, prefixes) — a length overflow once rejected a
  whole submit.
- **The ledger is the truth**; anything in memory must be derivable from these tables (D-17).

```sql
CREATE TYPE job_status AS ENUM
  ('pending','dispatching','running','cancelling','completed','failed','cancelled','paused');

CREATE TABLE worker_groups (
  id            bigserial PRIMARY KEY,
  name          text NOT NULL UNIQUE,
  requirements  jsonb NOT NULL DEFAULT '{}'      -- optional capability constraints, see 10-heterogeneous-gpus
);

CREATE TABLE workers (
  id               bigserial PRIMARY KEY,
  name             text NOT NULL UNIQUE,
  base_url         text NOT NULL UNIQUE,
  group_id         bigint REFERENCES worker_groups(id) ON DELETE SET NULL,
  enabled          boolean NOT NULL DEFAULT true,
  maintenance      boolean NOT NULL DEFAULT false,
  state            text NOT NULL DEFAULT 'unknown', -- online | busy_unresponsive | offline | unknown
  failing_since    timestamptz,                     -- first failure of the current streak; NULL after any success
  failure_kind     text,                            -- busy | dead | NULL
  current_job_id   bigint,                          -- set by the dispatcher under row lock
  capabilities     jsonb NOT NULL DEFAULT '{}',     -- backend, devices, vram, versions (from /system_stats)
  last_ok_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id                  bigserial PRIMARY KEY,
  prompt_id           uuid NOT NULL UNIQUE,         -- chosen by the client/router, sent to the worker (P1–P3)
  status              job_status NOT NULL DEFAULT 'pending',
  priority            bigint NOT NULL,              -- larger runs first
  group_id            bigint REFERENCES worker_groups(id) ON DELETE SET NULL,
  worker_id           bigint REFERENCES workers(id) ON DELETE SET NULL,
  owner               text NOT NULL,                -- identity from the identity provider
  idempotency_key     uuid,                         -- = prompt_id for frontend submits (D-23)
  browser_client_id   text,                         -- WS relay target; bound to owner (09-security)
  output_hint         text,                         -- display path estimate, corrected at completion (D-10)
  outputs             jsonb,                        -- real outputs from the worker at completion
  error               jsonb,                        -- {kind, message, node_errors?, worker?}
  progress            jsonb,                        -- {value, max, node, updated} — written throttled (D-15)
  attempt             int NOT NULL DEFAULT 0,       -- dispatch attempts (connect failures)
  created_at          timestamptz NOT NULL DEFAULT now(),
  dispatched_at       timestamptz,                  -- left the router
  started_at          timestamptz,                  -- worker reported execution_start
  finished_at         timestamptz,                  -- terminal state reached
  deleted_at          timestamptz,                  -- soft delete (hidden from lists)
  updated_at          timestamptz NOT NULL DEFAULT now()  -- humans only; no logic reads it
);
CREATE INDEX jobs_active      ON jobs (status, group_id, priority DESC, id) WHERE status IN ('pending','paused');
CREATE INDEX jobs_running     ON jobs (worker_id) WHERE status IN ('dispatching','running','cancelling');
CREATE INDEX jobs_owner_list  ON jobs (owner, deleted_at, status, id DESC);
CREATE UNIQUE INDEX jobs_idem ON jobs (owner, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE job_payloads (
  job_id      bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  prompt      jsonb NOT NULL,        -- the API-format workflow
  extra_data  jsonb NOT NULL DEFAULT '{}'
);
ALTER TABLE job_payloads ALTER COLUMN prompt SET COMPRESSION lz4;   -- PG ≥ 14

CREATE TABLE object_info_snapshots (
  group_id    bigint PRIMARY KEY REFERENCES worker_groups(id) ON DELETE CASCADE,
  data        bytea NOT NULL,        -- gzip of the /object_info body
  node_count  int NOT NULL,
  source      text NOT NULL,         -- worker name it came from
  fetched_at  timestamptz NOT NULL
);

CREATE TABLE job_events (            -- audit trail of state transitions (small rows, append-only)
  id         bigserial PRIMARY KEY,
  job_id     bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  at         timestamptz NOT NULL DEFAULT now(),
  from_status job_status,
  to_status   job_status NOT NULL,
  reason     text
);

CREATE TABLE schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
```

Rollout tables (`profiles`, `profile_assignments`, `rollouts`, `rollout_steps`, `audit_log`) follow
[07](07-rollout-and-upgrades.md) and are added in M4.

## Notes

- **Priority:** append = `min(priority) - 1` within pending, "run next" = `max + 1`, retry = append. A gap-free integer is
  not needed; `bigint` never runs out in practice.
- **Idempotency** (D-23): unique `(owner, idempotency_key)`; a resubmit returns the existing row. Keys are never reused
  (the key is also the globally unique `prompt_id`). A key that collides with **another owner's** job returns 409
  without revealing whether that job exists; the insert is `ON CONFLICT DO NOTHING` + lookup, never a 500.
- **Why `dispatching` is a state:** it is the window between commit and the worker's answer. Reconcile treats it like
  `running` but with a shorter grace period, and the dashboard can show "sending".
- **Why `worker.current_job_id`:** the dispatcher locks the worker row; a worker with a current job is not idle even if
  its `/queue` momentarily reads empty (the check and the POST are not atomic).
- **Retention:** `job_events` and terminal jobs older than N days can be pruned by a periodic task; payloads first.
