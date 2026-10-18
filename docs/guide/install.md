# Installing a farm

A ComfyFarm site has four kinds of machines. They can share hosts on a small farm.

```
 users' browsers ──▶ gateway ──▶ router ──▶ GPU workers (ComfyUI + comfyfarm_worker)
                        │          │
                        ▼          ▼
                   UI worker    Postgres
              (ComfyUI, CPU, comfyfarm_ui)
        all ComfyUI instances share one input/output/user storage (NAS)
```

| Part | What it is | Where |
|---|---|---|
| **control plane** | router + gateway + Postgres | one Linux host (containers or systemd) |
| **UI worker** | a stock ComfyUI, CPU is enough, with the `comfyfarm_ui` extension; serves the screen and user files | any host; it must not be busy (no GPU jobs) |
| **GPU workers** | stock ComfyUI with the `comfyfarm_worker` extension, started by the launcher | each GPU machine (≤ ~20 is the design target) |
| **shared storage** | one filesystem for `input`, `output`, `user` on every ComfyUI | a NAS (NFS/SMB) — required; models may be local NVMe |

## 1. Control plane (containers)

On a machine with internet access, build the image and carry it in:

```sh
docker build -f deploy/Dockerfile -t comfyfarm:latest .
docker pull postgres:17
docker save comfyfarm:latest postgres:17 | gzip > comfyfarm-images.tar.gz
```

On the control-plane host inside the network:

```sh
docker load < comfyfarm-images.tar.gz
cd deploy/compose && cp env.example .env
docker run --rm comfyfarm:latest comfyfarm secrets >> .env    # then set DB_PASSWORD and UI_WORKER_URL in .env
mkdir -p backups keys && sudo chown 10001 backups keys        # the router writes backups and its agent key here
docker compose up -d
docker compose exec router sh -c 'echo "<password>" | comfyfarm add-user <you> --admin'
```

Not `docker compose build`/`run` for the first two steps: compose refuses to read its file until DB_PASSWORD and the
two secrets are set. The database is PostgreSQL 17 because the image's `pg_dump` is 17 — a dump only restores into
a server of the same major version (`comfyfarm doctor` checks this).

Users open `http://<control-plane>:8080`. Port 8188 (router) is for the workers' launchers and for monitoring
(`/metrics`); do not publish it to users.

Two Docker habits to know on a site network. Ports Docker publishes bypass host firewalls such as ufw — restrict
8188 at the network or with `ROUTER_PORT=<host-ip>:8188`; Postgres is deliberately not published at all. And Docker
takes its networks from `172.17–31.0.0/16`, then `192.168.0.0/20`: if those ranges are routed on your site (VPNs,
other subnets), traffic to them silently stops on this host — set `"default-address-pools"` in
`/etc/docker/daemon.json` to a free range before the first `docker compose up`.

Without containers: `pip install` the package into `/opt/comfyfarm/venv`, put the settings into
`/etc/comfyfarm/{router,gateway}.env` and use `deploy/systemd/comfyfarm-{router,gateway}.service` — see
[running.md](running.md).

## 2. UI worker

A stock ComfyUI on the shared storage, started with `--multi-user --listen <ip>` and the `--input/--output/--user`
directories on the NAS. Copy `ui_ext/comfyfarm_ui` into its `custom_nodes`. It needs no GPU.

## 3. GPU workers

What a GPU host needs before setup: Linux with systemd, Python ≥ 3.10, an ssh server, the GPU driver (ComfyFarm checks
it, never installs it), and — for NFS mounts written by setup — `nfs-common`. Everything else comes from the router.

**In the dashboard: System → Workers → Set up a worker** (docs/design/14). Describe the host — address, instance name,
GPU, group, the profile to run, the paths (the artifact store at the **same path on every host**) and NFS mounts if
setup should write them. Then choose how setup gets root on the host:

- **Run a command yourself** (recommended): copy the one line the dashboard shows and run it on the host as a user who
  may use `sudo`. It downloads the setup script, checks it against the checksum shown, and runs it. The router never
  holds root on your workers.
- **Let the router log in once**: type an admin account and password for the host. The router logs in over ssh and
  runs the same script with `sudo`; the password is used for that run only. For those minutes the router has root on
  the host.

The script creates the service account (`render`/`video` groups), installs the worker tools root-owned (the router
cannot replace them), writes the unit and instance configuration, mounts NFS, installs the router's agent key with a
forced command, enables lingering — and runs the **host check**, which the dashboard shows line by line: driver and
device access, directories, every published venv runs here, clock, the router answers, the port is free. Fix what it
says and run the same command again (it is idempotent). When it is green, **Add the worker**: the dashboard adds it,
configures its instance with the ssh restart backend and the pinned host keys, assigns the profile and starts it.

Later: *Check host* on the instance (System → Rollout) and `comfyfarm doctor` run the same check through the agent.

Worker access control: put `COMFYFARM_ALLOW=<control-plane address>` in the profile's environment (with containers
the control plane talks from the host's address). Without it the dashboard shows the worker as **unsecured**. On AMD
with PyTorch ≥ 2.13 ROCm wheels also put `HSA_TOOLS_DISABLE_REGISTER=1` there — without it every worker burns one CPU
core while idle ([rollouts.md](rollouts.md#5-after-a-new-venv-watch-the-idle-worker)).

Without the dashboard (or to see what setup does): [rollouts.md](rollouts.md) §2 lists the same pieces by hand.

The router's keys directory (`COMFYFARM_KEYS_DIR`; with containers `deploy/compose/keys`, owned by uid 10001) holds
its agent key (made on first use) and `known_hosts`.

## 4. In the dashboard

System → Groups (one per set of identical workers) first, then *Set up a worker* for each GPU host (above) → Users
(accounts, which groups each may use).

## 5. Check

```sh
docker compose exec router comfyfarm doctor --ui http://<ui-worker>:8188
```

Every line should be OK: ledger, an administrator, each worker reachable and *secured*, groups agreeing on nodes and
models, GPU workers seeing files uploaded through the UI worker (shared storage), disk space. Then back up
([backup.md](backup.md)) — before anyone depends on the farm.
