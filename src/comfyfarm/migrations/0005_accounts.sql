-- 0005: accounts and group permissions (docs/design/08 'Accounts and permissions', D-34, D-35).

-- Users of every identity provider get a row: local users have a password; trusted-header users are created on first
-- sight without one (permissions and preferences need a row either way).
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE users ADD COLUMN prefs jsonb NOT NULL DEFAULT '{}';
-- Sessions carry the epoch they were issued in; bumping it (password change, disable, reset) ends all of them.
-- A counter, not a timestamp: gateway and database clocks need not agree.
ALTER TABLE users ADD COLUMN session_epoch int NOT NULL DEFAULT 0;

CREATE TABLE user_groups (
  username  text   NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
  group_id  bigint NOT NULL REFERENCES worker_groups(id) ON DELETE CASCADE,
  PRIMARY KEY (username, group_id)
);

-- Groups a job may run in (NULL = any: admins, development identity). Kept in step with the owner's permissions, so a
-- revoked group stops taking the owner's queued jobs at once (core/access.py).
ALTER TABLE jobs ADD COLUMN allowed_groups bigint[];

-- history page: newest first, per owner
CREATE INDEX jobs_finished ON jobs (finished_at DESC, id DESC) WHERE status IN ('completed','failed','cancelled');

-- a deleted worker's name and URL can be registered again (deletion is soft, D-06b)
ALTER TABLE workers DROP CONSTRAINT workers_name_key;
ALTER TABLE workers DROP CONSTRAINT workers_base_url_key;
CREATE UNIQUE INDEX workers_name_live ON workers (name) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX workers_url_live ON workers (base_url) WHERE deleted_at IS NULL;
