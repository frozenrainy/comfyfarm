-- 0001: ledger. See docs/design/02-data-model.md for the reasoning behind every column.
CREATE TYPE job_status AS ENUM
  ('pending','dispatching','running','cancelling','completed','failed','cancelled','paused');

CREATE TABLE worker_groups (
  id            bigserial PRIMARY KEY,
  name          text NOT NULL UNIQUE,
  requirements  jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE workers (
  id               bigserial PRIMARY KEY,
  name             text NOT NULL UNIQUE,
  base_url         text NOT NULL UNIQUE,
  group_id         bigint REFERENCES worker_groups(id) ON DELETE SET NULL,
  enabled          boolean NOT NULL DEFAULT true,
  maintenance      boolean NOT NULL DEFAULT false,
  state            text NOT NULL DEFAULT 'unknown',
  failing_since    timestamptz,
  failure_kind     text,
  current_job_id   bigint,
  capabilities     jsonb NOT NULL DEFAULT '{}',
  last_ok_at       timestamptz,
  offline_since    timestamptz,
  deleted_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id                  bigserial PRIMARY KEY,
  prompt_id           uuid NOT NULL UNIQUE,
  status              job_status NOT NULL DEFAULT 'pending',
  priority            bigint NOT NULL,
  group_id            bigint REFERENCES worker_groups(id) ON DELETE SET NULL,
  worker_id           bigint REFERENCES workers(id) ON DELETE SET NULL,
  owner               text NOT NULL,
  idempotency_key     uuid,
  browser_client_id   text,
  output_hint         text,
  outputs             jsonb,
  error               jsonb,
  progress            jsonb,
  attempt             int NOT NULL DEFAULT 0,
  absent_seen_at      timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  dispatched_at       timestamptz,
  dispatch_ended_at   timestamptz,
  started_at          timestamptz,
  finished_at         timestamptz,
  deleted_at          timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_active      ON jobs (status, group_id, priority DESC, id) WHERE status IN ('pending','paused');
CREATE INDEX jobs_on_worker   ON jobs (worker_id) WHERE status IN ('dispatching','running','cancelling');
CREATE INDEX jobs_owner_list  ON jobs (owner, deleted_at, status, id DESC);
CREATE UNIQUE INDEX jobs_idem ON jobs (owner, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE job_payloads (
  job_id      bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  prompt      jsonb NOT NULL,
  extra_data  jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE object_info_snapshots (
  group_id    bigint PRIMARY KEY REFERENCES worker_groups(id) ON DELETE CASCADE,
  data        bytea NOT NULL,
  node_count  int NOT NULL,
  source      text NOT NULL,
  fetched_at  timestamptz NOT NULL
);

CREATE TABLE job_events (
  id          bigserial PRIMARY KEY,
  job_id      bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  at          timestamptz NOT NULL DEFAULT now(),
  from_status job_status,
  to_status   job_status NOT NULL,
  reason      text
);
CREATE INDEX job_events_job ON job_events (job_id, id);
