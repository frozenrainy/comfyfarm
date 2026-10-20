-- Node packs picked in the dashboard (docs/design/15): built into node sets and venvs by the control-plane builder,
-- from repositories under the builder's own COMFYFARM_BUILD_NODE_SOURCES. Pinned: a full commit.
CREATE TABLE node_packs (
  id                 bigserial PRIMARY KEY,
  name               text NOT NULL UNIQUE,          -- its directory under custom_nodes
  repo               text NOT NULL,
  commit             text NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$'),
  ref                text,                          -- the branch or tag it was resolved from (shown only)
  ui                 boolean NOT NULL DEFAULT true, -- false: imports only with a GPU, kept off the UI worker
  requirements_file  text,
  constraints        text[] NOT NULL DEFAULT '{}',
  note               text,
  added_by           text,
  added_at           timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
