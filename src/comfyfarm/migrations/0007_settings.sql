-- 0007: site settings changed from the dashboard (System → Settings). Small, typed in code (api/admin.py SETTINGS).
CREATE TABLE settings (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text
);
