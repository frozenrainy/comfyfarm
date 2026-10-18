-- Worker setup runs (docs/design/14). The token itself is never stored: only its SHA-256. The script (the setup
-- parameters and the worker tools of this version) is kept so the operator's checksum and the download always agree.
CREATE TABLE worker_setups (
  id             bigserial PRIMARY KEY,
  token_sha256   text NOT NULL UNIQUE,
  spec           jsonb NOT NULL,                 -- what setup was asked for (no secrets)
  extra          jsonb NOT NULL DEFAULT '{}',    -- worker name, group, ssh port, profile to assign
  script         text NOT NULL,
  script_sha256  text NOT NULL,
  mode           text NOT NULL DEFAULT 'command' CHECK (mode IN ('command', 'ssh')),
  status         text NOT NULL DEFAULT 'waiting'
                 CHECK (status IN ('waiting', 'running', 'reported', 'failed', 'finished')),
  log            text NOT NULL DEFAULT '',
  result         jsonb,
  worker_id      bigint REFERENCES workers(id) ON DELETE SET NULL,
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  reported_at    timestamptz,
  finished_at    timestamptz
);
