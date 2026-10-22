-- A group's node definitions that could not be read again when they changed (a rollout ended before the health
-- monitor saw its workers back; a copy of a model arrived while none answered): read again on the editor's next
-- request once a worker of the group answers, instead of being served unchanged until someone refreshes by hand.
ALTER TABLE object_info_snapshots ADD COLUMN stale boolean NOT NULL DEFAULT false;
