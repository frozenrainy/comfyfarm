# Running the router and the gateway

## Router

```sh
export COMFYFARM_DSN=postgresql://comfyfarm:…@db/comfyfarm
export COMFYFARM_GATEWAY_SECRET=…          # shared with the gateway; identity headers are trusted only with it
export COMFYFARM_ARTIFACTS_DIR=/srv/art    # optional: lets the router check artifacts before assignments
comfyfarm serve --host 0.0.0.0 --port 8188 --log-dir /var/log/comfyfarm
```

Migrations run at start (under an advisory lock, so two routers starting at once do not race). Logs go to stdout for
the service manager; with `--log-dir` also to `router.log` (rotated at 20 MB, 5 kept) and `router-error.log`, which holds
only real failures — point your alerting at that file.

Environment: `COMFYFARM_DSN` (required; the password may instead be in `PGPASSWORD`), `COMFYFARM_INSTANCE` (name of this router; part of the worker event
identity), `COMFYFARM_GATEWAY_SECRET`, `COMFYFARM_DEFAULT_GROUP`, `COMFYFARM_ARTIFACTS_DIR`, `COMFYFARM_KEYS_DIR` (restart backends may only reference ssh
keys and agent token files inside it; default `/etc/comfyfarm/keys`),
`COMFYFARM_DEV_IDENTITY` (development only, loopback: every request is this user).

First admin: `echo 'the-password' | comfyfarm add-user alice --admin`. Everything else is done in the dashboard.

## Gateway

```sh
export COMFYFARM_GATEWAY_SECRET=…          # the same value as the router's
export COMFYFARM_SESSION_KEY=…             # 32+ random bytes; signs login cookies
python -m comfyfarm.gateway --router http://127.0.0.1:8188 --ui http://ui-worker:8188 --port 8080
```

With the systemd unit (`deploy/systemd/comfyfarm-gateway.service`) these go into `/etc/comfyfarm/gateway.env`
together with `UI_WORKER_URL=auto` (the UI worker set up in the dashboard; or a fixed URL). With `--log-dir` it writes `gateway.log` and
`gateway-error.log` (the router's files are `router.*`; the units create `/var/log/comfyfarm` themselves).

Browsers talk only to the gateway. `--provider trusted-header` takes the identity from an authenticating reverse
proxy in front of it instead of the login page (see [08](../design/08-gateway-and-identity.md)). The proxy's
address is what the gateway sees on the connection (`--trusted-proxy`); `X-Forwarded-For` is not read. A TCP proxy
in front that hides the client address makes every client look like that proxy — see design 09, "Proxies that hide the
client address".

## Checking a farm

```sh
comfyfarm doctor --ui http://ui-worker:8188
```

Answers, one line each with OK / WARN / FAIL: ledger reachable and writable, number of administrators, stuck
rollouts, every worker (reachable? locked down with `COMFYFARM_ALLOW`? which group?), whether the workers of a group
agree on nodes and models, whether GPU workers see files uploaded through the UI worker (shared storage), disk space,
published artifacts, backup tooling. Exit code 1 when anything FAILs — usable from a cron job.

**Memory of the workers**: System → Workers (and, for users, the cards of their groups on the dashboard) shows each
worker's host RAM in use, swap with how fast it is being read and written, and VRAM in use with the part ComfyUI holds.
A job that crawls while swap is read at tens of MB/s does not fit in that host's RAM. The device's VRAM use and swap
need worker extension 0.11 (*Build*, then roll the GPU and UI profiles out); until then RAM shows from ComfyUI's own
report. The same numbers are on `/metrics` (`comfyfarm_worker_*`) for a history in Prometheus/Grafana.

Backups: [backup.md](backup.md). Upgrading workers: [rollouts.md](rollouts.md).
