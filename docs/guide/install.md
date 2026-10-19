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

Two compose files, the same images:

| | `deploy/easy/` | `deploy/compose/` (strict) |
|---|---|---|
| first start | `docker compose up -d`, nothing to edit | secrets, `.env`, state directories by hand (below) |
| secrets | made on first start, files in the `state` volume | files on the host (`/srv/comfyfarm/secrets`, 0700) |
| ports | 8080 and 8188 on every address of the host | `<host-ip>:<port>` you choose |
| image tag | the release's default | required (`COMFYFARM_VERSION`) |
| first administrator | in the browser, with the setup code from `docker compose logs router` | the same, or `comfyfarm add-user` |
| keys, backups | in the `state` volume | `/srv/comfyfarm/{keys,backups}` |

Neither puts a secret into the environment (`docker inspect` shows none) or publishes Postgres. Easy is for a farm
behind a firewall you control; restrict 8188 to the worker hosts there — Docker publishes past ufw.

### Easy

```sh
git clone https://github.com/frozenrainy/comfyfarm && cd comfyfarm/deploy/easy    # or the release tarball
docker compose up -d                                   # pulls the release image from ghcr.io
docker compose logs router | grep "setup code"
```

Bind mounts instead of the volumes work as they are: `init` gives `./state` and `./logs` to the image's account
(uid 10001) on every start. Put them into `compose.override.yaml` next to `compose.yaml` rather than editing it —
compose merges the two (by mount target), and an upgrade is then just the new `compose.yaml`:

```yaml
services:
  init:    { volumes: ["./state:/state", "./logs:/var/log/comfyfarm"] }
  db:      { volumes: ["./db:/var/lib/postgresql/data", "./state:/state:ro"] }
  router:  { volumes: ["./state:/state", "./logs:/var/log/comfyfarm"] }
  gateway: { volumes: ["./state:/state:ro", "./logs:/var/log/comfyfarm"] }
```

Open `http://<this host>:8080`: the login page offers to create the administrator with that code (a new code on every
start until one exists). Optional settings go into a `.env` next to `compose.yaml`: `GATEWAY_PORT`, `ROUTER_PORT`,
`COMFYFARM_ARTIFACTS_MOUNT`, `COMFYFARM_USER_DIR`, `UI_WORKER_URL`, `COMFYFARM_DEFAULT_GROUP` (as in the strict file).
Closed network: pull (or `docker compose build`) outside, `docker save ghcr.io/frozenrainy/comfyfarm:<version>
postgres:17`, and `docker load` it inside next to the repository. Backups: `docker compose exec router comfyfarm backup /state/backups
--user-dir /userdata`, then copy them off the host (`docker compose cp router:/state/backups .`).

### Strict

On a machine with internet access, take the image of a release (never `latest`: the tag in `.env` is what an upgrade
changes) and carry it in:

```sh
V=0.1.0-dev.2                                                 # a release tag without the v
docker pull ghcr.io/frozenrainy/comfyfarm:$V                  # or build it: docker build -f deploy/Dockerfile -t ghcr.io/frozenrainy/comfyfarm:$V .
docker pull postgres:17
docker save ghcr.io/frozenrainy/comfyfarm:$V postgres:17 | gzip > comfyfarm-images.tar.gz
```

The published image carries a signed build provenance: `gh attestation verify oci://ghcr.io/frozenrainy/comfyfarm:$V
-R frozenrainy/comfyfarm` shows that it was built by this repository's workflow from the tagged commit.

The control-plane host needs Docker with the Compose v2 plugin (`docker compose version`; tested with Docker 29 and
Compose 2.40, Ubuntu 24.04's `docker.io` and `docker-compose-v2`), and an ssh server if the UI worker will run on it
(§2). Carry in the images and the release (the tarball, or a `git clone` of the repository at that version), then:

```sh
docker load < comfyfarm-images.tar.gz
sudo tar xzf comfyfarm-<version>.tar.gz -C /opt                # → /opt/comfyfarm; keep it there (upgrades go over it)
sudo install -d -m 700 /srv/comfyfarm/secrets                  # the three secrets, as files (never in .env)
sudo docker run --rm -u 0 -v /srv/comfyfarm/secrets:/s ghcr.io/frozenrainy/comfyfarm:$V comfyfarm secrets --write /s
cd /opt/comfyfarm/deploy/compose && sudo cp env.example .env && sudo chmod 600 .env
sudo editor .env      # COMFYFARM_VERSION=$V; ROUTER_PORT/GATEWAY_PORT as <host-ip>:<port>; COMFYFARM_ARTIFACTS_MOUNT=<ART>
                      # (the artifact store as mounted on this host: the dashboard then lists and checks published
                      # artifacts); COMFYFARM_USER_DIR=<the UI worker's user directory on this host> so that backups
                      # include the users' workflows (backup.md)
sudo mkdir -p /srv/comfyfarm/keys /srv/comfyfarm/backups && sudo chown 10001 /srv/comfyfarm/keys /srv/comfyfarm/backups
sudo chmod 700 /srv/comfyfarm/keys
docker compose up -d                                          # (the router keeps its agent key and backups there)
docker compose exec router comfyfarm add-user <you> --admin   # asks for the password (or: the browser, with the
                                                              # setup code from `docker compose logs router`)
```

The secrets are mounted into the containers as compose secrets (`/run/secrets/…`, read through `PGPASSWORD_FILE`,
`COMFYFARM_GATEWAY_SECRET_FILE`, `COMFYFARM_SESSION_KEY_FILE`), so `docker inspect` does not show them. `--write` never
replaces an existing file: the database password is fixed when Postgres first initialises its volume, and a new session
key logs everyone out. Keep a copy of `/srv/comfyfarm/secrets` and `/srv/comfyfarm/keys` where you keep backups
(backup.md) — a reinstall needs them.

**Upgrading the control plane**: `docker load` the new images, then unpack the new release **over** the old one
(`sudo tar xzf comfyfarm-<version>.tar.gz -C /opt`) — never delete `/opt/comfyfarm` first: your `.env` lives there —
set `COMFYFARM_VERSION` to the new tag and run `docker compose up -d`. Migrations run when the router starts; take a
backup before (backup.md). From a release that kept the secrets in `.env`: write each value into its file in
`/srv/comfyfarm/secrets` (`DB_PASSWORD` → `db_password`, `COMFYFARM_GATEWAY_SECRET` → `gateway_secret`,
`COMFYFARM_SESSION_KEY` → `session_key`, mode 0444, the directory 0700) and remove the three lines from `.env`.

Not `docker compose build`/`run` for the first steps: compose refuses to read its file until COMFYFARM_VERSION is set
and the three secret files exist. The database is PostgreSQL 17 because the image's `pg_dump` is 17 — a dump only restores into
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

A stock ComfyUI that serves the screen and the users' files. It needs no GPU and never runs a job; it usually lives on
the control-plane host (port 8189 there, 8188 is the router). Its host needs what §3 lists for any worker host — an ssh
server included: the router starts and checks it over ssh like the others. Set it up like a GPU worker (§3) — *Set up
a worker → UI worker* — after publishing what it runs and creating its profile (setup offers the profiles that exist):

- a node set with the two extensions: `python3 publish.py nodes <ART> farm-ui-1 --from <dir>` where `<dir>` holds
  `ui_ext/comfyfarm_ui` and `worker_ext/comfyfarm_worker` (the latter locks it to the farm's addresses);
- a CPU venv (rollouts.md §1);
- a profile: the same core as the GPU workers, the CPU venv, nodes `farm-ui-1`, flag `--cpu`, environment
  `COMFYFARM_ALLOW=<gateway address>,<router address>` — stock `--multi-user` has no login of its own, so without the
  allow-list anyone on the network could read every user's workflows. On the control-plane host with containers, the
  gateway and the router reach it from the compose network, not the host's address: allow that subnet too
  (`docker network inspect comfyfarm_default -f '{{(index .IPAM.Config 0).Subnet}}'`).

The gateway finds the UI worker by itself (`UI_WORKER_URL=auto`, the default). As a worker row it is health-checked,
host-checked and rolled out with the others, so its ComfyUI and frontend follow the GPU workers' version.

## 3. GPU workers

What a worker host needs before setup: Linux with systemd, Python ≥ 3.10, an ssh server, the GPU driver (ComfyFarm checks
it, never installs it), the system libraries the GPU wheels load (ROCm PyTorch: `libatomic1`; the host check names any
that is missing), and — for NFS mounts written by setup — `nfs-common`. Everything else comes from the router.

The shared directories (`input`, `output`, `user` on the NAS) must be writable by the service account's uid — the same
uid on every host (setup's *Service account uid*, default 47800): `chown 47800` them on the NAS once, or the host check
says so.

**In the dashboard: System → Workers → Set up a worker** (docs/design/14). Describe the host — address, instance name,
GPU, group, the profile to run, the router's address as the worker reaches it (`http://<router>:8188`), the paths (the
artifact store at the **same path on every host**) and NFS mounts **only if setup should write them** (leave the box
empty where the site has mounted the share; setup also leaves an existing mount alone). Then choose how setup gets
root on the host:

- **Run a command yourself** (recommended): copy the one line the dashboard shows and run it on the host as a user who
  may use `sudo`. It downloads the setup script, checks it against the checksum shown, and runs it. The router never
  holds root on your workers.
- **Let the router log in once**: type an admin account and password for the host. The router logs in over ssh and
  runs the same script with `sudo`; the password is used for that run only. For those minutes the router has root on
  the host. That account must be allowed to log in with a password over ssh (`PasswordAuthentication`) and to use
  `sudo` (with its password or without).

Closed the dialog while the host was running the script? *Set up a worker* offers **Continue** for unfinished setups:
a reported one goes straight to *Add the worker*; for the others the dashboard makes a new command (the old one stops
working).

The script creates the service account (`render`/`video` groups), installs the worker tools root-owned (the router
cannot replace them), writes the unit and instance configuration, mounts NFS, installs the router's agent key with a
forced command, enables lingering — and runs the **host check**, which the dashboard shows line by line: driver and
device access, directories, every published venv runs here, clock, the router answers, the port is free. Fix what it
says and run the same command again (it is idempotent). When it is green, **Add the worker**: the dashboard adds it,
configures its instance with the ssh restart backend and the pinned host keys, assigns the profile and starts it.

Later: *Check host* on the instance (System → Rollout) and `comfyfarm doctor` run the same check through the agent.

Worker access control: put `COMFYFARM_ALLOW=<control-plane address>` in the profile's environment (with containers
the control plane talks to other hosts from the host's address). Without it the dashboard shows the worker as
**unsecured**. On AMD with PyTorch ≥ 2.13 ROCm wheels also put `HSA_TOOLS_DISABLE_REGISTER=1` there — without it every
worker burns one CPU core while idle ([rollouts.md](rollouts.md#5-after-a-new-venv-watch-the-idle-worker)). The host
check shows each venv's PyTorch version (also in `<ART>/venv/<name>/freeze.txt`).

One ComfyUI instance per GPU: ComfyUI keeps models in VRAM between jobs, so two instances sharing a GPU run each other
out of memory.

Without the dashboard (or to see what setup does): [rollouts.md](rollouts.md) §2 lists the same pieces by hand.

The router's keys directory (`COMFYFARM_KEYS_DIR`; with containers `/srv/comfyfarm/keys` on the host, owned by uid
10001 — inside the router container it is `/keys`) holds its agent key (made on first use) and `known_hosts`.

## 4. In the dashboard

In this order: System → Groups (one per set of identical workers; requirements are optional — leave them empty unless
a group must only take workers with a backend or enough VRAM) → Rollout → Profiles (one for the UI worker, one per kind
of GPU worker) → *Set up a worker*: the UI worker, then each GPU host → Users (accounts, which groups each may use).
A group's node list is read from its workers the first time someone needs it; *Refresh nodes* is for later, after
custom nodes change. `COMFYFARM_DEFAULT_GROUP` (optional) is the group whose nodes a user sees before picking one;
without it, the first group the user may use.

## 5. Check

```sh
docker compose exec router comfyfarm doctor
```

It finds the UI worker by itself (`--ui <url>` only to check another one). Every line should be OK: ledger, an
administrator, each worker reachable and *secured*, groups agreeing on nodes and models, GPU workers seeing files
uploaded through the UI worker (shared storage), each host's check through its agent, disk space. Then back up
([backup.md](backup.md)) — before anyone depends on the farm.
