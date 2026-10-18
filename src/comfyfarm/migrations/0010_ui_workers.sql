-- A UI worker (docs/design/14) is a worker row too: health, host checks and rollouts treat it like the others, the
-- dispatcher never gives it a job, and the gateway finds its address here.
ALTER TABLE workers ADD COLUMN kind text NOT NULL DEFAULT 'gpu' CHECK (kind IN ('gpu', 'ui'));
ALTER TABLE workers ADD CONSTRAINT workers_ui_has_no_group CHECK (kind = 'gpu' OR group_id IS NULL);
