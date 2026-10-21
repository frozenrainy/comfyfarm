-- A rollout step copies the target's artifacts to a host that keeps copies, before it drains the worker (docs/design/16).
ALTER TABLE rollout_steps DROP CONSTRAINT IF EXISTS rollout_steps_status_check;
ALTER TABLE rollout_steps ADD CONSTRAINT rollout_steps_status_check
  CHECK (status IN ('pending', 'fetching', 'draining', 'restarting', 'starting', 'verifying', 'done',
                    'failed', 'reverting', 'reverted', 'revert_failed', 'skipped'));
