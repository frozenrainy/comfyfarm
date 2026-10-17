# Running the router and the gateway

## Router

```sh
export COMFYFARM_DSN=postgresql://comfyfarm:…@db/comfyfarm
export COMFYFARM_GATEWAY_SECRET=…          # shared with the gateway; identity headers are trusted only with it
export COMFYFARM_ARTIFACTS_DIR=/srv/art    # optional: lets the router check artifacts before assignments
comfyfarm serve --host 0.0.0.0 --port 8188 --log-dir /var/log/comfyfarm
```

Migrations run at start (under an advisory lock, so two routers starting at once do not race). Logs go to stdout for
the service manager; with `--log-dir` also to `comfyfarm.log` (rotated at 20 MB, 5 kept) and `error.log`, which holds
only real failures — point your alerting at that file.

Environment: `COMFYFARM_DSN` (required), `COMFYFARM_INSTANCE` (name of this router; part of the worker event
identity), `COMFYFARM_GATEWAY_SECRET`, `COMFYFARM_DEFAULT_GROUP`, `COMFYFARM_ARTIFACTS_DIR`,
`COMFYFARM_DEV_IDENTITY` (development only, loopback: every request is this user).

First admin: `echo 'the-password' | comfyfarm add-user alice --admin`. Everything else is done in the dashboard.

## Gateway

```sh
export COMFYFARM_GATEWAY_SECRET=…          # the same value as the router's
export COMFYFARM_SESSION_KEY=…             # 32+ random bytes; signs login cookies
python -m comfyfarm.gateway --router http://127.0.0.1:8188 --ui http://ui-worker:8188 --port 8080
```

Browsers talk only to the gateway. `--provider trusted-header` takes the identity from an authenticating reverse
proxy in front of it instead of the login page (see [08](../design/08-gateway-and-identity.md)).

## Checking a farm

```sh
comfyfarm doctor --ui http://ui-worker:8188
```

Answers, one line each with OK / WARN / FAIL: ledger reachable and writable, number of administrators, stuck
rollouts, every worker (reachable? locked down with `COMFYFARM_ALLOW`? which group?), whether the workers of a group
agree on nodes and models, whether GPU workers see files uploaded through the UI worker (shared storage), disk space,
published artifacts, backup tooling. Exit code 1 when anything FAILs — usable from a cron job.

Backups: [backup.md](backup.md). Upgrading workers: [rollouts.md](rollouts.md).
