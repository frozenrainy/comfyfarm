# Installing a farm

The short way: one Linux server with a GPU and Docker, with internet on it for the builds. [First day](#first-day)
takes it from nothing to an artist running a workflow; the rest of this page is the detail and the choices.

```
 users' browsers ──▶ gateway ──▶ router ──▶ GPU workers (ComfyUI + comfyfarm_worker)
                        │          │
                        ▼          ▼
                   UI worker    Postgres
              (ComfyUI, CPU, comfyfarm_ui)
        all ComfyUI instances share one input/output/user storage
```

| Part | What it is | Where |
|---|---|---|
| **control plane** | router + gateway + Postgres, the builder and the UI worker (containers) | one Linux host with Docker |
| **UI worker** | ComfyUI on the CPU with the `comfyfarm_ui` extension: the screen and the users' files | the `ui` container (or a host of its own, §2) |
| **GPU workers** | ComfyUI with the `comfyfarm_worker` extension, started by the launcher — a systemd service on the host, not a container | each GPU machine (the control-plane host, too) |
| **shared storage** | `input`, `output`, `user` for every ComfyUI | one machine: a local directory (the default); more: a NAS or an NFS/SMB share |
| **artifact store** | ComfyUI, venvs and node sets the builder publishes | on the control plane; each GPU machine gets **copies** at the same path (or mounts it) |
| **models** | checkpoints, LoRAs, VAEs … in ComfyUI's folder layout | on each GPU worker (local disk is fine); the same files on every worker of a group |

**Words used here.** A *recipe* says which ComfyUI and which PyTorch to build for a kind of GPU — `rocm-gfx1201` for AMD
RDNA4 (Radeon AI PRO R9700, RX 9070), `cuda-cu130` for NVIDIA (RTX 20xx–50xx, driver 580 or newer). A *build* turns a
recipe and your node packs into published *artifacts* and leaves a *template*; *From a recipe* turns a template into
*profiles* — what a worker runs. A *group* is workers with the same GPU, nodes and models; users are allowed groups. An
*instance* is one ComfyUI on a host.

## First day

One GPU server that is both control plane and worker, all defaults. The dashboard's *Getting started* list (on the
Dashboard page, for administrators) shows where you are.

1. **Control plane** (§1): in `deploy/easy`, `docker compose up -d`, then
   `docker compose logs -f router | grep -m1 "setup code"`. Open `http://<this server>:8080` and create the
   administrator with that code.
2. **UI worker**: nothing to do — the first start builds it and starts it (a minute or two; *Getting started* says when).
   ComfyUI opens in the browser from then on.
3. **A build for your GPU**: *System → Rollout*. Optionally *Add node pack* for each custom node pack you want
   ([custom-nodes.md](custom-nodes.md)). Then *Build* — the recipe of your GPU — and *From a recipe* → *Create the
   profiles*. If you added node packs, roll the UI profile out to the UI worker (*Start rollout*).
4. **The GPU worker**: put your models under `/srv/comfyfarm/models` (`checkpoints/`, `loras/`, `vae/` …, readable by
   everyone; setup makes the directory if it is not there, and the host check warns while it is empty).
   *System → Workers → Set up a worker*: the form is filled for this server — its address, the router, the paths, port
   8288 (the router has 8188), the newest GPU profile, and a group `gpu` made with the worker. ComfyUI listens on every
   address (`0.0.0.0`; its allow-list refuses all but the router and the gateway) — put this host's address in
   *ComfyUI listens on* if nothing may listen on all addresses. Run the command it shows (as a user with sudo), *Add the
   worker*. It starts.
5. **Artists**: *System → Users → New user* — every group is ticked. They open `http://<this server>:8080`.
6. **Check**: `docker compose exec router comfyfarm doctor`, and one workflow as an artist. ComfyUI's own first
   workflow names models you may not have: open *Templates*, take a basic image generation one, and pick your
   checkpoint in its loader. The image shows in ComfyUI's queue and in the dashboard's *History*.

More GPU machines: §3, *Adding a GPU machine*. A different kind of GPU: a second group, and a build with its recipe.

Two Docker habits, before the first `up`. Ports Docker publishes bypass host firewalls such as ufw: by default 8080
(users) and 8188 (workers, monitoring) are open on every address — set `GATEWAY_PORT` / `ROUTER_PORT` to
`<host-ip>:<port>` in `.env` and let only the worker hosts reach 8188 (§1). And Docker takes its networks from
`172.17–31.0.0/16`, then `192.168.0.0/20`: if those ranges are routed on your site (VPNs, other subnets), set
`"default-address-pools"` in `/etc/docker/daemon.json` to a free range first. 8080 is plain HTTP: put a TLS proxy in
front of it if users come from outside a trusted network.

## 1. Control plane (containers)

Two compose files, the same images:

| | `deploy/easy/` | `deploy/compose/` (strict) |
|---|---|---|
| first start | `docker compose up -d`, nothing to edit | secrets, `.env`, directories by hand (below) |
| data | one directory, `/srv/comfyfarm/data` (`COMFYFARM_DATA`) | `/srv/comfyfarm/{secrets,keys,backups}` and volumes |
| ports | 8080 and 8188 on every address of the host | `<host-ip>:<port>` you choose |
| image tag | the release's default | required (`COMFYFARM_VERSION`) |
| builder, UI worker | yes; node packs from `https://github.com/` | with `COMPOSE_PROFILES=farm`; node packs only from sources you name |
| first administrator | in the browser, with the setup code from the router's log | the same, or `comfyfarm add-user` |

Neither puts a secret into the environment (`docker inspect` shows none) or publishes Postgres.

### Easy

```sh
git clone https://github.com/frozenrainy/comfyfarm && cd comfyfarm/deploy/easy    # or the release tarball
docker compose up -d                                   # pulls the release image from ghcr.io
docker compose logs -f router | grep -m1 "setup code"  # waits for the router; Ctrl-C after it printed
```

Open `http://<this host>:8080`: the login page offers to create the administrator with that code (a new code on every
start until one exists). What the first start does by itself (docs/design/15):

1. `init` makes the secrets and gives each directory to its account: the artifact store to the builder (uid 10002),
   the shared storage to the workers' account (uid 47800) — only directories that are new or already theirs; one that
   holds someone else's files is left alone and named in `docker compose logs init`.
2. The **builder** builds ComfyUI on the CPU for the UI worker — this host needs internet for it (~2.5 GB).
3. The router registers the **UI worker container**, gives it that profile and starts it. *Getting started* (Dashboard)
   and *System → Rollout* show the state; workflows open in the browser once it is ready. The router's log says
   `worker ui (#1) has not answered yet … — normal while it is set up, built or starting` until then, and
   `worker ui (#1) answers — its first answer` when it is up.

Settings, all optional, in a `.env` next to `compose.yaml`:

| Setting | Default | What |
|---|---|---|
| `GATEWAY_PORT`, `ROUTER_PORT` | `8080`, `8188` | `<host-ip>:<port>` publishes on one address only |
| `COMFYFARM_DATA` | `/srv/comfyfarm/data` | everything this control plane keeps: secrets, keys, database, logs, caches — on an SSD (*Later*, *Disks*) |
| `COMFYFARM_ARTIFACTS_MOUNT` | `/srv/comfyfarm/art` | the artifact store — GPU workers see it at this **same path** |
| `COMFYFARM_MODELS_MOUNT` | `/srv/comfyfarm/model-store` | the model store: models added in *System → Models* ([models.md](models.md)) — hard disks are fine |
| `COMFYFARM_SHARED` | `/srv/comfyfarm/shared` | `input`, `output`, `user` below it — the storage GPU workers use too, at the same path |
| `COMFYFARM_UID` | `47800` | the workers' service account uid (*Set up a worker*): the shared storage belongs to it |
| `COMFYFARM_BUILD_NODE_SOURCES` | `https://github.com/` | where node packs picked in the dashboard may come from; empty = none |
| `COMFYFARM_BUILD_NODE_ARCHIVES` | `1` | node packs uploaded as archives ([custom-nodes.md](custom-nodes.md)); empty = none |
| `COMFYFARM_FIRST_RECIPE` | `rocm-gfx1201` | whose UI part the first start builds — the same in every recipe, so this is not a GPU choice |
| `COMFYFARM_DEFAULT_GROUP` | (none) | the group whose nodes a user sees before picking one |

#### Later

- **Data**: `COMFYFARM_DATA` holds the secrets, the router's agent key (every worker trusts it), the database and
  the logs. Back it up (below); delete it only to start over — then every worker must be set up again.
- **Disks**: put `COMFYFARM_DATA` on an SSD; the artifact store (`COMFYFARM_ARTIFACTS_MOUNT`) and the model store
  (`COMFYFARM_MODELS_MOUNT`) may sit on hard disks.
  A build writes tens of thousands of files into the store, and on the same hard disk the database's commits wait
  behind them — on one HDD a single statement waited over 30 s during a first start, and the router skipped work and
  said `… took over 30 s — its disk is busy`. The database is small (tens of MB); the builder's caches in the same
  directory (a few GB) make builds faster there too. To move it: `docker compose down`, `sudo cp -a` the directory
  to the new place (owners matter: the database's files are uid 999's), set `COMFYFARM_DATA` in `.env`,
  `docker compose up -d`.
- **Upgrade**: check out the new release (`git fetch --tags && git checkout v<version>`) and `docker compose up -d`;
  `init` runs again and changes nothing; the database migrates when the router starts.
- **From 0.1.0-dev.10 or earlier** (easy only): those kept the data in Docker volumes; 0.1.0-dev.11 and later read
  `COMFYFARM_DATA`. Copy the volumes before the new release's first start — otherwise it starts as a new, empty farm
  (the volumes stay untouched; stop it, `sudo rm -rf /srv/comfyfarm/data`, and copy then):

  ```sh
  docker compose down                     # still the old release; not -v
  git fetch --tags && git checkout v<version>                     # the new release
  img=$(docker compose config --images | grep comfyfarm | head -1)  # its image
  sudo mkdir -p /srv/comfyfarm/data       # or your COMFYFARM_DATA
  for v in state db logs builds ui-agent builder-home ui-home; do
    docker run --rm --user 0:0 --network none -v comfyfarm_$v:/from:ro -v /srv/comfyfarm/data:/to \
      --entrypoint sh "$img" -c "mkdir -p /to/$v && cp -a /from/. /to/$v/"
  done
  docker compose up -d
  ```

  Sessions, accounts, workers and the UI worker carry over (rehearsed from 0.1.0-dev.10). Once it runs, the old volumes
  can go: `docker volume rm comfyfarm_state comfyfarm_db …`.
- **Backups**: `docker compose exec router comfyfarm backup /state/backups --user-dir /userdata` (the users' workflows:
  `COMFYFARM_SHARED/user`, mounted read-only into the router); the result is in `COMFYFARM_DATA/state/backups` —
  copy it off the host ([backup.md](backup.md)).
- **Closed network**: the first start, every *Build* and every node pack lookup need internet on this host. Without
  it: pull the images outside, `docker save ghcr.io/frozenrainy/comfyfarm:<version> postgres:17`, `docker load` them
  inside, and publish the artifacts on a machine that has internet with `publish.py recipe`, then carry the artifact
  store in ([rollouts.md](rollouts.md) §1).

### Strict

On a machine with internet access, take the image of a release (never `latest`: the tag in `.env` is what an upgrade
changes) and carry it in:

```sh
V=0.1.0-dev.18                                                 # a release tag without the v
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

The two Docker habits in [First day](#first-day) apply here too.

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

A GPU worker is ComfyUI as a systemd user service on the host — not a container; the host needs no Docker. What it
needs before setup: Linux with systemd, Python ≥ 3.10, an ssh server, the GPU driver (AMD: the amdgpu kernel driver —
ROCm itself comes inside the venv; NVIDIA: the driver, 580 or newer for `cuda-cu130`; ComfyFarm checks it, never
installs it), the system libraries the GPU wheels load (ROCm PyTorch: `libatomic1`; the host check names any that is
missing), and its storage **already mounted**: the shared directories and the artifact store. Setup never mounts
anything or edits `/etc/fstab` — storage is the site's (a local directory on one machine, NFS or SMB for more); the
host check verifies the paths. Everything else comes from the router.

**Models**: ComfyUI's folder layout (`checkpoints/`, `loras/`, `vae/`, `upscale_models/` …) under the worker's
*Models directory* (default `/srv/comfyfarm/models`), readable by the service account (it never writes there). Every
worker of a group needs the same files under the same names — a workflow names its models — so give them a share
(read-only is fine) or copy them. The UI worker needs none: the editor lists the GPU workers' models. Setup makes the
directory when it is missing (root's, mode 755: you put the files there, the service account reads them); the host check
fails while it is missing and warns while it holds no model. Models from Hugging Face can come from the dashboard
instead (*System → Models*): the farm copies them to the workers of the groups that use them, into a *Model store* of
their own next to this directory ([models.md](models.md)).

The service account is its own: keep the default `comfyfarm` and setup creates it. It runs ComfyUI, and its
`authorized_keys` holds only the router's restart key — setup refuses a person's login account (home under `/home`,
sudo rights), never replaces keys it did not write, and stops if a unit in that account's home would take the place
of the one it installs. The admin account you type for *Let the router log in once* is a different one: yours.

The shared directories (`input`, `output`, `user` on the NAS) must be writable by the service account's uid — the same
uid on every host (setup's *Service account uid*, default 47800): `chown 47800` them on the NAS once, or the host check
says so.

**In the dashboard: System → Workers → Set up a worker** (docs/design/14). Describe the host — address, instance name,
GPU, group, the profile to run, the router's address as the worker reaches it, the paths. Hover the **?** next to a
field for what it is for. The form starts from what the deployment knows: the compose paths of the artifact store and
the shared storage, the router at the address this page was opened at with the published router port, the newest
profile, and — on the first GPU worker — this page's host as its address and port 8288 (the router publishes 8188 on
the same host). Change only what differs on that host. Then choose how setup gets root on the host:

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

With compose the builder builds everything: *Set up a worker* does not offer the next option. Only for farms without
the builder: **Allow builds from the dashboard on this host** — the host then builds recipes on request from
*System → Rollout → Profiles → From a recipe* ([rollouts.md](rollouts.md) §1) — as a separate build account that
owns the artifact store, never as the service account. It needs internet and git on that host (setup brings a pinned uv, checked against its release
checksum, into `/opt/comfyfarm/worker/bin`), and an artifact
store that is empty or already that account's ([design 14](../design/14-worker-setup.md)).

Worker access control: setup allows the router's address as the host reaches it (and, with the router on that very
host, Docker's bridge networks) — `COMFYFARM_ALLOW_NETWORK` in `/etc/comfyfarm/<instance>.env`. A profile's
`COMFYFARM_ALLOW` adds to it. A worker that allows nobody shows as **unsecured** in the dashboard; one that turns
requests away says from where (*refused … the last from <address>*) — if that is the gateway or the router (a worker
that reaches them over a network setup did not guess), add the address there. On AMD, the `rocm-gfx1201` recipe's
profiles carry `HSA_TOOLS_DISABLE_REGISTER=1` — without it every worker burns one CPU core while idle
([rollouts.md](rollouts.md#5-after-a-new-venv-watch-the-idle-worker)); keep it in profiles you make by hand. The host
check shows each venv's PyTorch version (also in `<ART>/venv/<name>/freeze.txt`).

One ComfyUI instance per GPU: ComfyUI keeps models in VRAM between jobs, so two instances sharing a GPU run each other
out of memory.

Without the dashboard (or to see what setup does): [rollouts.md](rollouts.md) §2 lists the same pieces by hand.

The router's keys directory holds its agent key (made on first use) and `known_hosts` — easy:
`COMFYFARM_DATA/state/keys`; strict: `/srv/comfyfarm/keys` on the host (`/keys` in the container).

### Adding a GPU machine

What it must share with the farm, at the **same absolute paths** as on the control-plane host:

- the shared storage (`COMFYFARM_SHARED`: `input`, `output`, `user`, writable by uid 47800);
- for its group, the same models (above).

The **artifact store is copied, not shared**, by default (*Artifact store on this host: Copied here by the farm*):
setup gives the GPU machine its own store at the control plane's path (`COMFYFARM_ARTIFACTS_MOUNT`), and the farm
copies into it what a profile needs — before a rollout drains the worker, or when it first starts — each file checked
against the builder's signature ([16-artifact-copies.md](../design/16-artifact-copies.md)). Nothing to mount; the
disk needs room for the venvs (tens of GB with several builds). A small NAS gains most: the venvs are read locally.
*Mounted here* instead uses a share you mount at that path (read-only is enough), as before.

On one control-plane host that holds the shared storage locally, export it, e.g. NFS (`/etc/exports` there, then
the same `/etc/fstab` line on the GPU machine, mounted before you run setup):

```
/srv/comfyfarm/shared  <gpu-host>(rw,no_subtree_check,all_squash,anonuid=47800,anongid=47800)
# on the GPU machine:  <control-plane>:/srv/comfyfarm/shared  /srv/comfyfarm/shared  nfs  _netdev,nofail  0 0
# a mounted artifact store, if you choose it:
/srv/comfyfarm/art     <gpu-host>(ro,no_subtree_check)
```

On SMB the owner comes from the mount (`uid=47800,gid=47800`), not from `chown`. Mount before Docker and setup start: a
directory used before its share is mounted fills the local disk under the mount point. Then *Set up a worker* for that
machine (its address, port 8188 is fine there); the form shows the control plane's paths under its fields and marks one
that differs. The same kind of GPU: the same group and profile. Another kind: a new
group, a *Build* with its recipe, its own profiles. Network: the GPU machine reaches the router's 8188 (renders and
copies of the artifacts); the control-plane host reaches the GPU machine's ssh (22) and its ComfyUI port.

### Removing a worker host

*System → Workers → Delete* (a running job is reported lost), then on the host:

```sh
sudo systemctl --user -M comfyfarm@ stop 'comfyfarm@*'           # its ComfyUI instances
sudo loginctl disable-linger comfyfarm && sudo loginctl terminate-user comfyfarm
sudo systemctl disable --now comfyfarm-build.path comfyfarm-fetch.path 2>/dev/null    # builds, copies
sudo rm -rf /etc/comfyfarm /opt/comfyfarm /etc/systemd/user/comfyfarm@.service /etc/systemd/system/comfyfarm-build.* \
            /etc/systemd/system/comfyfarm-fetch.* /var/lib/comfyfarm-store
sudo userdel -r comfyfarm             # its home: the router's key, the local copies, temp; -r comfyfarm-build likewise
sudo userdel comfyfarm-store          # copy mode: then remove its copies, the artifact and model store paths on this host
```

Storage is the site's and stays: the models, the shared storage, a mounted artifact store. A host that keeps other instances
keeps all of this: stop and disable `comfyfarm@<instance>` only, and remove its `/etc/comfyfarm/<instance>.env` and
`.check.json`.

## 4. In the dashboard

The order is [First day](#first-day). Groups: one per set of identical workers (the first is made with the first GPU
worker); requirements are optional — leave them empty unless a group must only take workers with a backend or enough
VRAM. Users: a new account may use every group unless you untick some; artists choose among theirs in ComfyUI.
A group's node list is read from its workers the first time someone needs it and again after every rollout to them;
*Refresh nodes* is only for changes made outside a rollout. Without the builder: Profiles (one for the UI worker, one
per kind of GPU worker) → *Set up a worker*: the UI worker, then each GPU host.

## 5. Check

```sh
docker compose exec router comfyfarm doctor
```

It finds the UI worker by itself (`--ui <url>` only to check another one). Every line should be OK: ledger, an
administrator, each worker reachable and *secured*, groups agreeing on nodes and models, GPU workers seeing files
uploaded through the UI worker (shared storage), each host's check through its agent, disk space. Then back up
([backup.md](backup.md)) — before anyone depends on the farm.
