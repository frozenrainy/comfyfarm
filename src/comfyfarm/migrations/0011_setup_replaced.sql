-- A setup whose values were corrected in the dashboard is replaced by a new one: its token stops working and it no
-- longer appears under "Continue".
ALTER TABLE worker_setups DROP CONSTRAINT IF EXISTS worker_setups_status_check;
ALTER TABLE worker_setups ADD CONSTRAINT worker_setups_status_check
  CHECK (status IN ('waiting', 'running', 'reported', 'failed', 'finished', 'replaced'));
