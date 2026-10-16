-- 0002: capability requirements (docs/design/10-heterogeneous-gpus.md).
ALTER TABLE jobs ADD COLUMN requirements jsonb NOT NULL DEFAULT '{}';

-- Does a worker's capability document satisfy a requirement document?
-- Supported keys (validated at submit/group update, unknown keys are rejected there):
--   backend: ["cuda","rocm",...]   min_vram_gb: number   os: ["posix","nt"]
-- A worker whose capabilities are unknown satisfies only empty requirements.
CREATE FUNCTION comfyfarm_satisfies(caps jsonb, req jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT
    (NOT req ? 'backend' OR (caps ? 'backend' AND req->'backend' ? (caps->>'backend')))
    AND (NOT req ? 'os' OR (caps ? 'os' AND req->'os' ? (caps->>'os')))
    AND (NOT req ? 'min_vram_gb' OR coalesce((
          SELECT max((d->>'vram_total')::numeric) FROM jsonb_array_elements(coalesce(caps->'devices', '[]'::jsonb)) d
        ), 0) >= (req->>'min_vram_gb')::numeric * 1073741824)
$$;
