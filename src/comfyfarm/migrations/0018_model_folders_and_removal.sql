-- Model folders and removing models (docs/design/17 phase 2, field dev.18).
-- An instance's model folders: what its render's extra_model_paths lists. NULL: set up before 0.1.0-dev.19 — its
-- launcher takes the legacy list only; setup since records the farm's folders and keeps them current.
ALTER TABLE instances ADD COLUMN model_folders text[];

-- Removing a model from the farm: 'deleting' while the builder takes it out of the store, then 'deleted' — kept (its
-- key) so hosts that still hold a copy are told to drop it; the same place can be filled again.
ALTER TABLE models DROP CONSTRAINT models_state_check;
ALTER TABLE models ADD CONSTRAINT models_state_check
  CHECK (state IN ('queued', 'downloading', 'ready', 'failed', 'deleting', 'deleted'));
ALTER TABLE models ADD COLUMN deleted_at timestamptz;
ALTER TABLE models DROP CONSTRAINT models_folder_filename_key;
CREATE UNIQUE INDEX models_place ON models (folder, filename) WHERE state <> 'deleted';
