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
| **control plane** | router + gateway + Postgres, and with compose the builder and the UI worker container | one Linux host (containers) |
| **UI worker** | a stock ComfyUI, CPU is enough, with the `comfyfarm_ui` extension; serves the screen and user files | with compose: the `ui` container on the control plane; else any host that is not busy (no GPU jobs) |
| **GPU workers** | stock ComfyUI with the `comfyfarm_worker` extension, started by the launcher | each GPU machine (≤ ~20 is the design target) |
| **shared storage** | one filesystem for `input`, `output`, `user` on every ComfyUI | a NAS (NFS/SMB) — required; models may be local NVMe |
| **artifact store** | ComfyUI, venvs and node sets the builder publishes; read by every worker | at the **same absolute path** everywhere (a share or a copy) |

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

What the first start does by itself (docs/design/15), in about a minute or two:

1. `init` makes the secrets and gives each directory to its account: the artifact store to the builder (uid 10002),
   the shared storage to the workers' account (uid 47800) — only directories that are new or already theirs; one that
   holds someone else's files is left alone and named in `docker compose logs init`.
2. The **builder** builds ComfyUI on the CPU for the UI worker (this host needs internet for it: ~2.5 GB).
3. The router registers the **UI worker container**, gives it that profile and starts it. Workflows open in the
   browser now — before any GPU worker exists, with the node definitions of the UI worker.

Where things live, all changeable in a `.env` next to `compose.yaml`:

| Setting | Default | What |
|---|---|---|
| `COMFYFARM_ARTIFACTS_MOUNT` | `/srv/comfyfarm/art` | the artifact store — GPU workers must see it at this **same path** |
| `COMFYFARM_SHARED` | `/srv/comfyfarm/shared` | `input`, `output`, `user` below it — the storage GPU workers use too, at the same path |
| `COMFYFARM_UID` | `47800` | the workers' service account uid (*Set up a worker*): the shared storage belongs to it |
| `COMFYFARM_BUILD_NODE_SOURCES` | `https://github.com/` | where node packs picked in the dashboard may come from; empty = none |
| `COMFYFARM_FIRST_RECIPE` | `rocm-gfx1201` | whose UI half the first start builds (the UI half is the same in every recipe) |
| `ROUTER_PORT`, `GATEWAY_PORT` | `8188`, `8080` | `<host-ip>:<port>` publishes on one address only |

On one machine with a GPU, that machine is also the GPU worker (§3) and nothing needs sharing. With more machines,
mount the artifact store and the shared storage on every GPU worker at the same paths (NFS, SMB — the site's), or put
them on a NAS and point both settings there.

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

(The builder's and the UI worker's own volumes — `builder-home`, `ui-home` — hold caches and local copies only; the
artifact store and the shared storage are host paths already.)

Open `http://<this host>:8080`: the login page offers to create the administrator with that code (a new code on every
start until one exists). Optional settings go into a `.env` next to `compose.yaml`: `GATEWAY_PORT`, `ROUTER_PORT`,
`COMFYFARM_DEFAULT_GROUP`, `UI_WORKER_URL` and those above.
Closed network: pull (or `docker compose build`) outside, `docker save ghcr.io/frozenrainy/comfyfarm:<version>
postgres:17`, and `docker load` it inside next to the repository.

The `state` volume holds the secrets, the router's agent key (every worker trusts it) and the backups; `db` the
ledger. `docker compose down` keeps them, **`docker compose down -v` deletes them** — then every worker must be set up
again. Upgrade: check out the new release (`git fetch --tags && git checkout v<version>`) and `docker compose up -d`;
`init` runs again and changes nothing. Backups: `docker compose exec router comfyfarm backup /state/backups --user-dir /userdata` (the users'
workflows: `COMFYFARM_SHARED/user`, mounted read-only into the router); copy them off the host
(`docker compose cp router:/state/backups .`).
With bind mounts the state directory is the service account's alone (0700): read it as root.

### Strict

On a machine with internet access, take the image of a release (never `latest`: the tag in `.env` is what an upgrade
changes) and carry it in:

```sh
V=0.1.0-dev.11                                                 # a release tag without the v
docker pull ghcr.io/frozenrainy/comfyfarm:$V                  # or build it: docker build -f deploy/Dockerfile -t ghcr.io/frozenrainy/comfyfarm:$V .
docker pull postgres:17
docker save ghcr.io/frozenrainy/comfyfarm:$V postgres:17 | gzip > comfyfarm-images.tar.gz
```

The published image carries a signed build provenance: `gh attestation verify oci://ghcr.io/frozenrainy/comfyfarm:$V
-R frozenrainy/comfyfarm` shows that it was built by this repository's workflow from the tagged commit.

The control-plane host needs Docker with the Compose v2 plugin (`docker compose version`; tested with Docker 29 and
Compose 2.40, Ubuntu 24.04's `docker.io` and `docker-compose-v2`), and an ssh server if the UI worker will run on it
(§2). Carry in the images and the repository at that release (`git clone --branch v$V …`, or the tag's source
archive from GitHub, which unpacks as `comfyfarm-<version>/`), and do the rest as root — the state is root's:

```sh
sudo docker load < comfyfarm-images.tar.gz
sudo mv comfyfarm-$V /opt/comfyfarm                            # keep it there: an upgrade is a checkout in that directory
sudo install -d -m 700 /srv/comfyfarm/secrets                  # the three secrets, as files (never in .env)
sudo docker run --rm -u 0 -v /srv/comfyfarm/secrets:/s ghcr.io/frozenrainy/comfyfarm:$V comfyfarm secrets --write /s
cd /opt/comfyfarm/deploy/compose && sudo cp env.example .env && sudo chmod 600 .env
sudo editor .env      # COMFYFARM_VERSION=$V; ROUTER_PORT/GATEWAY_PORT as <host-ip>:<port>; COMFYFARM_ARTIFACTS_MOUNT=<ART>
                      # (the artifact store as mounted on this host: the dashboard then lists and checks published
                      # artifacts); COMFYFARM_USER_DIR=<the UI worker's user directory on this host> so that backups
                      # include the users' workflows (backup.md)
sudo mkdir -p /srv/comfyfarm/keys /srv/comfyfarm/backups && sudo chown 10001 /srv/comfyfarm/keys /srv/comfyfarm/backups
sudo chmod 700 /srv/comfyfarm/keys
sudo docker compose up -d                                     # pulls the image named in .env; never builds (pull_policy)
sudo docker compose exec router comfyfarm add-user <you> --admin   # asks for the password (or: the browser, with the
                                                                   # setup code from `docker compose logs router`)
```

The secrets are mounted into the containers as compose secrets (`/run/secrets/…`, read through `PGPASSWORD_FILE`,
`COMFYFARM_GATEWAY_SECRET_FILE`, `COMFYFARM_SESSION_KEY_FILE`), so `docker inspect` does not show them. `--write` never
replaces an existing file: the database password is fixed when Postgres first initialises its volume, and a new session
key logs everyone out. Keep a copy of `/srv/comfyfarm/secrets` and `/srv/comfyfarm/keys` where you keep backups
(backup.md) — a reinstall needs them.

**Upgrading the control plane**: `docker load` the new image, check out the new release in `/opt/comfyfarm`
(`git fetch --tags && git checkout v<version>`, or unpack the new archive and move your `.env` across) — never delete
the directory with `.env` in it — set `COMFYFARM_VERSION` to the new tag and run `sudo docker compose up -d`. Migrations run when the router starts; take a
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

A stock ComfyUI that serves the screen and the users' files. It needs no GPU and never runs a job.

**With compose it is the `ui` container** (easy; strict with `COMPOSE_PROFILES=farm`) and needs nothing from you: the
first start builds and starts it (§1). It is reachable only from the router and the gateway, on the compose network;
the router restarts, checks and rolls it out through its agent like any worker. Removed in the dashboard, it is not
added again — to run the UI worker on a host instead, remove it, then set the host up as below (`docker compose stop
ui` then frees its resources).

**On a host** (the strict deployment without the profile, or a machine of its own): port 8189 on the control-plane host
(8188 is the router). Its host needs what §3 lists for any worker host — an ssh server included: the router starts and
checks it over ssh like the others. Set it up like a GPU worker (§3) — *Set up a worker → UI worker* — after
publishing what it runs and creating its profile (setup offers the profiles that exist):

- a node set with the two extensions: `python3 publish.py nodes <ART> farm-ui-1 --from <dir>` where `<dir>` holds
  `ui_ext/comfyfarm_ui` and `worker_ext/comfyfarm_worker` (the latter locks it to the farm's addresses);
- a CPU venv (rollouts.md §1);
- a profile: the same core as the GPU workers, the CPU venv, nodes `farm-ui-1`, flag `--cpu`, environment
  `COMFYFARM_ALLOW=<gateway address>,<router address>` — stock `--multi-user` has no login of its own, so without the
  allow-list anyone on the network could read every user's workflows. On the control-plane host with containers, the
  gateway and the router reach it from the compose network, not the host's address: allow that subnet too
  (`docker network inspect comfyfarm_default -f '{{(index .IPAM.Config 0).Subnet}}'`).

The gateway finds the UI worker by itself (`UI_WORKER_URL=auto`, the default). Until a GPU worker of a group answers,
the editor takes its node definitions from the UI worker — it runs the same node packs on the CPU — so workflows open
and the node packs show before any GPU worker exists (a pack marked `"ui": false` appears once a GPU worker answers). As a worker row it is health-checked,
host-checked and rolled out with the others, so its ComfyUI and frontend follow the GPU workers' version.

## 3. GPU workers

What a worker host needs before setup: Linux with systemd, Python ≥ 3.10, an ssh server, the GPU driver (ComfyFarm checks
it, never installs it), the system libraries the GPU wheels load (ROCm PyTorch: `libatomic1`; the host check names any
that is missing), and its storage **already mounted**: the shared directories and the artifact store. Setup never
mounts anything or edits `/etc/fstab` — storage is infrastructure, the site's (NFS, SMB, a local disk for a single
host); the host check verifies the paths. Everything else comes from the router.

The service account is its own: keep the default `comfyfarm` and setup creates it. It runs ComfyUI, and its
`authorized_keys` holds only the router's restart key — setup refuses a person's login account (home under `/home`,
sudo rights), never replaces keys it did not write, and stops if a unit in that account's home would take the place
of the one it installs. The admin account you type for *Let the router log in once* is a different one: yours.

The shared directories (`input`, `output`, `user` on the NAS) must be writable by the service account's uid — the same
uid on every host (setup's *Service account uid*, default 47800): `chown 47800` them on the NAS once, or the host check
says so.

**In the dashboard: System → Workers → Set up a worker** (docs/design/14). Describe the host — address, instance name,
GPU, group, the profile to run, the router's address as the worker reaches it (`http://<router>:8188`), the paths (the
artifact store at the **same path on every host**). Hover the **?** next to a field for what it is for. Then choose
how setup gets root on the host:

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

A value was wrong — the router address, a path? **Correct the values** (in the setup dialog, before the script has run
or after it failed) opens the form again with everything filled in; the corrected setup replaces the old one, whose
command stops working. Fixing the *host* instead (a package, a permission) needs no new setup: run the same command
again.

What setup does with what is already on the host — it never overwrites or deletes something it did not make:

- **Service account**: an existing service account is used (the log says so); a different uid stops setup. A person's
  login account is refused before anything changes.
- **Directories**: existing ones are used as they are — never emptied, never given to another owner; the host check
  says whether the service account can write them.
- **`authorized_keys`**: setup's own file is replaced; one it did not write stops setup.
- **Artifact store with builds on**: an empty one is handed to the build account; one with contents stops setup with
  the command to hand it over.
- **ComfyFarm's own files** (tools, unit, configuration): rewritten every run — that is how *Set up again* upgrades.

The script creates the service account (`render`/`video` groups), installs the worker tools root-owned (the router
cannot replace them), writes the unit and instance configuration, installs the router's agent key with a
forced command, enables lingering — and runs the **host check**, which the dashboard shows line by line: driver and
device access, directories, every published venv runs here, clock, the router answers, the port is free. Fix what it
says and run the same command again (it is idempotent). When it is green, **Add the worker**: the dashboard adds it,
configures its instance with the ssh restart backend and the pinned host keys, assigns the profile and starts it.
Added with *assign later* (a new farm has no profile yet)? Once there is one, *System → Rollout*: **Assign** offers
*Assign and start* for a worker that is not running, and its row has **Start**. A running worker changes what it runs
through a rollout, which waits for its job first.

Later: *Check host* on the instance (System → Rollout) and `comfyfarm doctor` run the same check through the agent.

**Set up again** (System → Workers, on the worker's row) runs setup once more for a host that is already a worker:
after a router upgrade (new worker tools), to change its address, group, profile or service account, or to turn
*builds from the dashboard* on or off. Its name and instance stay; the last step is *Update the worker*, which also
restarts a running worker so that the new tools and environment take effect — through a rollout of its own profile,
so it finishes its job first. *Restart* on its row in System → Rollout does the same at any time.

With compose, the artifact store is the builder's (`COMFYFARM_ARTIFACTS_MOUNT`): give the GPU host that path, and
*Set up a worker* the same path as its artifact store. The rest of this paragraph is for farms without the builder.
**Allow builds from the dashboard on this host** (in *Set up a worker*): the host then builds recipes on request from
*System → Rollout → Profiles → From a recipe* ([rollouts.md](rollouts.md) §1) — as a separate build account that
owns the artifact store, never as the service account. It needs internet and git on that host (setup brings a pinned uv, checked against its release
checksum, into `/opt/comfyfarm/worker/bin`), and an artifact
store that is empty or already that account's ([design 14](../design/14-worker-setup.md)).

Worker access control: setup allows the router's address as the host reaches it (and, with the router on that very
host, Docker's bridge networks) — `COMFYFARM_ALLOW_NETWORK` in `/etc/comfyfarm/<instance>.env`. A profile's
`COMFYFARM_ALLOW` adds to it. A worker that allows nobody shows as **unsecured** in the dashboard; one that turns
requests away says from where (*refused … the last from <address>*) — if that is the gateway or the router (a worker
that reaches them over a network setup did not guess), add the address there. On AMD with PyTorch ≥ 2.13 ROCm wheels also put `HSA_TOOLS_DISABLE_REGISTER=1` there — without it every
worker burns one CPU core while idle ([rollouts.md](rollouts.md#5-after-a-new-venv-watch-the-idle-worker)). The host
check shows each venv's PyTorch version (also in `<ART>/venv/<name>/freeze.txt`).

One ComfyUI instance per GPU: ComfyUI keeps models in VRAM between jobs, so two instances sharing a GPU run each other
out of memory.

Without the dashboard (or to see what setup does): [rollouts.md](rollouts.md) §2 lists the same pieces by hand.

The router's keys directory (`COMFYFARM_KEYS_DIR`; with containers `/srv/comfyfarm/keys` on the host, owned by uid
10001 — inside the router container it is `/keys`) holds its agent key (made on first use) and `known_hosts`.

## 4. In the dashboard

In this order: System → Groups (one per set of identical workers; requirements are optional — leave them empty unless
a group must only take workers with a backend or enough VRAM) → Rollout → **Node packs** (the custom nodes, optional;
[custom-nodes.md](custom-nodes.md)) → **Build** for your kind of GPU → *From a recipe* (the GPU and UI profiles) →
roll the UI profile out to the UI worker → *Set up a worker*: each GPU host, with the GPU profile → Users (accounts,
which groups each may use). Without the builder: Profiles (one for the UI worker, one per kind of GPU worker) → *Set up
a worker*: the UI worker, then each GPU host.
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
