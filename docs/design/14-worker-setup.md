# 14 — Setting up worker hosts

Status: **implemented** (2026-10-18) — `worker_setup.py`, `hostcheck.py`, agent `check`, `api/setup.py`, dashboard *Set up a worker* / *Check host*, doctor host lines; rehearsed end to end on empty hosts in a closed network, 1 router + 2 GPU workers (docs/research/install-rehearsal-2026-10-18.txt). Goal: once the control plane runs, an operator turns an empty Linux host into a
working GPU worker from the dashboard — checked, secured and assigned — without reading the install guide line by line.

## What a worker host needs

| Piece | Today (install.md) | Who may do it |
|---|---|---|
| service account, `render`/`video` groups, lingering user manager | by hand, root | root, once |
| `launcher.py`, `agent.py`, `hostcheck.py` | copied by hand | **root, once per upgrade of these tools** |
| systemd unit, instance environment | by hand | root |
| the router's agent key with a forced command | by hand | root |
| shared storage mounts (input/output/user, models, `<ART>`) | by hand | root |
| GPU driver | the site's business | the site — ComfyFarm only **checks** it |
| ComfyUI core, venv, nodes | launcher, from published artifacts | the launcher, as the service account |

## The line we do not cross (D-40)

The launcher is the last gate against a compromised router (D-37): it refuses any render that is not stock ComfyUI from
published artifacts. That only holds if the router cannot replace the launcher. So:

- The worker-side tools, the unit and the instance environment are installed **by root during setup**, owned by root,
  read-only for the service account (`/opt/comfyfarm/worker/`, `/etc/systemd/user/comfyfarm@.service`,
  `/etc/comfyfarm/<instance>.env`, `/etc/comfyfarm/agent.env`, a root-owned `authorized_keys`).
- The router's everyday key (the restart agent's forced command) can do exactly four things: `restart`, `status`,
  `check` (the read-only host check below) and nothing else. Upgrading the worker tools is a new setup run — an
  operator-approved moment.
- The GPU driver is never installed by ComfyFarm. A site that runs a farm has a driver policy; we report what we find.

## Two ways to get root, the operator chooses (D-41)

Setup needs root for minutes, once. The dashboard offers both and explains the trade-off next to the choice:

1. **Run a command yourself** (default). The dashboard shows a one-line command for that host. The operator runs it with
   `sudo` there; it downloads the setup script from the router (with a one-time token), checks its SHA-256 against the
   value shown in the dashboard, and runs it. The router never holds root on the worker.
2. **Let the router log in once.** The operator types an administrator account and password for that host into the
   dashboard. The router logs in over ssh, runs the same script with `sudo`, and forgets the password (never written to
   the ledger, the log or disk). Quicker for many hosts; for those minutes the router has root on the worker.

Both run the same script with the same parameters and report the same way, so everything after is identical.

## The setup script (`worker_setup.py`, standard library)

Served by the router at `GET /api/worker_setup/<token>/script` (the token is one-time-ish: valid 2 hours, until the
setup finishes). The script embeds the worker tools of this router version and the setup parameters; it is idempotent
(run it again to repair or upgrade) and has `--dry-run`.

1. Preconditions: Linux with systemd, Python ≥ 3.10, root.
2. Account `comfyfarm` (configurable) with a **fixed uid** (default 47800 — shared storage checks numbers, so every host
   uses the same one): system user, home `/var/lib/comfyfarm`, groups `render` and `video` when present.
3. Tools to `/opt/comfyfarm/worker/` (root, 0755/0644); unit to `/etc/systemd/user/`; instance environment and agent
   configuration to `/etc/comfyfarm/` (root, 0644 — no secrets in them).
4. Optional mounts: `server:/export → mountpoint` lines become NFS entries in `/etc/fstab` (`_netdev,nofail`) and are
   mounted now.
5. Directories — after the mounts: `<LOCAL>` (owned by the account), the instance's input/output/user/temp
   (created if missing; on shared storage they normally exist and must be writable by the account's uid).
6. The router's agent public key into the account's `authorized_keys` (root-owned) with the forced command and every
   forwarding off.
7. `loginctl enable-linger`; the unit is **not** started (nothing is assigned yet).
8. `hostcheck` as the account; the result, the host's ssh host keys and the tool versions are POSTed back to
   `/api/worker_setup/<token>/report`. The router pins the host keys (no trust-on-first-use later).

## The host check (`hostcheck.py`, standard library, read-only)

Runs as the service account: at the end of setup, from the dashboard (*Check host*, via the agent's `check` verb), and
from `comfyfarm doctor` for every instance with an ssh backend. One line per check, OK / WARN / FAIL, as JSON for the
router:

- system: Linux, systemd user manager running, lingering on, Python version for the launcher
- GPU: AMD — `/dev/kfd` and a render node readable/writable by the account, GPUs from the KFD topology (gfx target),
  `amdgpu` version; NVIDIA — `/dev/nvidiactl` access, `nvidia-smi` names and driver version; none found → FAIL when the
  setup said a GPU is expected
- paths: `<ART>` readable with published artifacts, `<LOCAL>` writable with free space, models readable,
  input/output/user/temp writable, the user-writable rule of contract §4 (shared code with the launcher)
- venvs: every published venv runs here and imports torch — a missing system library is named with its package
  (rehearsal: ROCm torch needs `libatomic1`)
- time: NTP synchronised (`timedatectl`), clock within a few seconds of the router's
- network: the router answers (`/api/health/dispatch`), the instance port is free (or the instance owns it)
- shared storage: writes a marker into the output directory; the router reads every host's markers and says which
  hosts see the same files

## Router side

- Agent key: generated on first use (`ssh-keygen -t ed25519`) in `COMFYFARM_KEYS_DIR`; `known_hosts` there too.
- Ledger: `worker_setups` (spec, mode, status, log, result, pinned host keys, expiry; a hash of the token, never the
  token).
- Admin API: create a setup (spec → command, script SHA-256, token URL) · run it over ssh (account + password in the
  request body only) · read it · *finish* (add the worker, configure its instance with the ssh backend, assign the
  chosen profile, start the unit through the agent, wait for `match`).
- Dashboard: *Set up a worker* (System → Workers), a step-by-step dialog; *Check host* on each configured instance.

## Venvs carry their interpreter (D-42)

A venv built by `uv venv` points `bin/python` at uv's interpreter store in the builder's home — the artifact then works
only on the build host. Contract §2 already says a venv references nothing outside itself; publishing now puts the
interpreter inside the artifact (`<ART>/venv/<name>/.python/`) and refuses a venv whose `bin/python` or `pyvenv.cfg`
`home` resolves outside it. The host check runs every venv's interpreter on the host.

`<ART>` must be mounted at the same absolute path on every host (venvs are built at their final path).

## Not in this round

Windows workers (contract §9.2), driver installation, non-NFS mounts written by setup (SMB/CIFS: mount it yourself, the
check still verifies it), a fleet view of host checks over time.
