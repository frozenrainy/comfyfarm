-- What a workflow's last successful run executed (dev.22): ComfyUI skips the branches a lazy input does not ask for,
-- and the API prompt cannot tell which. The progress bar of the next run of the same shape weighs only those nodes.
-- shape: core.jobs.workflow_shape (node ids and types — not seeds, texts or files); ran: the prompt's node ids that
-- reported progress (a loop body's ids once, not per iteration).
CREATE TABLE workflow_runs (
  shape text PRIMARY KEY,
  ran text[] NOT NULL,
  at timestamptz NOT NULL DEFAULT now()
);
