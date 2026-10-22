-- Uploads (docs/design/18): models and node pack archives from the operator's own files, sent in chunks.
CREATE TABLE uploads (
  id          uuid PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('model', 'nodepack')),
  filename    text NOT NULL,
  size        bigint NOT NULL CHECK (size > 0),
  received    bigint NOT NULL DEFAULT 0,
  state       text NOT NULL DEFAULT 'uploading' CHECK (state IN ('uploading', 'done', 'failed', 'cancelled')),
  meta        jsonb NOT NULL DEFAULT '{}',          -- model: folder, groups, note · node pack: name, ui, pack id …
  error       text,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- a model comes from Hugging Face, an upload or the inbox; only the first has a repository and a file in it
ALTER TABLE models ADD COLUMN source text NOT NULL DEFAULT 'hf' CHECK (source IN ('hf', 'upload', 'inbox'));
ALTER TABLE models ADD COLUMN upload uuid;
ALTER TABLE models ALTER COLUMN repo DROP NOT NULL;
ALTER TABLE models ALTER COLUMN path DROP NOT NULL;

-- a node pack is a git repository at a commit, or an archive pinned by its SHA-256
ALTER TABLE node_packs ADD COLUMN archive text CHECK (archive ~ '^[0-9a-f]{64}$');
ALTER TABLE node_packs ADD COLUMN archive_name text;
ALTER TABLE node_packs ALTER COLUMN repo DROP NOT NULL;
ALTER TABLE node_packs ALTER COLUMN commit DROP NOT NULL;
ALTER TABLE node_packs ADD CONSTRAINT node_packs_source CHECK ((repo IS NOT NULL AND commit IS NOT NULL) <> (archive IS NOT NULL));
