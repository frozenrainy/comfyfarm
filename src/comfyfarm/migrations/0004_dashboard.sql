-- 0004: dashboard (M3). Notes are free text for humans; no logic reads them.
ALTER TABLE jobs ADD COLUMN note text;
ALTER TABLE jobs ADD COLUMN retry_of bigint REFERENCES jobs(id) ON DELETE SET NULL;   -- user retry = new job (docs/design/03)
ALTER TABLE workers ADD COLUMN note text;

-- last dispatch per worker group (/api/health/dispatch, /metrics)
CREATE INDEX jobs_dispatched ON jobs (worker_id, dispatched_at DESC) WHERE dispatched_at IS NOT NULL;
