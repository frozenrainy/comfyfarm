# Backup and restore

A farm has three kinds of state. Back up all three; each is lost differently.

| What | Where | Lost when |
|---|---|---|
| **Ledger** — users, groups, permissions, jobs and their workflows, history, notes, profiles, instances, rollouts, audit log, site settings | Postgres | the database host dies |
| **User data** — saved workflows, ComfyUI settings per user | the UI worker's user directory (`--user-directory`; on shared storage) | the storage dies, or someone deletes a folder |
| **Artifacts** — published ComfyUI releases, venvs, node sets | the artifact store `<ART>` | the store dies; they are large and reproducible from recipes |

Not in any backup on purpose: the **gateway secret, session key, ssh keys and agent token files**. They live in the
service configuration (environment files, key files) and should be kept with it — a backup that carries them is a
second copy to protect. Inputs and outputs (user uploads, rendered results) are on shared storage and are the
site's business; ComfyFarm only references them.

## Backup

```sh
export COMFYFARM_DSN=postgresql://…           # the router's connection string
comfyfarm backup /backups --user-dir /srv/comfy/user     # --pg-bin <dir> if pg_dump is not on PATH
```

Writes `/backups/comfyfarm-backup-<UTC time>/` with `ledger.dump` (pg_dump, custom format), `userdata.tar.gz`,
`artifacts.json` (names and manifests of everything published — enough to know what to re-publish) and
`MANIFEST.json`. Run it from cron or a timer; the router keeps running. Keep several generations on another host.

The artifact store itself: copy `<ART>` with the site's usual file backup, or accept re-publishing from recipes after
a loss (publish keeps names unique, so a restored ledger's profiles point at the same names).

## Restore

1. Stop the router (and the gateway).
2. `comfyfarm restore /backups/comfyfarm-backup-<time> --yes --user-dir /srv/comfy/user` — the live tables are
   dropped and recreated from the dump; the user directory is replaced.
3. Start the router. Run `comfyfarm doctor`: workers come back online by themselves; a rollout that was running at
   backup time shows as *interrupted* — abort it.

Sessions survive a restore (the cookie's epoch is in the ledger); passwords changed after the backup are back to the
old ones — tell the users.

## One database, not a cluster

A single Postgres with backups and this runbook is the supported setup. High availability (two routers, replicated
databases, a virtual IP) is a module for later; the requirements it must meet are in [01](../design/01-architecture.md).
For a small farm, a restore that takes ten minutes is safer than a failover nobody has rehearsed.
