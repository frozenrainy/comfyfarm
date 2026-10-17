-- 0006: profiles, instances, rollouts, audit (docs/design/07, contract docs/contracts/comfy-upgrade-1.md).

-- What a worker runs: artifact names + flags. The render (§3) combines a profile with an instance's paths.
CREATE TABLE profiles (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  core        text NOT NULL,                       -- <ART>/core/<core>
  venv        text NOT NULL,                       -- <ART>/venv/<venv>
  nodes       text[] NOT NULL DEFAULT '{}',        -- <ART>/nodes/<n>, in load order
  frontend    text,                                -- <ART>/frontend/<f> (optional --front-end-root)
  flags       jsonb NOT NULL DEFAULT '[]',         -- extra ComfyUI argv tokens, e.g. ["--preview-method", "auto"]
  env         jsonb NOT NULL DEFAULT '{}',
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- How a worker is launched (comfy-upgrade/1 'instance'): one per worker that the farm may restart.
CREATE TABLE instances (
  worker_id        bigint PRIMARY KEY REFERENCES workers(id) ON DELETE CASCADE,
  instance         text NOT NULL UNIQUE,
  role             text NOT NULL DEFAULT 'gpu' CHECK (role IN ('gpu', 'ui')),
  platform         text NOT NULL DEFAULT 'posix' CHECK (platform IN ('posix', 'windows')),
  hosts            text[] NOT NULL DEFAULT '{}',   -- addresses allowed to fetch the render (others: 403)
  listen           text NOT NULL DEFAULT '0.0.0.0',
  port             int NOT NULL,
  paths            jsonb NOT NULL,                 -- art, local, models, input, output, user, temp
  profile_id       bigint REFERENCES profiles(id), -- current assignment (NULL: none → render 404)
  restart_backend  text NOT NULL DEFAULT 'manual',
  restart_config   jsonb NOT NULL DEFAULT '{}',
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rollouts (
  id                 bigserial PRIMARY KEY,
  target_profile_id  bigint NOT NULL REFERENCES profiles(id),
  status             text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'succeeded', 'failed', 'aborting', 'aborted', 'interrupted')),
  window_size        int NOT NULL DEFAULT 1,
  options            jsonb NOT NULL DEFAULT '{}',
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  finished_at        timestamptz,
  error              text
);
-- one rollout at a time: two would fight over the same workers' maintenance flags
CREATE UNIQUE INDEX rollouts_one_active ON rollouts ((true)) WHERE status IN ('running', 'aborting', 'interrupted');

CREATE TABLE rollout_steps (
  id               bigserial PRIMARY KEY,
  rollout_id       bigint NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
  worker_id        bigint NOT NULL REFERENCES workers(id),
  position         int NOT NULL,
  from_profile_id  bigint REFERENCES profiles(id),
  to_profile_id    bigint NOT NULL REFERENCES profiles(id),
  was_maintenance  boolean,                        -- restored when the step succeeds
  status           text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'draining', 'restarting', 'starting', 'verifying', 'done',
                                     'failed', 'reverting', 'reverted', 'revert_failed', 'skipped')),
  detail           text,
  error            text,
  started_at       timestamptz,
  finished_at      timestamptz,
  UNIQUE (rollout_id, worker_id)
);

CREATE TABLE audit_log (
  id      bigserial PRIMARY KEY,
  at      timestamptz NOT NULL DEFAULT now(),
  actor   text NOT NULL,                           -- user, or 'rollout #n (user)'
  action  text NOT NULL,
  target  text,
  detail  jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_log_at ON audit_log (at DESC);
