-- Models from the dashboard (docs/design/17): what the builder fetched into the model store, and which groups use it.
CREATE TABLE models (
  id          bigserial PRIMARY KEY,
  folder      text NOT NULL,                     -- ComfyUI's model folder (checkpoints, diffusion_models, …)
  filename    text NOT NULL,
  repo        text NOT NULL,                     -- Hugging Face repository, file and the ref asked for
  path        text NOT NULL,
  ref         text NOT NULL DEFAULT 'main',
  revision    text,                              -- the commit it was fetched from
  size        bigint,
  sha256      text,
  key         text,                              -- fetch.model_key: what hosts ask for and the builder signs
  state       text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'downloading', 'ready', 'failed')),
  bytes       bigint NOT NULL DEFAULT 0,
  error       text,
  request     text,                              -- the builder request fetching it
  note        text,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (folder, filename)
);
CREATE TABLE group_models (
  group_id    bigint NOT NULL REFERENCES worker_groups(id) ON DELETE CASCADE,
  model_id    bigint NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, model_id)
);
