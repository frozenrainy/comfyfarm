-- D-47: a profile deleted from the dashboard is archived — kept for the rollout history that names it (and for the
-- way back of a worker that still runs it, D-45), gone from lists, assignments and new rollouts. Its name is free again.
ALTER TABLE profiles ADD COLUMN deleted_at timestamptz;
ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_name_key;
CREATE UNIQUE INDEX profiles_name_live ON profiles (name) WHERE deleted_at IS NULL;
