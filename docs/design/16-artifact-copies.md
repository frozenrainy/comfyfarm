# 16 — Artifact copies: GPU hosts without a shared artifact store

Status: **implemented** (0.1.0-dev.14). Rehearsed with a second host that shares no artifact store (a systemd
container on the control plane's network): set up in copy mode by default; its first start copied ComfyUI, the CPU venv
(2.3 GB) and the node set in about 30 s and ran a job; a rollout copied a node set the host lacked before it drained
the worker (step `fetching`); a list signed with another key was refused and nothing installed. Found on the way: the
builder's uid had no passwd entry in the image, and ssh-keygen refuses to run without one (the image has the account
now; a builder without a key still builds), and a CPU-only worker failed its setup on a GPU its account could not open
(a warning now).

## Why

Every host runs what the builder published, at the **same absolute path** (a venv names its own interpreter by path,
D-42). Until now the operator made that true: a share mounted on every host, or a copy kept in step by hand. A site
whose only storage is a small NAS pays for it at every start — a venv is tens of thousands of files read over the
network — and a site without NFS had to `rsync` after every build, before every rollout (forgotten once, the rollout
fails with "artifact not ready").

Now the farm copies. **Copy mode is the default** for a GPU host set up from the dashboard; a mounted store (NFS, a
share) stays a choice of the setup form.

## The rule that must not change

A router that is taken over cannot run code of its choosing on the workers (D-37). Today that holds because the store
is written by the builder only and the router mounts it read-only. Copies come *through* the router, so the router
must not be able to change what it hands on:

- The **builder signs** every artifact it publishes: a list of its files (path, type, mode, size, SHA-256; a link's
  target) in `<ART>/signed/<kind>/<name>.json`, signed with an SSH key only the builder holds (`ssh-keygen -Y sign`,
  namespace `comfyfarm-artifact`). The builder signs what is in its store on start (older artifacts) and after each
  build. Its public key is in the builder's policy.
- **Setup pins that key** on the host (`/etc/comfyfarm/artifact-signers`, root's) — the same trust as the rest of
  setup (D-41): what the operator runs at setup is what the farm is.
- The host **verifies** the signature (`ssh-keygen -Y verify`, OpenSSH ≥ 8.1, already there for the router's agent)
  and then every file against the list while it writes it. Anything else in the tar — another path, another type, a
  link elsewhere, a different byte — and the copy is thrown away.

The router only streams the store it already reads (`GET /api/worker_artifacts/<kind>/<name>`), to the hosts of its
instances.

## On the host

| | who | what |
|---|---|---|
| `comfyfarm-store` | its own account (setup makes it) | owns the local store at `<ART>`; downloads and verifies |
| the service account | runs ComfyUI | reads the store, as before — never writes it: custom node code cannot change a venv |
| `comfyfarm-fetch.path`, `.service` | root | takes requests from a drop box, runs the download as `comfyfarm-store` |

As builds (14): `/var/lib/comfyfarm-store` is root's (0711); `requests/` takes a file from the service account
(0730), `status/` is root's. A request names artifacts (`venv/rocm72-1` …), never a URL; a name that is not a
published, signed artifact simply fails.

- **The launcher**, starting with a render whose artifacts are missing here, asks for them and waits (its log says
  what it waits for), then starts. A failed copy is a refusal (exit 2): the unit does not restart in a loop.
- **A rollout** asks for the target's artifacts *before* it drains the worker (step `fetching`): the worker keeps
  taking jobs while the copy runs, and is out of service for the restart only. An agent without `fetch` (older worker
  tools, the UI worker container, a manual backend) skips the step; the launcher still fetches at start.
- An existing store that is not empty and not `comfyfarm-store`'s — a mount, or the control plane's own store on the
  one-server farm — is used as it is: setup says so, and nothing is copied there.

## Not (yet)

- Removing copies no profile uses any more (they stay; the host check shows the space).
- Models: not artifacts. They stay the site's (a share, or a copy per GPU worker); the group consistency check says
  when workers differ.
- Resuming an interrupted download: it starts again.
