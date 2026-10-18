-- A session epoch is never reused for a user name: it starts at the account's creation time (ms) and every bump
-- keeps it at or ahead of the clock, so deleting "alice" and creating "alice" again does not revive old cookies.
ALTER TABLE users ALTER COLUMN session_epoch TYPE bigint;
ALTER TABLE users ALTER COLUMN session_epoch SET DEFAULT (extract(epoch FROM clock_timestamp()) * 1000)::bigint;
UPDATE users SET session_epoch = (extract(epoch FROM clock_timestamp()) * 1000)::bigint;   -- everyone logs in once more
CREATE FUNCTION next_session_epoch(e bigint) RETURNS bigint LANGUAGE sql VOLATILE AS
  $$ SELECT greatest(e + 1, (extract(epoch FROM clock_timestamp()) * 1000)::bigint) $$;

-- Statistics count a job in the group its worker was in when it ran, not the worker's group today.
ALTER TABLE jobs ADD COLUMN ran_group_id bigint REFERENCES worker_groups(id) ON DELETE SET NULL;
UPDATE jobs j SET ran_group_id = w.group_id FROM workers w WHERE w.id = j.worker_id;
CREATE FUNCTION jobs_stamp_ran_group() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.worker_id IS NOT NULL AND (TG_OP = 'INSERT' OR NEW.worker_id IS DISTINCT FROM OLD.worker_id) THEN
    NEW.ran_group_id := (SELECT group_id FROM workers WHERE id = NEW.worker_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER jobs_ran_group BEFORE INSERT OR UPDATE OF worker_id ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_stamp_ran_group();
