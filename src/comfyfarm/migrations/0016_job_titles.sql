-- What a person recognises a job by in a queue (field, dev.17: several queued jobs looked alike): the title typed
-- next to Run, else the workflow's tab name; and a hint the router reads from the workflow itself — where it saves
-- (filename_prefix) and the start of its prompt.
ALTER TABLE jobs ADD COLUMN title text;
ALTER TABLE jobs ADD COLUMN hint text;
